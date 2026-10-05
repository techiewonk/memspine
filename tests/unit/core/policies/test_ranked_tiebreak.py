"""Equal scores must order by content, not by random record ids or set order."""

from __future__ import annotations

from datetime import UTC, datetime

from memspine.core.policies.assembly import ranked
from memspine.core.records import MemoryRecord

T = datetime(2023, 5, 1, tzinfo=UTC)


def _rec(content: str) -> MemoryRecord:
    return MemoryRecord(namespace="a", memory_type="episodic", content=content, valid_from=T)


def test_ties_order_the_same_whatever_the_input_order() -> None:
    a, b, c = _rec("alpha"), _rec("bravo"), _rec("charlie")
    one = [r.content for r, _ in ranked([(a, 0.5), (b, 0.5), (c, 0.9)])]
    two = [r.content for r, _ in ranked([(b, 0.5), (c, 0.9), (a, 0.5)])]
    assert one == two
    assert one[0] == "charlie"
