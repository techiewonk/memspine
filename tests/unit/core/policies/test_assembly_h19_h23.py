"""H19 latest-dated slots and H23 near-duplicate removal in assembly."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from memspine.core.policies.assembly import AssemblyPolicy
from memspine.core.records import MemoryRecord

T0 = datetime(2023, 1, 1, tzinfo=UTC)


def _rec(content: str, days: int) -> MemoryRecord:
    return MemoryRecord(
        namespace="a", memory_type="episodic", content=content, valid_from=T0 + timedelta(days=days)
    )


def test_latest_slots_keep_the_newest_record_in() -> None:
    old = [(_rec(f"old note {i} about running shoes", i), 0.9 - i * 0.01) for i in range(5)]
    newest = (_rec("new note: switched to trail running shoes", 100), 0.2)
    scored = [*old, newest]
    plain = AssemblyPolicy.bind({}).assemble(scored, budget_tokens=40)
    slotted = AssemblyPolicy.bind({"latest_slots": 1}).assemble(scored, budget_tokens=40)
    assert newest[0] not in plain.records
    assert newest[0] in slotted.records


def test_dedupe_drops_near_duplicates() -> None:
    a = (_rec("Melanie went to the beach with her kids on Sunday", 1), 0.9)
    b = (_rec("Melanie went to the beach with her kids on Sunday!", 2), 0.85)
    c = (_rec("Caroline painted a sunrise", 3), 0.5)
    on = AssemblyPolicy.bind({"dedupe_jaccard": 0.8, "mmr_lambda": 1.0}).assemble(
        [a, b, c], budget_tokens=500
    )
    off = AssemblyPolicy.bind({"mmr_lambda": 1.0}).assemble([a, b, c], budget_tokens=500)
    assert len(off.records) == 3 and len(on.records) == 2
