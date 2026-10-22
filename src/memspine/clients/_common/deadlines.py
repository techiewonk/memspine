"""Deadlines for connector opens and health round trips."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable

__all__ = ["check_deadline", "probe"]


def check_deadline(value: float, name: str = "deadline_s") -> float:
    """A finite positive number of seconds (booleans rejected)."""
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return float(value)


async def probe(deadline_s: float, check: Callable[[], Awaitable[bool]]) -> bool:
    """Run one health round trip under ``deadline_s``; any error or timeout is ``False``.

    Exception text is dropped: it may carry credentials or vendor diagnostics."""
    check_deadline(deadline_s)
    try:
        async with asyncio.timeout(deadline_s):
            return bool(await check())
    except Exception:
        return False
