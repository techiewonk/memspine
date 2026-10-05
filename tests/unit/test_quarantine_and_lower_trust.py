"""Public ``Engine.quarantine`` and the opt-in lower-trust contest rung."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from memspine import Engine
from memspine.core.policies.conflict import ConflictPolicy, ConflictVerdict
from memspine.core.records import MemoryRecord, RecordStatus, SourceInfo
from memspine.exceptions import ConflictError

T = datetime(2023, 5, 1, tzinfo=UTC)


def _rec(content: str, trust: float, *tags: str) -> MemoryRecord:
    rec = MemoryRecord(
        namespace="a",
        memory_type="semantic",
        content=content,
        entity="ana",
        attribute="city",
        valid_from=T,
        tags=list(tags),
    )
    return rec.model_copy(update={"trust": trust})


def test_lower_trust_write_contests_instead_of_superseding() -> None:
    benign, poison = _rec("Ana lives in Lyon", 0.7), _rec("Ana lives in Zorgville", 0.5)
    assert ConflictPolicy.bind({}).resolve(poison, benign) is ConflictVerdict.UPDATE
    on = ConflictPolicy.bind({"contest_lower_trust": True})
    assert on.resolve(poison, benign) is ConflictVerdict.CONTEST
    # nor may it retract the more-trusted fact
    retract = _rec("Ana no longer lives in Lyon", 0.5, "retract")
    assert on.resolve(retract, benign) is ConflictVerdict.CONTEST
    # equal or higher trust still updates; far lower trust is still rejected
    assert on.resolve(_rec("Ana lives in Nice", 0.7), benign) is ConflictVerdict.UPDATE
    assert on.resolve(_rec("Ana lives in Nice", 0.3), benign) is ConflictVerdict.NOOP


def _engine(**extra: object) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        read={"hybrid": False},
        **extra,  # type: ignore[arg-type]
    )


async def test_quarantine_hides_record_and_is_scoped() -> None:
    eng = _engine()
    await eng.start()
    try:
        rec = await eng.write("the gateway cert rotates on Fridays", namespace="a")
        held = await eng.quarantine(rec.record_id, namespace="a")
        assert held.quarantined and held.status is RecordStatus.QUARANTINED
        hits = await eng.search("gateway cert", namespace="a", top_k=5)
        assert rec.record_id not in {r.record_id for r, _ in hits}
        again = await eng.quarantine(rec.record_id, namespace="a")  # idempotent
        assert again.quarantined
        with pytest.raises(ConflictError):
            await eng.quarantine(rec.record_id, namespace="b")
        with pytest.raises(ConflictError):
            await eng.quarantine("no-such-id", namespace="a")
    finally:
        await eng.stop()


async def test_quarantine_drains_descendants_under_live_reevaluation() -> None:
    eng = _engine(
        integrity={
            "enabled": True,
            "kappa": 0.9,
            "admission_threshold": 0.2,
            "live_reevaluation": True,
        }
    )
    await eng.start()
    try:
        seed = await eng.write("gateway cert rotates on Fridays", namespace="a")
        child = await eng.write(
            "summary: gateway cert rotates on Fridays",
            namespace="a",
            source=SourceInfo(role="assistant", channel="internal"),
            actor="assistant",
            derived_from=[seed.record_id],
        )
        assert await eng.effective_trust(child.record_id) > 0
        await eng.quarantine(seed.record_id, namespace="a")
        assert await eng.effective_trust(child.record_id) == 0
    finally:
        await eng.stop()


async def test_attribute_less_fact_is_not_corroborated_by_same_entity() -> None:
    """B-1: two unrelated trusted writes about one person do not promote a held
    event fact that has no attribute (``None == None`` is not a key match)."""
    eng = _engine()
    await eng.start()
    try:
        held = await eng.write(
            "Ana visited Oslo in May",
            namespace="a",
            source=SourceInfo(role="tool", channel="internal"),
            actor="tool",
            entity="ana",
        )
        await eng.quarantine(held.record_id, namespace="a")
        for text in ("Ana likes tea", "Ana has a cat"):
            await eng.write(text, namespace="a", entity="ana")
        stored = await eng._require_started().get_record(held.record_id)
        assert stored is not None
        assert stored.quarantined and stored.status is RecordStatus.QUARANTINED
        assert stored.corroborations == 0
    finally:
        await eng.stop()


def test_attribute_less_records_do_not_conflict() -> None:
    """B-1: ConflictPolicy treats attribute-less records as independent."""
    a = _rec("Ana visited Oslo", 0.7).model_copy(update={"attribute": None})
    b = _rec("Ana adopted a cat", 0.7).model_copy(update={"attribute": None})
    assert ConflictPolicy.bind({}).resolve(b, a) is ConflictVerdict.ADD


async def test_quarantine_refuses_a_forgotten_record() -> None:
    """B-2: a DELETED record cannot be moved to QUARANTINED (and so revived)."""
    eng = _engine()
    await eng.start()
    try:
        rec = await eng.write("the gateway cert rotates on Fridays", namespace="a")
        await eng.forget(rec.record_id, namespace="a")
        with pytest.raises(ConflictError, match="no such record"):
            await eng.quarantine(rec.record_id, namespace="a")
        stored = await eng._require_started().get_record(rec.record_id)
        assert stored is not None and stored.status is RecordStatus.DELETED
        assert not stored.quarantined
    finally:
        await eng.stop()


async def test_quarantine_refuses_a_taint_archived_record() -> None:
    """B-2: a rolled-back poison stays archived."""
    eng = _engine()
    await eng.start()
    try:
        rec = await eng.write("the gateway cert rotates on Fridays", namespace="a")
        await eng.rollback_taint(rec.record_id, namespace="a")
        with pytest.raises(ConflictError, match="no such record"):
            await eng.quarantine(rec.record_id, namespace="a")
        stored = await eng._require_started().get_record(rec.record_id)
        assert stored is not None and stored.status is RecordStatus.ARCHIVED
        assert "taint_archived" in stored.tags
    finally:
        await eng.stop()
