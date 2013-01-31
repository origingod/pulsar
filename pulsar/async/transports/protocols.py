from copy import copy

from pulsar import ProtocolError
from pulsar.utils.sockets import nice_address
from pulsar.async.access import NOTHING
from pulsar.async.defer import EventHandler

from .transport import TransportProxy, LOGGER


__all__ = ['Protocol', 'ProtocolConsumer', 'Connection', 'Producer']


class Protocol(EventHandler):
    '''Abstract class implemented in :class:`Connection`
and :class:`ProtocolConsumer`'''
    def connection_made(self, transport):
        '''Indicates that the :class:`Transport` is ready and connected
to the entity at the other end. The protocol should probably save the
transport reference as an instance variable (so it can call its write()
and other methods later), and may write an initial greeting or request
at this point.'''
        raise NotImplementedError
    
    def data_received(self, data):
        '''The transport has read some data from the connection.'''
        raise NotImplementedError
    
    def eof_received(self):
        '''This is called when the other end called write_eof() (or
something equivalent).'''
        raise NotImplementedError
        
    def connection_lost(self, exc):
        '''The transport has been closed or aborted, has detected that the
other end has closed the connection cleanly, or has encountered an
unexpected error. In the first three cases the argument is None;
for an unexpected error, the argument is the exception that caused
the transport to give up.'''
        pass

    
class ProtocolConsumer(Protocol):
    '''The :class:`Protocol` consumer is one most important
:ref:`pulsar primitive <pulsar_primitives>`. It is responsible for receiving
incoming data from a the :meth:`Protocol.data_received` method implemented
in :class:`Connection`. It is used to decode and producing responses, i.e.
writing back to the client or server via
the :attr:`transport` attribute. The only method to implement should
be :meth:`Protocol.data_received`.

By default it has `start` and `finish` :ref:`one time event <one-time-event>`
and `data_received` :ref:`many times event <many-times-event>`.

.. attribute:: connection

    The :class:`Connection` of this consumer
    
.. attribute:: transport

    The :class:`Transport` of this consumer
    
.. attribute:: request

    Optional :class:`Request` instance (used for clients).
    
.. attribute:: on_finished

    A :class:`Deferred` called once the :class:`ProtocolConsumer` has
    finished consuming protocol. It is called by the
    :attr:`connection` before disposing of this consumer. It is
    a proxy of ``self.event('finish')``.
'''
    ONE_TIME_EVENTS = ('finish',)
    MANY_TIMES_EVENTS = ('data_received',)
    def __init__(self, connection, request=None):
        super(ProtocolConsumer, self).__init__()
        self._connection = None
        self._current_request = None
        # this counter is updated by the connection
        self._data_received_count = 0
        # this counter is updated via the new_request method
        self._request_processed = 0
        self._reconnect_retries = 0
        self.new_request(request)
        connection.set_consumer(self)
    
    @property
    def connection(self):
        return self._connection
    
    @property
    def event_loop(self):
        if self._connection:
            return self._connection.event_loop
    
    @property
    def current_request(self):
        return self._current_request
        
    @property
    def transport(self):
        if self._connection:
            return self._connection.transport
    
    @property
    def address(self):
        if self._connection:
            return self._connection.address
        
    @property
    def producer(self):
        if self._connection:
            return self._connection.producer
    
    @property
    def on_finished(self):
        return self.event('finish')
    
    def start_request(self):
        '''Invoked by client consumer to kick start the request with
remote server.'''
        raise NotImplementedError
    
    def new_request(self, request):
        '''Reset this consumer for with a new *request*. This method is used by
:class:`Client` consumers when a request needs to be resubmitted. Not used
by :class:`Server` consumers.'''
        self._request_processed += 1
        self._current_request = request
    
    def reset_connection(self):
        if self._connection:
            consumer = copy(self)
            self._connection._current_consumer = consumer
            self._connection = None
            consumer.finish()
        
    def finished(self, result=NOTHING):
        '''Call this method when done with this :class:`ProtocolConsumer`.
By default it calls the :meth:`Connection.finished` method of the
:attr:`connection` attribute.'''
        if self._connection:
            return self._connection.finished(self, result)
        
    def connection_lost(self, exc):
        self.finished(exc)
        
    def _data_received(self, data):
        self._data_received_count += 1 
        self._reconnect_retries = 0
        return self.data_received(data)
        
        
class Connection(Protocol, TransportProxy):
    '''A client or server connection with an endpoint. This is not
connected until :meth:`Protocol.connection_made` is called by the
:class:`Transport`. This class is the bridge between the :class:`Transport`
and the :class:`ProtocolConsumer`. It has a :class:`Protocol`
interface and it routes data arriving from the :attr:`transport` to
the :attr:`current_consumer`, an instance of :class:`ProtocolConsumer`.

It has two :ref:`one time events <one-time-event>`, *connection_made* and
*connection_lost*, and three :ref:`many times events <many-times-event>`,
*pre_request*, *data_received* and *post_request*.

.. attribute:: producer

    The producer of this :class:`Connection`, It is either a :class:`Server`
    or a client :class:`Client`.
    
.. attribute:: transport

    The :class:`Transport` of this protocol connection. Initialised once the
    :meth:`Protocol.connection_made` is called.
    
.. attribute:: consumer_factory

    A factory of :class:`ProtocolConsumer` instances for this
    :class:`Connection`.
    
.. attribute:: session

    Connection session number. Created by the :attr:`producer`.
    
.. attribute:: processed

    Number of separate requests processed by this connection.
    
.. attribute:: current_consumer

    The :class:`ProtocolConsumer` currently handling incoming data.
'''
    ONE_TIME_EVENTS = ('connection_made', 'connection_lost')
    MANY_TIMES_EVENTS = ('data_received', 'pre_request', 'post_request')
    #
    def __init__(self, address, session, timeout, consumer_factory, producer):
        super(Connection, self).__init__()
        self._address = address
        self._session = session 
        self._processed = 0
        self._timeout = timeout
        self._idle_timeout = None
        self._current_consumer = None
        self._consumer_factory = consumer_factory
        self._producer = producer
        
    def __repr__(self):
        return '%s session %s' % (nice_address(self._address), self._session)
    
    def __str__(self):
        return self.__repr__()
    
    @property
    def session(self):
        return self._session
    
    @property
    def consumer_factory(self):
        return self._consumer_factory
    
    @property
    def current_consumer(self):
        return self._current_consumer
        
    @property
    def processed(self):
        return self._processed
    
    @property
    def address(self):
        return self._address
    
    @property
    def timeout(self):
        return self._timeout
    
    @property
    def producer(self):
        return self._producer
    
    def set_consumer(self, consumer, new=True):
        '''Set a new :class:`ProtocolConsumer` for this :class:`Connection`.'''
        assert self._current_consumer is None, 'Consumer is not None'
        self._current_consumer = consumer
        consumer._connection = self
        self._processed += 1
        self.fire_event('pre_request', consumer)
    
    def connection_made(self, transport):
        # Implements protocol connection_made
        self._transport = transport
        # let everyone know we have a connection with endpoint
        self.fire_event('connection_made')
        self._add_idle_timeout()
        
    def data_received(self, data):
        self._cancel_timeout()
        while data:
            consumer = self._current_consumer
            if consumer is None:
                # New consumer
                consumer = self._consumer_factory(self)
            data = consumer._data_received(data)
            if data and self._current_consumer:
                # if data is returned from the response feed method and the
                # response has not done yet raise a Protocol Error
                raise ProtocolError
        self._add_idle_timeout()
    
    def connection_lost(self, exc):
        '''Implements the :class:`Protocol.connection_lost` callback.
It performs these actions in the following order:
* Cancel the idle timeout if set
* Fire the *connection_lost* :ref:`one time event <one-time-event>` with *exc*
  as event data.
* Invokes the connection_lost method in the :attr:`current_consumer` if
  available.'''
        self._cancel_timeout()
        self.fire_event('connection_lost', exc)
        if self._current_consumer:
            self._current_consumer.connection_lost(exc)
                             
    def upgrade(self, consumer_factory):
        '''Update the :attr:`consumer_factory` attribute with a new
:class:`ProtocolConsumer` factory. This function can be used when the protocol
specification changes during a response (an example is a WebSocket
response).'''
        self._consumer_factory = consumer_factory
        
    def finished(self, consumer, result=NOTHING):
        '''Call this method to close the current *consumer*.'''
        if consumer is self._current_consumer:
            self.fire_event('post_request', consumer)
            consumer.fire_event('finish', result)
            self._current_consumer = None
            consumer._connection = None
        else:
            raise RuntimeError()
    
    ############################################################################
    ##    INTERNALS
    def _timed_out(self):
        LOGGER.info('%s idle for %d seconds. Closing connection.',
                        self, self._timeout)
        self.close()
         
    def _add_idle_timeout(self):
        if not self.closed and not self._idle_timeout and self._timeout:
            self._idle_timeout = self.event_loop.call_later(self._timeout,
                                                            self._timed_out)
            
    def _cancel_timeout(self):
        if self._idle_timeout:
            self._idle_timeout.cancel()
            self._idle_timeout = None
         
         
class Producer(Protocol):
    '''A Producer of :class:`Connection` with remote servers or clients.
It is the base class for both :class:`Server` and :class:`ConnectionPool`.
The main method in this class is :meth:`new_connection` where a new
:class:`Connection` is created and added to the set of
:attr:`concurrent_connections`.

.. attribute:: connection_factory

    A factory producing the :class:`Connection` from a
    remote client with this producer.
    This attribute is used in the :meth:`new_connection` method.
    There shouldn't be any reason to change the default :class:`Connection`,
    it is here just in case.
    
.. attribute:: concurrent_connections

    Number of concurrent active connections
    
.. attribute:: received

    Total number of received connections
    
.. attribute:: timeout

    number of seconds to keep alive an idle connection
    
.. attribute:: max_connections

    Maximum number of connections allowed. A value of 0 (default)
    means no limit.
'''
    connection_factory = Connection
    def __init__(self, max_connections=0, timeout=0, connection_factory=None):
        super(Producer, self).__init__()
        self._received = 0
        self._timeout = timeout
        self._max_connections = max_connections
        self._concurrent_connections = set()
        if connection_factory:
            self.connection_factory = connection_factory
    
    @property
    def timeout(self):
        return self._timeout
    
    @property
    def received(self):
        return self._received
    
    @property
    def max_connections(self):
        return self._max_connections
    
    @property
    def concurrent_connections(self):
        return len(self._concurrent_connections)
    
    def new_connection(self, address, consumer_factory, producer=None):
        '''Called when a new :class:`Connection` is created. The *producer*
is either a :class:`Server` or a :class:`Client`. If the number of
:attr:`concurrent_connections` is greater or equal :attr:`max_connections`
a :class:`RuntimeError` is raised.'''
        if self._max_connections and self._received >= self._max_connections:
            raise RuntimeError('Too many connections')
        # increased the connections counter
        self._received = session = self._received + 1
        # new connection - not yet connected!
        producer = producer or self
        conn = self.connection_factory(address, session, self.timeout,
                                       consumer_factory, producer)
        conn.bind_event('connection_made', self._add_connection)
        conn.copy_many_times_events(producer)
        conn.bind_event('connection_lost', self._remove_connection)
        return conn
    
    def close_connections(self, connection=None, async=True):
        '''Close *connection* if specified, otherwise close all
active connections.'''
        if connection:
            connection.transport.close(async)
        else:
            for connection in list(self._concurrent_connections):
                connection.transport.close(async)
            
    def _add_connection(self, connection):
        self._concurrent_connections.add(connection)
        
    def _remove_connection(self, connection):
        self._concurrent_connections.discard(connection)
    