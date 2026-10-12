"""Optional request budgets shared by fallbacks and corrective retries."""
import asyncio
import contextvars
import functools
import inspect
import time
from contextlib import contextmanager

_deadline = contextvars.ContextVar("agentcli_request_deadline", default=None)


def remaining_timeout(timeout):
    deadline = _deadline.get()
    return timeout if deadline is None else min(timeout, max(0, deadline - time.monotonic()))


def request_budget(function):
    @contextmanager
    def scope(duration):
        if duration is None:
            yield
            return
        if duration < 0:
            raise ValueError("wall_timeout must be nonnegative")
        deadline = time.monotonic() + duration
        parent = _deadline.get()
        token = _deadline.set(min(parent, deadline) if parent is not None else deadline)
        try:
            yield
        finally:
            _deadline.reset(token)
    if inspect.iscoroutinefunction(function):
        @functools.wraps(function)
        async def asynchronous(*args, **kwargs):
            with scope(kwargs.get("wall_timeout")):
                return await function(*args, **kwargs)
        return asynchronous
    @functools.wraps(function)
    def synchronous(*args, **kwargs):
        with scope(kwargs.get("wall_timeout")):
            return function(*args, **kwargs)
    return synchronous


async def bounded_stream(iterator, deadline):
    """Keep a single deadline across native retries; always close the iterator."""
    try:
        while True:
            try:
                if deadline is None:
                    item = await anext(iterator)
                else:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise asyncio.TimeoutError("wall timeout expired")
                    item = await asyncio.wait_for(anext(iterator), left)
            except StopAsyncIteration:
                return
            yield item
    finally:
        close = getattr(iterator, "aclose", None)
        if close:
            await close()
