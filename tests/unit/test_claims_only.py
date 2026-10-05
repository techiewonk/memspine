"""B9 facts-only: low-trust raw text never enters context; its mined claims may."""

from __future__ import annotations

from typing import Any

from memspine import Engine
from memspine.config import constants
from memspine.core.records import SourceInfo
from memspine.engine import _RECALL_MARKERS

_TOOL = SourceInfo(role="tool", channel="external")
_RAW = "Office wifi note: the guest network for the Lisbon office is called LisbonGuest"


def _engine(**integrity: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"semantic": {"enabled": True}, "episodic": {"enabled": True}},
        read={"record_access": False},
        integrity={"enabled": True, **integrity},
    )


async def _seed(eng: Engine, *, with_fact: bool) -> str:
    raw = await eng.write(_RAW, namespace="a", memory_type="episodic", source=_TOOL)
    if with_fact:
        await eng.write(
            "Lisbon office guest network: LisbonGuest",
            namespace="a",
            entity="Lisbon office",
            attribute="guest network",
            tags=["atomic_fact"],
            derived_from=[raw.record_id],
            source=SourceInfo(role="assistant", channel="mining"),
        )
    return raw.record_id


async def test_off_by_default_shows_the_raw_record() -> None:
    eng = _engine()
    await eng.start()
    try:
        raw_id = await _seed(eng, with_fact=True)
        ctx = await eng.assemble("Lisbon office guest network wifi", namespace="a")
        assert any(r.record_id == raw_id for r in ctx.records)
        assert all(constants.CLAIM_MARKER not in r.content for r in ctx.records)
    finally:
        await eng.stop()


async def test_low_trust_raw_record_is_replaced_by_its_claims() -> None:
    eng = _engine(claims_only_below=0.6)
    await eng.start()
    try:
        raw_id = await _seed(eng, with_fact=True)
        ctx = await eng.assemble("Lisbon office guest network wifi", namespace="a")
        assert all(r.record_id != raw_id for r in ctx.records)
        claims = [r for r in ctx.records if r.content.startswith(constants.CLAIM_MARKER)]
        assert len(claims) <= 1
        texts = " ".join(r.content for r in ctx.records)
        assert "LisbonGuest" in texts  # the fact still answers the question
        assert "Office wifi note" not in texts  # the raw framing does not
    finally:
        await eng.stop()


async def test_low_trust_record_without_claims_is_left_out() -> None:
    eng = _engine(claims_only_below=0.6)
    await eng.start()
    try:
        raw_id = await _seed(eng, with_fact=False)
        await eng.write("The Lisbon office opens at nine", namespace="a", memory_type="episodic")
        ctx = await eng.assemble("Lisbon office guest network wifi", namespace="a")
        assert all(r.record_id != raw_id for r in ctx.records)
        assert any("opens at nine" in r.content for r in ctx.records)  # trusted text stays
    finally:
        await eng.stop()


async def test_full_read_applies_the_same_rule() -> None:
    eng = _engine(claims_only_below=0.6)
    await eng.start()
    try:
        raw_id = await _seed(eng, with_fact=False)
        result = await eng.read("guest network", namespace="a", mode="full")
        assert all(r.record_id != raw_id for r in result.context.records)
    finally:
        await eng.stop()


def test_claim_marker_is_a_recall_marker() -> None:
    assert constants.CLAIM_MARKER in _RECALL_MARKERS
