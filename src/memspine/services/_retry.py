"""Retry transient provider failures (network drops, throttling, 5xx) with backoff.

A dropped connection to a cloud model must not abort a whole write or read: one
"Server disconnected" mid-ingest otherwise kills the caller (seen in the paid LoCoMo
runs, 2026-10-03). Only transient classes are retried; bad requests, auth and
validation errors surface at once.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from memspine.observability.logging import get_logger

__all__ = ["TRANSIENT_ERRORS", "retry_transient"]

_log = get_logger(__name__)

#: Exception class names treated as transient (litellm's mapped classes plus the
#: underlying transport ones), matched by name so no provider SDK is imported here.
TRANSIENT_ERRORS = frozenset(
    {
        "APIConnectionError",
        "Timeout",
        "APITimeoutError",
        "RateLimitError",
        "ServiceUnavailableError",
        "InternalServerError",
        "ServerDisconnectedError",
        "ClientConnectorError",
        "ConnectionError",
        "ConnectionResetError",
        "TimeoutError",
        "ThrottlingException",
    }
)


def _is_transient(exc: BaseException) -> bool:
    return any(cls.__name__ in TRANSIENT_ERRORS for cls in type(exc).__mro__)


async def retry_transient[T](
    call: Callable[[], Awaitable[T]],
    *,
    what: str,
    attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
) -> T:
    """Await ``call()``, retrying transient failures with exponential backoff."""
    for attempt in range(1, attempts + 1):
        try:
            return await call()
        except Exception as exc:
            if attempt == attempts or not _is_transient(exc):
                raise
            delay = min(max_delay, base_delay * 2 ** (attempt - 1))
            _log.warning(
                "provider.transient_retry",
                what=what,
                attempt=attempt,
                delay_s=delay,
                error=type(exc).__name__,
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover
