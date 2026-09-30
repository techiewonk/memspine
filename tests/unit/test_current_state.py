"""C4': deterministic current-state view and explicit retraction (FORK-A6)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from memspine import Engine
from memspine.core.records import RecordStatus, SourceInfo


def _engine(**extra: Any) -> Engine:
    return Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"semantic": {"enabled": True}},
        **extra,
    )


async def test_current_state_view_shows_current_and_history() -> None:
    eng = _engine(read={"current_state_view": True})
    await eng.start()
    try:
        await eng.write(
            "Caroline lives in Boston",
            namespace="a",
            entity="caroline",
            attribute="city",
            valid_from=datetime(2023, 1, 5, tzinfo=UTC),
        )
        await eng.write(
            "Caroline lives in Seattle",
            namespace="a",
            entity="caroline",
            attribute="city",
            valid_from=datetime(2023, 6, 1, tzinfo=UTC),
        )
        ctx = await eng.assemble("where does Caroline live", namespace="a")
        [line] = [r.content for r in ctx.records if "Seattle" in r.content]
        assert line.startswith("CURRENT (since 2023-06-01): Caroline lives in Seattle")
        assert "HISTORY (superseded): 2023-01-05 to 2023-06-01: Caroline lives in Boston" in line
    finally:
        await eng.stop()


async def test_view_off_by_default() -> None:
    eng = _engine()
    await eng.start()
    try:
        await eng.write(
            "Caroline lives in Boston", namespace="a", entity="caroline", attribute="city"
        )
        ctx = await eng.assemble("where does Caroline live", namespace="a")
        assert all(not r.content.startswith("CURRENT") for r in ctx.records)
    finally:
        await eng.stop()


async def test_retract_ends_the_fact_with_no_successor() -> None:
    eng = _engine()
    await eng.start()
    try:
        fact = await eng.write(
            "Mel is allergic to peanuts",
            namespace="a",
            entity="mel",
            attribute="allergy",
            source=SourceInfo(role="user"),
        )
        await eng.retract(
            "mel",
            "allergy",
            namespace="a",
            reason="tested negative",
            source=SourceInfo(role="user"),
        )
        storage = eng._require_started()
        old = await storage.get_record(fact.record_id)
        assert old is not None and old.status is RecordStatus.ARCHIVED
        assert old.valid_to is not None and old.evolve_to is None  # ended, not replaced
        assert await storage.find_active_fact("a", "mel", "allergy") is None
    finally:
        await eng.stop()


async def test_low_trust_source_cannot_retract_an_operator_fact() -> None:
    eng = _engine()
    await eng.start()
    try:
        fact = await eng.write(
            "MFA is required for contractors",
            namespace="a",
            entity="policy",
            attribute="mfa",
            source=SourceInfo(role="operator"),
        )
        await eng.retract(
            "policy", "mfa", namespace="a", source=SourceInfo(role="tool", channel="web")
        )
        storage = eng._require_started()
        kept = await storage.get_record(fact.record_id)
        assert kept is not None and kept.status is RecordStatus.ACTIVATED  # trust gate held
    finally:
        await eng.stop()
