"""Cancellation-safe completion of owned synchronous operations."""
import asyncio


async def complete_in_thread(function, *args, **kwargs):
    """Do not release resources while an already-started worker still uses them."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
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
