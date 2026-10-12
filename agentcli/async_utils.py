"""Cancellation-safe completion of owned synchronous operations."""
import asyncio
import contextvars
import functools


async def complete_in_thread(function, *args, **kwargs):
    """Do not release resources while an already-started worker still uses them."""
    # Not a Task: loop shutdown cancels all Tasks, but must not mark a running
    # worker finished. Copy request context just as to_thread does.
    context = contextvars.copy_context()
    task = asyncio.get_running_loop().run_in_executor(
        None, context.run, functools.partial(function, *args, **kwargs))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    if cancelled:
        # Retrieve any exception before honoring cancellation of the owner.
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    return task.result()
