"""#44: ``firewall.pii`` at the write door, across text fields, and ``pii_default_tier``."""

from __future__ import annotations

from typing import Any

from memspine import Engine
from memspine.core.records import PiiTier


def _engine(**extra: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        **extra,
    )


async def test_pii_off_is_the_default() -> None:
    eng = _engine()
    await eng.start()
    try:
        rec = await eng.write("call me on +1 415-555-0123", namespace="a")
        assert "415-555-0123" in rec.content and rec.pii_tier is PiiTier.NONE
    finally:
        await eng.stop()


async def test_pii_redact_covers_every_text_field() -> None:
    eng = _engine(firewall={"pii": "redact"})
    await eng.start()
    try:
        rec = await eng.write(
            "my card is 4111 1111 1111 1111",
            namespace="a",
            entity="card 4111111111111111",
            attribute="ip 10.0.0.7",
            tags=["ssn:123-45-6789", "plain"],
        )
        assert "4111" not in rec.content and "[REDACTED:payment_card]" in rec.content
        assert rec.entity == "card [REDACTED:payment_card]"
        assert rec.attribute == "ip [REDACTED:ipv4]"
        assert rec.tags == ["ssn:[REDACTED:us_ssn]", "plain"]
        events = await eng._require_started().read_events()
        assert all("4111" not in str(e.payload) for e in events)
    finally:
        await eng.stop()


async def test_pii_tag_keeps_text_and_raises_tier() -> None:
    eng = _engine(firewall={"pii": "tag"})
    await eng.start()
    try:
        rec = await eng.write("reach me at 192.168.10.4 or +44 20 7946 0958", namespace="a")
        assert "192.168.10.4" in rec.content
        assert {"pii:ipv4", "pii:phone"} <= set(rec.tags)
        assert rec.pii_tier is PiiTier.HIGH
        clean = await eng.write("nothing personal here", namespace="a")
        assert clean.pii_tier is PiiTier.NONE and clean.tags == []
    finally:
        await eng.stop()


async def test_pii_default_tier_policy_is_applied() -> None:
    eng = _engine(
        memories={"semantic": {"enabled": True, "policies": {"pii_default_tier": "regulated"}}}
    )
    await eng.start()
    try:
        rec = await eng.write("account opened in March", namespace="a")
        assert rec.pii_tier is PiiTier.REGULATED
        explicit = await eng.write("low note", namespace="a", pii_tier=PiiTier.LOW)
        assert explicit.pii_tier is PiiTier.LOW  # a caller-set tier wins
    finally:
        await eng.stop()


async def test_write_messages_prewarm_matches_redacted_content() -> None:
    eng = _engine(firewall={"pii": "redact"})
    await eng.start()
    try:
        records = await eng.write_messages(
            [
                {"role": "user", "content": "my number is +1 415-555-0123"},
                {"role": "user", "content": "and my ssn is 123-45-6789"},
            ],
            namespace="a",
        )
        assert [r.content for r in records] == [
            "my number is [REDACTED:phone]",
            "and my ssn is [REDACTED:us_ssn]",
        ]
    finally:
        await eng.stop()
