"""Async helpers for draining blocking provider work on cancellation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable


async def run_blocking_drained[T](
    function: Callable[..., T], /, *args: object, **kwargs: object
) -> T:
    """Wait for a started thread operation before allowing dependency teardown.

    Cancelling ``asyncio.to_thread``'s await does not stop its worker thread. A
    provider builder or service must drain that work before its SQL lease can
    close, then re-raise the caller's cancellation.
    """
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            # A completed worker exception is retrieved below, where a prior
            # caller cancellation takes precedence over the worker failure.
            break
    try:
        result = task.result()
    except BaseException:
        if cancelled:
            raise asyncio.CancelledError from None
        raise
    if cancelled:
        raise asyncio.CancelledError
    return result
