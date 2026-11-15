"""Wait for native cleanup to finish when its async caller is cancelled."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable


async def drain_close(operation: Awaitable[None]) -> None:
    """Preserve cancellation only after an owned resource's close has finished.

    Cancelling ``asyncio.to_thread`` stops its awaiter, not its native worker. Closing
    a second handle or retrying the resource while that worker runs is unsafe.
    """
    closing: asyncio.Future[None] = asyncio.ensure_future(operation)
    cancelled = False
    while True:
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            cancelled = True
            if not closing.done():
                # Another cancel may arrive while draining. Keep shielding the same
                # operation until native cleanup has actually finished.
                continue
        break
    closing.result()  # A native close error takes precedence over cancellation.
    if cancelled:
        raise asyncio.CancelledError
