"""Intrinsic synchronization for built-in stores and turn reservations."""
import contextvars
import functools
import inspect
from contextlib import contextmanager

_reservations = contextvars.ContextVar("agentcli_turn_reservations", default=None)


def synchronized_store(cls):
    """Protect complete public operations, including nested calls, with RLock."""
    def protect(function):
        @functools.wraps(function)
        def locked(self, *args, **kwargs):
            with self._lock:
                return function(self, *args, **kwargs)
        return locked
    for name, function in list(vars(cls).items()):
        if not name.startswith("_") and inspect.isfunction(function):
            setattr(cls, name, protect(function))
    return cls


def reserve_conversation(store, conv):
    state = _reservations.get()
    if state is None:
        return
    key = (id(store), conv.id)
    if key not in state:
        pin = getattr(store, "pin", None)
        if pin is not None:
            pin(conv.id)
    state[key] = (store, {k: v for k, v in conv.metadata.items()
                         if k.startswith("session_reset_generation:")})


def session_generation_matches(store, conv, provider):
    state = _reservations.get()
    entry = state.get((id(store), conv.id)) if state is not None else None
    if entry is None:
        return True
    key = "session_reset_generation:" + provider
    return entry[1].get(key, 0) == conv.metadata.get(key, 0)


@contextmanager
def _scope(state):
    token = _reservations.set(state)
    try:
        yield
    finally:
        _reservations.reset(token)


def _release(state):
    for (_, cid), (store, _) in state.items():
        unpin = getattr(store, "unpin", None)
        if unpin is not None:
            unpin(cid)


def reserved_turn(function):
    """Keep handles alive from preparation through queued/active completion."""
    if inspect.isasyncgenfunction(function):
        @functools.wraps(function)
        async def streaming(self, *args, **kwargs):
            state = {}
            iterator = function(self, *args, **kwargs)
            try:
                while True:
                    try:
                        with _scope(state):
                            chunk = await anext(iterator)
                    except StopAsyncIteration:
                        return
                    yield chunk
            finally:
                try:
                    with _scope(state):
                        await iterator.aclose()
                finally:
                    _release(state)
        return streaming
    if inspect.iscoroutinefunction(function):
        @functools.wraps(function)
        async def asynchronous(self, *args, **kwargs):
            state = {}
            try:
                with _scope(state):
                    return await function(self, *args, **kwargs)
            finally:
                _release(state)
        return asynchronous
    @functools.wraps(function)
    def synchronous(self, *args, **kwargs):
        state = {}
        try:
            with _scope(state):
                return function(self, *args, **kwargs)
        finally:
            _release(state)
    return synchronous
