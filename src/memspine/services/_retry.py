"""Retry transient provider failures (network drops, throttling, 5xx) with backoff.

A dropped connection to a cloud model must not abort a whole write or read: one
"Server disconnected" mid-ingest otherwise kills the caller (seen in the paid LoCoMo
runs, 2026-10-03). Only transient classes are retried; bad requests, auth and
validation errors surface at once.
"""

from __future__ import annotations

import asyncio
import re
import time
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


#: Message fragments that mark a permanent failure even under a transient class name
#: (litellm wraps an expired-credential 403 in APIConnectionError).
_PERMANENT_HINTS = (
    "token included in the request is expired",
    "expiredtoken",
    "unrecognizedclient",
    "invalidsignature",
    "accessdenied",
    "403 forbidden",
    # B-6: non-AWS providers (OpenAI-compatible, Azure, local servers). A spent
    # quota arrives as a 429 RateLimitError but never clears by waiting.
    "insufficient_quota",
    "invalid_api_key",
    "incorrect api key",
    "not found",  # includes "model not found"
    "does not exist",
)
_HTTP_401 = re.compile(r"\b401\b")

#: Monotonic clock for the total retry deadline (a seam for tests).
_clock = time.monotonic


def _is_transient(exc: BaseException) -> bool:
    text = str(exc).lower()
    if any(hint in text for hint in _PERMANENT_HINTS) or _HTTP_401.search(text):
        return False
    return any(cls.__name__ in TRANSIENT_ERRORS for cls in type(exc).__mro__)


async def retry_transient[T](
    call: Callable[[], Awaitable[T]],
    *,
    what: str,
    attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    max_total_s: float = 60.0,
) -> T:
    """Await ``call()``, retrying transient failures with exponential backoff.

    ``max_total_s`` caps the whole retry budget: a retry whose backoff would end
    past that deadline (measured from the first attempt) is not taken, and the
    last error surfaces instead.
    """
    deadline = _clock() + max_total_s
    for attempt in range(1, attempts + 1):
        try:
            return await call()
        except Exception as exc:
            if attempt == attempts or not _is_transient(exc):
                raise
            delay = min(max_delay, base_delay * 2 ** (attempt - 1))
            if _clock() + delay > deadline:
                raise
            _log.warning(
                "provider.transient_retry",
                what=what,
                attempt=attempt,
                delay_s=delay,
                error=type(exc).__name__,
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover
