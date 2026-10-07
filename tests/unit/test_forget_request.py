"""G25 (plan v3.2): "forget that" requests in dialogue are detected, never auto-deleted."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.config import constants
from memspine.core.forget_request import forget_target, is_forget_request


@pytest.mark.parametrize(
    "text",
    [
        "Please forget what I said about my ex.",
        "Don't remember my home address.",
        "Delete that from your memory, please.",
        "Stop using my old job title.",
        "Can you forget my phone number?",
    ],
)
def test_forget_requests_are_found(text: str) -> None:
    assert is_forget_request(text)


@pytest.mark.parametrize(
    "text",
    [
        "I always forget my keys.",
        "Remember the milk!",
        "I deleted the app last week.",
        "We should keep in touch.",
        "I don't remember where I put the charger.",
    ],
)
def test_ordinary_sentences_are_not_requests(text: str) -> None:
    assert not is_forget_request(text)


def test_forget_target_keeps_what_to_forget() -> None:
    assert forget_target("Please forget my old home address.") == "my old home address"


async def _engine(detector: bool) -> Engine:
    policies: dict[str, Any] = {"forget_detector": True} if detector else {}
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True, "policies": policies}},
        read={"hybrid": False},
    )
    await eng.start()
    return eng


async def _talk(eng: Engine) -> None:
    t0 = datetime(2023, 5, 8, 13, 0, tzinfo=UTC)
    texts = [
        "My home address is 12 Elm Road in Leeds",
        "I like jazz on Sundays",
        "Please forget my home address",
    ]
    msgs = [
        {"role": "user", "content": c, "timestamp": (t0 + timedelta(minutes=i)).isoformat()}
        for i, c in enumerate(texts)
    ]
    await eng.write_messages(msgs, namespace="a", session_id="s1", group_id="s1")


async def test_detector_tags_and_lists_candidates_without_deleting() -> None:
    eng = await _engine(detector=True)
    try:
        await _talk(eng)
        [request] = await eng.forget_requests("a")
        records = {r.record_id: r for r in await eng._require_started().list_records("a")}
        assert constants.FORGET_REQUEST_TAG in records[str(request["request_id"])].tags
        candidates = [records[c].content for c in request["candidates"]]  # type: ignore[union-attr]
        assert candidates[0] == "My home address is 12 Elm Road in Leeds"
        assert len(records) == 3  # nothing deleted
    finally:
        await eng.stop()


async def test_detector_off_tags_nothing() -> None:
    eng = await _engine(detector=False)
    try:
        await _talk(eng)
        assert await eng.forget_requests("a") == []
    finally:
        await eng.stop()
