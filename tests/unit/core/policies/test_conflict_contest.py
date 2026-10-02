"""H9: CONTEST verdict and the overlap rule (pure ladder logic)."""

from __future__ import annotations

from datetime import UTC, datetime

from memspine.core.policies.conflict import ConflictPolicy, ConflictVerdict
from memspine.core.records import MemoryRecord

T = datetime(2023, 5, 1, tzinfo=UTC)


def _rec(
    content: str, trust: float = 0.7, when: datetime = T, valid_to: datetime | None = None
) -> MemoryRecord:
    rec = MemoryRecord(
        namespace="a",
        memory_type="semantic",
        content=content,
        entity="ana",
        attribute="city",
        valid_from=when,
        valid_to=valid_to,
    )
    return rec.model_copy(update={"trust": trust})


def test_tie_is_contested_only_when_enabled() -> None:
    old, new = _rec("Ana lives in Lyon"), _rec("Ana lives in Nice")
    assert ConflictPolicy.bind({}).resolve(new, old) is ConflictVerdict.UPDATE
    on = ConflictPolicy.bind({"contest_ties": True})
    assert on.resolve(new, old) is ConflictVerdict.CONTEST


def test_time_or_trust_still_decides_under_contest() -> None:
    on = ConflictPolicy.bind({"contest_ties": True})
    later = _rec("Ana lives in Nice", when=datetime(2023, 6, 1, tzinfo=UTC))
    assert on.resolve(later, _rec("Ana lives in Lyon")) is ConflictVerdict.UPDATE
    weaker = _rec("Ana lives in Nice", trust=0.5)
    assert on.resolve(weaker, _rec("Ana lives in Lyon", trust=0.7)) is ConflictVerdict.UPDATE


def test_overlap_rule_closed_past_interval_never_displaces() -> None:
    current = _rec("Ana lives in Lyon", when=datetime(2023, 6, 1, tzinfo=UTC))
    past = _rec(
        "Ana lived in Paris",
        when=datetime(2022, 1, 1, tzinfo=UTC),
        valid_to=datetime(2023, 1, 1, tzinfo=UTC),
    )
    assert ConflictPolicy.bind({}).resolve(past, current) is ConflictVerdict.ADD
