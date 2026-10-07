"""W16 (plan v3.2): sensitive-topic tags and the extended PII additions."""

from __future__ import annotations

import pytest

from memspine import Engine
from memspine.config.schema import FirewallConfig
from memspine.core.records import PiiTier
from memspine.core.redaction import redact
from memspine.core.sensitive import sensitive_topics


@pytest.mark.parametrize(
    ("text", "topic"),
    [
        ("I was diagnosed with diabetes last spring", "health"),
        ("My church choir meets on Thursdays", "religion"),
        ("I came out to my parents last year", "sexual_orientation"),
        ("I'm a union member at the plant", "political"),
        ("My immigration status is still pending", "ethnicity"),
        ("I got a parking ticket outside the office", "legal"),
        ("We are in debt after the move", "financial"),
    ],
)
def test_topics(text: str, topic: str) -> None:
    assert topic in sensitive_topics(text)


@pytest.mark.parametrize(
    "text",
    ["We went camping by the lake", "The church bells were pretty", "I love jazz"],
)
def test_ordinary_text_has_no_topic(text: str) -> None:
    assert sensitive_topics(text) == []


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("I live at 47 Kelburn Parade, Wellington", "47 Kelburn Parade"),
        ("Ship it to 4827 NE Tillamook St, Apt 3B, Portland", "Apt 3B"),
        ("The file is at /Users/emily_thompson/Projects/main.py", "emily_thompson"),
    ],
)
def test_extended_pii_streets_units_and_home_paths(text: str, secret: str) -> None:
    masked, _ = redact(text, secrets=False, pii=True, pii_extended=True)
    assert secret not in masked


def test_sensitive_topics_defaults_off() -> None:
    assert FirewallConfig().sensitive_topics is False


async def test_write_door_tags_topics_and_raises_the_tier() -> None:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        firewall={"sensitive_topics": True},
    )
    await eng.start()
    try:
        rec = await eng.write(
            "I was diagnosed with depression in May", namespace="a", memory_type="episodic"
        )
        plain = await eng.write("We went hiking in May", namespace="a", memory_type="episodic")
    finally:
        await eng.stop()
    assert "sensitive:health" in rec.tags
    assert rec.pii_tier is PiiTier.HIGH
    assert not any(t.startswith("sensitive:") for t in plain.tags)
