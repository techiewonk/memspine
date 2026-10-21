"""MF-13 profile-card read contract, backed by the real event log and hash embedder."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from memspine import Engine
from memspine.config.constants import PROFILE_CARD_MARKER
from memspine.derive.pipeline.stages.profile_card import card_to_dict
from memspine.derive.profile import ProfileCard, ProfileEntry, SupportRef
from memspine.store.events import EventKind, MemoryEvent

NS = "profile-card-read"
NOW = datetime.now(UTC)


def _engine(**read):
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"provider": "sqlite", "settings": {"path": ":memory:", "busy_timeout_s": 5.0}},
        embedding={"provider": "hash", "settings": {}},
        memories={"episodic": {"enabled": True}},
        offline={"session_close_v2": True, "profile_card_v2": True},
        read={"hybrid": False, "record_access": False, "profile_card_header": True, **read},
    )


async def _marker(
    eng: Engine,
    support_ids: tuple[str, ...],
    *,
    confirmed: datetime | None = None,
    key: str = "coffee",
    value: str = "likes cardamom coffee",
) -> None:
    at = confirmed or NOW
    entry = ProfileEntry(
        section="preferences",
        key=key,
        value=value,
        support=tuple(SupportRef(support_id, 0.9) for support_id in support_ids),
        restated=1,
        first_confirmed=at,
        last_confirmed=at,
    )
    card = ProfileCard(tenant=NS, owner=NS, entries=(entry,))
    await eng._require_started().append_event(
        MemoryEvent(
            kind=EventKind.MARKER,
            namespace=NS,
            payload={"marker": PROFILE_CARD_MARKER, "card": card_to_dict(card), "via": "test"},
        )
    )


async def _read(eng: Engine) -> str:
    """Flatten the public assembled result so assertions cover the read API path."""
    result = await eng.read(
        "What does Noor like?",
        namespace=NS,
        mode="retrieve",
        top_k=5,
        budget_tokens=200,
    )
    return "\n".join(record.content for record in result.context.records)


@pytest.mark.asyncio
async def test_marker_backed_card_renders_header_with_only_live_support() -> None:
    eng = _engine()
    await eng.start()
    try:
        source = await eng.write("Noor likes cardamom coffee", namespace=NS, memory_type="episodic")
        await _marker(eng, (source.record_id,))

        rendered = await _read(eng)

        assert "PROFILE NOTES (profile card:" in rendered
        assert "likes cardamom coffee" in rendered
        assert source.record_id in rendered
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_expired_card_entry_is_omitted() -> None:
    eng = _engine(profile_card_max_age_days=2)
    eng._clock = lambda: NOW
    await eng.start()
    try:
        source = await eng.write("Noor likes cardamom coffee", namespace=NS, memory_type="episodic")
        await _marker(eng, (source.record_id,), confirmed=NOW - timedelta(days=4))

        assert "PROFILE NOTES (profile card:" not in await _read(eng)
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_quarantined_support_does_not_keep_card_entry_visible() -> None:
    eng = _engine()
    await eng.start()
    try:
        source = await eng.write("Noor likes cardamom coffee", namespace=NS, memory_type="episodic")
        await _marker(eng, (source.record_id,))
        await eng.quarantine(source.record_id, namespace=NS)

        assert "likes cardamom coffee" not in await _read(eng)
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_forgotten_support_does_not_keep_card_entry_visible() -> None:
    eng = _engine()
    await eng.start()
    try:
        source = await eng.write("Noor likes cardamom coffee", namespace=NS, memory_type="episodic")
        await _marker(eng, (source.record_id,))
        await eng.forget(source.record_id, namespace=NS, hard=True)

        assert "likes cardamom coffee" not in await _read(eng)
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_marker_entry_with_no_support_does_not_render() -> None:
    eng = _engine()
    await eng.start()
    try:
        await _marker(eng, ())

        assert "PROFILE NOTES (profile card:" not in await _read(eng)
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_partly_hidden_support_suppresses_unattributed_value() -> None:
    """One hidden cited source suppresses the whole unattributed marker value."""
    eng = _engine()
    await eng.start()
    try:
        live = await eng.write("Noor has a pet turtle", namespace=NS, memory_type="episodic")
        hidden = await eng.write(
            "Noor's private medical condition is migraine", namespace=NS, memory_type="episodic"
        )
        private_value = "likes cardamom coffee because of her private migraine condition"
        await _marker(
            eng,
            (live.record_id, hidden.record_id),
            key="private medical preference",
            value=private_value,
        )
        # The marker value can contain information from both sources, but has no attribution
        # identifying which source supports which part of its text.
        await eng.quarantine(hidden.record_id, namespace=NS)

        rendered = await _read(eng)

        assert "PROFILE NOTES (profile card:" not in rendered
        assert private_value not in rendered
        assert hidden.record_id not in rendered
    finally:
        await eng.stop()
