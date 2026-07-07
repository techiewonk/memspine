"""Consolidation policy (M2): triggers + deterministic extractive summaries."""

from __future__ import annotations

from datetime import UTC, datetime

from memspine.core.policies.consolidation import (
    ConsolidationPolicy,
    ConsolidationTrigger,
    extractive_summary,
)
from memspine.core.records import MemoryRecord, SourceInfo

NOW = datetime(2026, 7, 7, tzinfo=UTC)


def rec(content: str, channel: str = "internal") -> MemoryRecord:
    return MemoryRecord(
        namespace="agent/a",
        memory_type="episodic",
        content=content,
        source=SourceInfo(channel=channel),
    )


def test_default_triggers() -> None:
    policy = ConsolidationPolicy.bind()
    assert policy.should_trigger(ConsolidationTrigger.SLEEP_CYCLE)
    assert policy.should_trigger(ConsolidationTrigger.SESSION_END)
    assert not policy.should_trigger(ConsolidationTrigger.HEAT, heat=10)  # not in defaults


def test_heat_trigger_respects_threshold() -> None:
    policy = ConsolidationPolicy.bind({"triggers": ["heat"], "heat_threshold": 5})
    assert not policy.should_trigger(ConsolidationTrigger.HEAT, heat=4)
    assert policy.should_trigger(ConsolidationTrigger.HEAT, heat=5)
    assert not policy.should_trigger(ConsolidationTrigger.SLEEP_CYCLE)  # replaced


def test_worth_summarizing_ignores_prior_summaries() -> None:
    policy = ConsolidationPolicy.bind({"min_session_records": 3})
    members = [rec("a"), rec("b"), rec("old summary", channel="consolidation")]
    assert not policy.worth_summarizing(members)  # only 2 real members
    assert policy.worth_summarizing([rec("a"), rec("b"), rec("c")])


def test_extractive_summary_is_deterministic_first_sentences() -> None:
    contents = [
        "Alice moved to Paris. She loves it there.",
        "Bob called about the invoice! It was overdue.",
        "",
        "Note without terminator",
    ]
    summary = extractive_summary(contents, max_chars=600)
    assert summary == (
        "Alice moved to Paris. Bob called about the invoice! Note without terminator"
    )
    assert extractive_summary(contents, max_chars=600) == summary  # deterministic (N6)


def test_extractive_summary_respects_budget() -> None:
    summary = extractive_summary(["word " * 500], max_chars=50)
    assert len(summary) <= 50
