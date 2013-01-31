'''A deferred module with almost the same API as twisted.'''
import sys
from copy import copy
import logging
import traceback
from threading import current_thread, local
from collections import deque, namedtuple
from itertools import chain
from inspect import isgenerator, isfunction, ismethod, istraceback

from pulsar import AlreadyCalledError, HaltServer
from pulsar.utils import events
from pulsar.utils.pep import raise_error_trace, iteritems, default_timer

from .access import get_request_loop, NOTHING


__all__ = ['Deferred',
           'EventHandler',
           'MultiDeferred',
           'DeferredGenerator',
           'Failure',
           'as_failure',
           'is_failure',
           'log_failure',
           'is_async',
           'maybe_async',
           'make_async',
           'safe_async',
           'async',
           'multi_async',
           'maybe_async_deco',
           'NOT_DONE',
           'STOP_ON_FAILURE',
           'CLEAR_ERRORS']

# Special objects
class NOT_DONE(object):
    pass

class STOP_ON_FAILURE(object):
    pass

class CLEAR_ERRORS(object):
    pass

class DeferredFailure(Exception):
    '''Raised when no other information is available on a Failure'''
    

EMPTY_DICT = {}
EMPTY_TUPLE = ()
LOGGER = logging.getLogger('pulsar.defer')

remote_stacktrace = namedtuple('remote_stacktrace', 'error_class error trace')

pass_through = lambda result: result

def iterdata(stream, start=0):
    '''Iterate over a stream which is either a dictionary or a list. This
iterator is over key-value pairs for a dictionary, and index-value pairs
for a list.'''
    if isinstance(stream, dict):
        return iteritems(stream)
    else:
        return enumerate(stream, start)

def is_generalised_generator(value):
    '''Check if *value* is a generator. This is more general than the
inspect.isgenerator function.'''
    return hasattr(value, '__iter__') and not hasattr(value, '__len__')

def is_stack_trace(trace):
    if isinstance(trace, remote_stacktrace):
        return True
    elif isinstance(trace,tuple) and len(trace) == 3:
        return istraceback(trace[2]) or\
                 (trace[2] is None and isinstance(trace[1],trace[0]))
    return False

def is_failure(value):
    return isinstance(value, Failure)

def as_failure(value, msg=None):
    '''Convert *value* into a :class:`Failure` if it is a stack trace or an
exception, otherwise returns *value*.'''
    if isinstance(value, Exception):
        exc_info = sys.exc_info()
        if value == exc_info[1]:
            return Failure(exc_info, msg)
        else:
            return Failure((value.__class__, value, None), msg)
    elif is_stack_trace(value):
        return Failure(value, msg)
    else:
        return value

def is_async(obj):
    '''Check if *obj* is an asynchronous instance'''
    return isinstance(obj, Deferred)

def maybe_async(val, description=None, max_errors=None):
    '''Convert *val* into an asynchronous instance only if *val* is a generator
or a function. If *val* is a :class:`Deferred` it checks if it has been
called and all callbacks have been consumed.
In this case it returns the :attr:`Deferred.result` attribute.'''
    if isgenerator(val):
        val = DeferredGenerator(val, max_errors=max_errors,
                                description=description)
    if is_async(val):
        return val.result_or_self()
    else:
        return as_failure(val)
    
def maybe_async_deco(f):
    def _(*args, **kwargs):
        return maybe_async(f(*args, **kwargs))
    return _

def make_async(val=None, description=None, max_errors=None):
    '''Convert *val* into an :class:`Deferred` asynchronous instance
so that callbacks can be attached to it.

:parameter val: can be a generator or any other value. If a generator, a
    :class:`DeferredGenerator` instance will be returned.
:parameter max_errors: the maximum number of errors tolerated if *val* is
    a generator. Default `None`.
:return: a :class:`Deferred` instance.

This function is useful when someone needs to treat a value as a deferred::

    v = ...
    make_async(v).add_callback(...)

'''
    val = maybe_async(val, description, max_errors)
    if not is_async(val):
        d = Deferred(description=description)
        d.callback(val)
        return d
    else:
        return val
    
def multi_async(iterable, **kwargs):
    return MultiDeferred(iterable, **kwargs).lock()
    
def safe_async(f, args=None, kwargs=None, description=None, max_errors=None):
    '''Execute function *f* safely and **always** returns an asynchronous
result.

:parameter f: function to execute
:parameter args: tuple of positional arguments for *f*.
:parameter kwargs: dictionary of key-word parameters for *f*.
:parameter description: Optional description for the :class:`Deferred` returned.
:parameter max_errors: the maximum number of errors tolerated if a :class:`DeferredGenerator`
    is returned.
:return: a :class:`Deferred` instance.
'''
    try:
        kwargs = kwargs if kwargs is not None else EMPTY_DICT
        args = args or EMPTY_TUPLE
        result = f(*args, **kwargs)
    except Exception:
        result = sys.exc_info()
    return make_async(result, max_errors=max_errors, description=description)

def log_failure(failure):
    '''Log the *failure* if *failure* is a :class:`Failure` or a
:class:`Deferred` with a called failure.'''
    failure = maybe_async(failure)
    if is_failure(failure):
        failure.log()
    return failure

############################################################### DECORATORS
class async:
    '''A decorator class which transforms a function into
an asynchronous callable.
    
:parameter max_errors: The maximum number of errors permitted if the
    asynchronous value is a :class:`DeferredGenerator`.
:parameter description: optional description.

Typical usage::

    @async()
    def myfunction(...):
        ...
'''
    def __init__(self, max_errors=None, description=None):
         self.max_errors = max_errors
         self.description = description or 'async decorator for '

    def __call__(self, func):
        description = '%s%s' % (self.description, func.__name__)
        def _(*args, **kwargs):
            return safe_async(func, args=args, kwargs=kwargs,
                              max_errors=self.max_errors,
                              description=description)
        _.__name__ = func.__name__
        _.__doc__ = func.__doc__
        return _


def coroutine(f):
    def _(*args, **kwargs):
        gen = f(*args, **kwargs)
        next(gen)
        return gen
    return _
            
        
############################################################### FAILURE
class Failure(object):
    '''Aggregate errors during :class:`Deferred` callbacks.

.. attribute:: traces

    List of (``errorType``, ``errvalue``, ``traceback``) occured during
    the execution of a :class:`Deferred`.

.. attribute:: logged

    Check if the last error was logged. It can be a way ofm switching off
    logging for certain errors.
'''
    def __init__(self, err=None, msg=None):
        self.should_stop = False
        self.msg = msg or ''
        self.traces = []
        self.append(err)

    def __repr__(self):
        return '\n\n'.join(self.format_all())
    __str__ = __repr__

    
    def _get_logged(self):
        return getattr(self.trace[1], '_failure_logged', False)
    def _set_logged(self, value):
        err = self.trace[1]
        if err:
            setattr(err, '_failure_logged', value)
    logged = property(_get_logged, _set_logged)
    
    def append(self, trace):
        '''Add new failure to self.'''
        if trace:
            if isinstance(trace, Failure):
                self.traces.extend(trace.traces)
            elif isinstance(trace, Exception):
                self.traces.append(sys.exc_info())
            elif is_stack_trace(trace):
                self.traces.append(trace)
        return self

    def clear(self):
        self.traces = []

    def format_all(self):
        for exctype, value, tb in self:
            if istraceback(tb):
                tb = traceback.format_exception(exctype, value, tb)
            if tb:
                yield '\n'.join(tb)
            else:
                yield str(value)
    
    def is_instance(self, classes):
        return isinstance(self.trace[1], classes)
            
    def __getstate__(self):
        self.log()
        traces = []
        for exctype, value, tb in self:
            if istraceback(tb):
                tb = traceback.format_exception(exctype, value, tb)
            traces.append(remote_stacktrace(exctype, value, tb))
        state = self.__dict__.copy()
        state['traces'] = traces
        return state

    def __getitem__(self, index):
        return self.traces[index]

    def __len__(self):
        return len(self.traces)

    def __iter__(self):
        return iter(self.traces)

    def raise_all(self, first=True):
        pos = 0 if first else -1
        if self.traces and isinstance(self.traces[pos][1], Exception):
            eclass, error, trace = self.traces.pop()
            self.log()
            raise_error_trace(error, trace)
        else:
            self.log()
            N = len(self.traces)
            if N == 1:
                raise DeferredFailure(
                    'There was one failure during callbacks.')
            elif N > 1:
                raise DeferredFailure(
                    'There were {0} failures during callbacks.'.format(N))

    @property
    def trace(self):
        if self.traces:
            return self.traces[-1]
        else:
            return (None,None,None)

    def log(self, log=None):
        if not self.logged:
            self.logged = True
            log = log or LOGGER
            for e in self:
                log.critical(self.msg, exc_info=e)

############################################################### Deferred
class Deferred(object):
    """The main class of the pulsar asynchronous tools.
It is a callback which will be put off until later.
The implementation is very similar to the ``twisted.defer.Deferred`` object.

.. attribute:: called

    ``True`` if the deferred was called. In this case the asynchronous result
    is ready and available in the :attr:`result`.

.. attribute:: running

    ``True`` if the deferred is running callbacks.
    
.. attribute:: paused

    Integer indicating the number of times this :class:`Deferred` has been
    paused because the result of a callback was another :class::`Deferred`.

.. attribute:: result

    This is available once the :class:`Deferred` has been called back. Note,
    this can be anything, including another :class:`Deferred`. Trying to access
    this attribute when :attr:`called` is ``False`` will result in an
    ``AttributeError`` exception.
"""
    paused = 0
    _called = False
    _runningCallbacks = False

    def __init__(self, description=None):
        self._description = description
        self._callbacks = deque()

    def __repr__(self):
        v = self._description or self.__class__.__name__
        if self.called:
            v += ' (called)'
        return v

    def __str__(self):
        return self. __repr__()

    @property
    def called(self):
        return self._called

    @property
    def running(self):
        return self._runningCallbacks

    def add_callback(self, callback, errback=None):
        """Add a callback as a callable function.
The function takes at most one argument, the result passed to the
:meth:`callback` method. If the *errback* callable is provided it will
be called when an exception occurs."""
        errback = errback if errback is not None else pass_through
        if hasattr(callback, '__call__') and hasattr(errback, '__call__'):
            self._callbacks.append((callback, errback))
            self._run_callbacks()
        else:
            raise TypeError('callback must be callable')
        return self

    def add_errback(self, errback):
        '''Same as :meth:`add_callback` but only for errors.'''
        return self.add_callback(pass_through, errback)

    def add_both(self, callback):
        '''Equivalent to `self.add_callback(callback, callback)`.'''
        return self.add_callback(callback, callback)

    def add_callback_args(self, callback, *args, **kwargs):
        return self.add_callback(\
                lambda result : callback(result, *args, **kwargs))

    def callback(self, result=None):
        '''Run registered callbacks with the given *result*.
This can only be run once. Later calls to this will raise
:class:`AlreadyCalledError`. If further callbacks are added after
this point, :meth:`add_callback` will run the *callbacks* immediately.

:return: the *result* input parameter
'''
        if isinstance(result, Deferred):
            raise RuntimeError('Received a deferred instance from '
                               'callback function')
        elif self._called:
            raise AlreadyCalledError('Deferred %s already called' % self)
        self.result = as_failure(result)
        self._called = True
        self._run_callbacks()
        return self.result

    def result_or_self(self):
        '''It returns the :attr:`result` only if available and all
callbacks have been consumed, otherwise it returns this :class:`Deferred`.
Users should use this method to obtain the result, rather than accessing
directly the :attr:`result` attribute.'''
        return self.result if self._called and not self._callbacks else self

    ##################################################    INTERNAL METHODS
    def _run_callbacks(self):
        if not self._called or self._runningCallbacks or self.paused:
            return
        while self._callbacks:
            callbacks = self._callbacks.popleft()
            callback = callbacks[is_failure(self.result)]
            try:
                self._runningCallbacks = True
                try:
                    self.result = maybe_async(callback(self.result))
                finally:
                    self._runningCallbacks = False
            except Exception as e:
                self._add_exception(e)
            else:
                if isinstance(self.result, Deferred):
                    # Add a pause
                    self._pause()
                    # Add a callback to the result to resume callbacks
                    self.result.add_both(self._continue)
                    break

    def _pause(self):
        """Stop processing until :meth:`unpause` is called."""
        self.paused += 1

    def _unpause(self):
        """Process all callbacks made since :meth:`pause` was called."""
        self.paused -= 1
        self._run_callbacks()

    def _continue(self, result):
        self.result = result
        self._unpause()
        return self.result

    def _add_exception(self, e):
        if not isinstance(self.result, Failure):
            self.result = Failure(e)
        else:
            self.result.append(e)
    
    
class EventHandler(object):
    '''A Mixin for handling one time events and events that occur several
times. This mixin is used in :class:`Protocol` and :class:`Producer`
for scheduling connections and requests.'''
    ONE_TIME_EVENTS = ()
    '''Event names which occur once only. Implemented as :class:`Deferred`.'''
    MANY_TIMES_EVENTS = ()
    '''Event names which occur several times. Implemented as list
of callables.'''
    
    def __init__(self):
        o = dict(((e, Deferred()) for e in self.ONE_TIME_EVENTS))
        m = dict(((e, []) for e in self.MANY_TIMES_EVENTS))
        self.ONE_TIME_EVENTS = o
        self.MANY_TIMES_EVENTS = m
        
    def event(self, name):
        '''Return the handler for event *name*.'''
        if name in self.ONE_TIME_EVENTS:
            return self.ONE_TIME_EVENTS[name]
        else:
            return self.MANY_TIMES_EVENTS[name]
    
    def bind_event(self, event, callback):
        '''Register a *callback* with *event*. The callback must be
a callable which accept one argument only.'''
        callback = self.safe_callback(event, callback)
        if event in self.ONE_TIME_EVENTS:
            self.ONE_TIME_EVENTS[event].add_both(callback)
        elif event in self.MANY_TIMES_EVENTS:
            self.MANY_TIMES_EVENTS[event].append(callback)
        else:
            LOGGER.warn('unknown event "%s" for %s', event, self)
        
    def fire_event(self, event, event_data=NOTHING):
        """Dispatches *event_data* to the *event* listeners.
If *event_data* is not provided, this instance will be dispatched.
If *event_data* is an error it will be converted to a :class:`Failure`."""
        event_data = self if event_data is NOTHING else as_failure(event_data)
        if event in self.ONE_TIME_EVENTS:
            self.ONE_TIME_EVENTS[event].callback(event_data)
        elif event in self.MANY_TIMES_EVENTS:
            for callback in self.MANY_TIMES_EVENTS[event]:
                callback(event_data)
        else:
            LOGGER.warn('unknown event "%s" for %s', event, self)
        events.fire(event, event_data)
        log_failure(event_data)
        
    def all_events(self):
        return chain(self.ONE_TIME_EVENTS, self.MANY_TIMES_EVENTS)
    
    def copy_many_times_events(self, other, *names):
        many = other.MANY_TIMES_EVENTS
        names = names or many
        for name in names:
            if name in many:
                if name in self.MANY_TIMES_EVENTS:
                    self.MANY_TIMES_EVENTS[name].extend(many[name])
                elif name in self.ONE_TIME_EVENTS:
                    d = self.ONE_TIME_EVENTS[name]
                    for callback in many[name]:
                        d.add_callback(callback)
    
    def safe_callback(self, event, callback):
        def _(result):
            try:
                callback(result)
            except Exception:
                LOGGER.exception('Unhandled exception in "%s" event', event)
            # always return result
            return result
        return _
            
                    
class DeferredGenerator(Deferred):
    '''A :class:`Deferred` for a generator over, possibly, deferred objects.
The callback will occur once the generator has stopped
(when it raises StopIteration), or a preset maximum number of errors has
occurred.

:parameter gen: a generator.
:parameter max_errors: The maximum number of exceptions allowed before
    stopping the generator and raise exceptions. By default the
    generator will continue regardless of errors, accumulating them into
    the final result.
'''
    def __init__(self, gen, max_errors=None, description=None,
                 error_handler=None, timeout=30):
        self.gen = gen
        self.max_errors = max(1, max_errors) if max_errors else 0
        self.timeout = timeout
        self.errors = Failure()
        super(DeferredGenerator,self).__init__(description=description)
        # the loop in the current thread... with preference to the request loop
        self.loop = get_request_loop()
        self._consume()

    def _consume(self, last_result=None):
        # Consume the generator
        self._start = default_timer()
        if isinstance(last_result, Failure):
            if self.should_stop(last_result):
                return self.conclude()
        try:
            result = next(self.gen)
        except StopIteration as e:
            # The generator has finished producing data
            return self.conclude(last_result)
        except Exception as e:
            if self.should_stop(e):
                return self.conclude()
            return self._consume()
        else:
            return self._check_async(result)

    def _check_async(self, result):
        result = maybe_async(result)
        if is_async(result):
            if default_timer() - self._start > self.timeout:
                try:
                    raise RuntimeError('Timeout!')
                except Exception as e:
                    return self.callback(e)
            self.loop.call_soon_threadsafe(self._check_async, result)
        elif result == NOT_DONE:
            self.loop.call_soon_threadsafe(self._consume)
        else:
            if result == CLEAR_ERRORS:
                self.errors.clear()
                result = None
            self._consume(result)
        
    def should_stop(self, failure):
        self.errors.append(failure)
        return self.max_errors and len(self.errors) >= self.max_errors

    def conclude(self, last_result=None):
        # Conclude the generator and callback the listeners
        result = last_result if not self.errors else self.errors
        self.gen = None
        self.errors = None
        return self.callback(result)


############################################################### MultiDeferred
class MultiDeferred(Deferred):
    '''A :class:`Deferred` for managing a stream if independent objects
which may be :class:`Deferred`.

.. attribute:: lock

    If ``True`` items can no longer be added to this :class:`MultiDeferred`.
    
.. attribute:: type

    The type of multideferred. Either a ``list`` or a ``dict``.
'''
    _locked = False

    def __init__(self, data=None, type=None, fireOnOneErrback=False,
                 handle_value=None, log_failure=False):
        self._deferred = {}
        self._failures = Failure()
        self.log_failure = log_failure
        self.fireOnOneErrback = fireOnOneErrback
        self.handle_value = handle_value
        if not type:
            type = data.__class__ if data is not None else list
        if not issubclass(type, (list, dict)):
            type = list
        self._stream = type()
        super(MultiDeferred, self).__init__()
        if data:
            self.update(data)

    @property
    def locked(self):
        return self._locked
        
    @property
    def type(self):
        return self._stream.__class__.__name__

    def lock(self):
        '''Lock the :class:`MultiDeferred` so that no new items can be added.
If it was alread :attr:`locked` a runtime exception is raised.'''
        if self._locked:
            raise RuntimeError(self.__class__.__name__ +\
                        ' cannot be locked twice.')
        self._locked = True
        if not self._deferred:
            self._finish()
        return self

    def update(self, stream):
        '''Update the :class:`MultiDeferred` with new data. It works for
both ``list`` and ``dict`` types.'''
        add = self._add
        for key, value in iterdata(stream, len(self._stream)):
            add(key, value)
        return self

    def append(self, value):
        '''Append only works for a list type multideferred'''
        if self.type == 'list':
            self._add(len(self._stream), value)
        else:
            raise RuntimeError('Cannot append a value to a "dict" type '
                               'multideferred')

    def _add(self, key, value):
        if self._locked:
            raise RuntimeError(self.__class__.__name__ +\
                               ' cannot add a dependent once locked.')
        if is_generalised_generator(value):
            value = list(value)
        value = maybe_async(value)
        if isinstance(value, (dict, list, tuple, set, frozenset)):
            value = self._make(value)    
        if not is_async(value) and self.handle_value:
            try:
                val = self.handle_value(value)
            except Exception as e:
                value = as_failure(e)
            else:
                if val is not value:
                    return self._add(key, val)
        self._setitem(key, value)
        # add callback if an asynchronous value
        if is_async(value):
            self._add_deferred(key, value)

    def _make(self, value):
        md = self.__class__(value, fireOnOneErrback=self.fireOnOneErrback,
                            handle_value=self.handle_value)
        return maybe_async(md.lock())

    def _add_deferred(self, key, value):
        self._deferred[key] = value
        value.add_both(lambda result: self._deferred_done(key, result))

    def _deferred_done(self, key, result):
        self._deferred.pop(key, None)
        self._setitem(key, result)
        if self._locked and not self._deferred and not self.called:
            self._finish()
        return result

    def _finish(self):
        if not self._locked:
            raise RuntimeError(self.__class__.__name__ +\
                               ' cannot finish until completed.')
        if self._deferred:
            raise RuntimeError(self.__class__.__name__ +\
                               ' cannot finish whilst waiting for '
                               'dependents %r' % self._deferred)
        if self.fireOnOneErrback and self._failures:
            self.callback(self._failures)
        else:
            self.callback(self._stream)

    def _setitem(self, key, value):
        stream = self._stream
        if isinstance(stream, list) and key == len(stream):
            stream.append(value)
        else:
            stream[key] = value
        if is_failure(value):
            if self.log_failure:
                log_failure(value)
            self._failures.append(value)
