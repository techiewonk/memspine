"""I3 (isolation review 2026-10-08): with every read leg and header on, two users' data
never cross. Each namespace holds a distinctive word; a read in one namespace must not
return the other's records through any leg, header or cache."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from memspine import Engine
from memspine.core.records import SourceInfo

T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)

#: Every opt-in read leg, header and expansion that reads the store directly.
ALL_ON = {
    "record_access": False,
    "temporal_leg": True,
    "temporal_leg_mentions": True,
    "temporal_infer_year": True,
    "temporal_soft": True,
    "metadata_leg": True,
    "subject_leg": True,
    "role_aware": True,
    "sentence_leg": True,
    "recency_leg": True,
    "entity_leg": True,
    "speaker_probe": True,
    "statement_probe": True,
    "cohesion_leg": True,
    "entity_expand_leg": True,
    "maxsim_leg": True,
    "session_digest": True,
    "recent_exchanges": 5,
    "facts_to_sources": True,
    "section_captions": True,
    "fusion": "minmax",
}


async def _seeded() -> Engine:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        read=ALL_ON,
    )
    await eng.start()
    for ns, word in (("alice", "zebracorn"), ("bob", "quokkaflux")):
        for i in range(4):
            turn = await eng.write(
                f"Ana: on 7 May 2023 we saw the {word} at Paris. It was lovely. Truly.",
                namespace=ns,
                memory_type="episodic",
                valid_from=T0 + timedelta(minutes=i),
            )
        await eng.write(
            f"Ana likes the {word}",
            namespace=ns,
            memory_type="semantic",
            tags=["atomic_fact"],
            source=SourceInfo(role="system", parents=[turn.record_id]),
            valid_from=T0,
        )
    return eng


@pytest.mark.parametrize("mode", ["replay", "retrieve", "compose", "full"])
async def test_no_leg_or_header_crosses_namespaces(mode: str) -> None:
    eng = await _seeded()
    try:
        for ns, mine, theirs in (
            ("alice", "zebracorn", "quokkaflux"),
            ("bob", "quokkaflux", "zebracorn"),
        ):
            result = await eng.read(
                f"What did Ana see in Paris on 7 May 2023? {theirs} {mine}",
                namespace=ns,
                mode=mode,
                budget_tokens=2000,
            )
            text = "\n".join(r.content for r in result.context.records)
            assert mine in text, (mode, ns)  # the read found the user's own memory
            assert theirs not in text, (mode, ns)
            assert all(r.namespace == ns for r in result.context.records if r.namespace), (mode, ns)
    finally:
        await eng.stop()


async def test_search_and_assemble_stay_in_namespace() -> None:
    eng = await _seeded()
    try:
        hits = await eng.search("quokkaflux zebracorn Paris", namespace="alice", top_k=20)
        assert hits and all(r.namespace == "alice" for r, _ in hits)
        ctx = await eng.assemble("quokkaflux", namespace="alice", budget_tokens=2000)
        assert "quokkaflux" not in "\n".join(r.content for r in ctx.records)
    finally:
        await eng.stop()
