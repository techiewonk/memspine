"""Transient provider failures are retried with backoff; others surface at once."""

from __future__ import annotations

import pytest

from memspine.services import _retry
from memspine.services._retry import retry_transient


class APIConnectionError(Exception):  # same name as litellm's mapped class
    pass


async def test_transient_failure_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(_retry.asyncio, "sleep", no_sleep)
    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise APIConnectionError("Server disconnected")
        return "ok"

    assert await retry_transient(flaky, what="t") == "ok"
    assert calls == 3


async def test_non_transient_failure_is_not_retried() -> None:
    calls = 0

    async def bad() -> str:
        nonlocal calls
        calls += 1
        raise ValueError("bad request")

    with pytest.raises(ValueError):
        await retry_transient(bad, what="t")
    assert calls == 1


async def test_gives_up_after_the_last_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(_retry.asyncio, "sleep", no_sleep)

    async def down() -> str:
        raise APIConnectionError("refused")

    with pytest.raises(APIConnectionError):
        await retry_transient(down, what="t", attempts=3)


async def test_expired_credentials_are_not_retried() -> None:
    # litellm wraps an expired-token 403 in APIConnectionError; retrying only burns time.
    calls = 0

    async def expired() -> str:
        nonlocal calls
        calls += 1
        raise APIConnectionError(
            "403 Forbidden: The security token included in the request is expired"
        )

    with pytest.raises(APIConnectionError):
        await retry_transient(expired, what="t")
    assert calls == 1


class RateLimitError(Exception):  # same name as litellm's mapped class
    pass


@pytest.mark.parametrize(
    "message",
    [
        "insufficient_quota: you exceeded your current quota",
        "Error code: 401 - invalid_api_key",
        "Incorrect API key provided",
        "model not found",
        "The model `x` does not exist",
    ],
)
async def test_permanent_provider_failures_are_not_retried(message: str) -> None:
    """B-6: quota, auth and missing-model errors from non-AWS providers."""
    calls = 0

    async def permanent() -> str:
        nonlocal calls
        calls += 1
        raise RateLimitError(message)

    with pytest.raises(RateLimitError):
        await retry_transient(permanent, what="t")
    assert calls == 1


async def test_total_deadline_caps_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """B-6: a retry whose backoff would pass ``max_total_s`` is not taken."""
    slept: list[float] = []

    async def record_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(_retry.asyncio, "sleep", record_sleep)
    monkeypatch.setattr(_retry, "_clock", lambda: sum(slept))  # time advances only by sleeping
    calls = 0

    async def down() -> str:
        nonlocal calls
        calls += 1
        raise APIConnectionError("Server disconnected")

    with pytest.raises(APIConnectionError):
        await retry_transient(down, what="t", attempts=5, base_delay=1.0, max_total_s=2.5)
    assert slept == [1.0]  # the 2 s second backoff would end past the 2.5 s budget
    assert calls == 2
