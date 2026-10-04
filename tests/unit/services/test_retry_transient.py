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
