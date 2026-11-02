"""F8 commitment (docs/BI01_CONTRACT_REWRITE_PLAN.md "Expanded synthetic fixtures (RM-54)";
role family 1, RM-50).

An agent commits to a report for a named beneficiary by an aware deadline, then explicitly
cancels. Commitment, actor and deadline stay separate; an intention or a first mention is
never a completion. No rule extracts commitments yet, so a scripted non-generative extractor
states the exact-span candidates; the compiler, registry, deposit, graph, rebuild and
erasure are the real path."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from scenario_harness import (
    ScriptedExtractor,
    assert_erased,
    assert_replay,
    binding,
    by_predicate,
    candidate,
    claims,
    edges,
    edges_of,
    envelope_evidence,
    msg,
    semantic_hits,
    send,
    settle,
    support,
    tick,
)
from scenario_harness import boot as _boot

from memspine.derive.non_generative_facts import FactStageResult, PendingClaimChange

COMMIT = "I will send the quarterly report to Kim by 2026-11-05T17:00+01:00."
CANCEL = "I cancel my quarterly report commitment."
QUERY = "Who committed to the quarterly report"


def _commit(r: Any, s: Any) -> list[Any]:
    return [
        candidate(
            r,
            s,
            predicate="committed_to",
            obj="quarterly report",
            object_entity_type="document",
            subject_type="agent",
            evidence=support(r, COMMIT),
            roles=[
                binding(1, "obligor", "agent-7", "entity", envelope_evidence(r), "agent"),
                binding(
                    2,
                    "action",
                    "send the quarterly report",
                    "text",
                    support(r, "send the quarterly report"),
                ),
                binding(3, "beneficiary", "Kim", "entity", support(r, "Kim"), "person"),
            ],
        )
    ]


def _cancel(r: Any, s: Any) -> FactStageResult:
    """The explicit cancellation as a pending change: the window resolves it to the one live
    commitment of the same sender it names (RM-54) and closes it with outcome declined."""
    return FactStageResult(
        pending=(
            PendingClaimChange(
                kind="cancellation",
                scope=s,
                owner="agent-7",
                evidence=support(r, CANCEL),
                old_value="quarterly report",
                outcome="declined",
            ),
        )
    )


def _extractor() -> ScriptedExtractor:
    ex = ScriptedExtractor()
    ex.on(COMMIT, _commit)
    ex.on(CANCEL, _cancel)
    return ex


def _agent(message_id: str, text: str, at: str) -> dict[str, Any]:
    return msg(message_id, "agent-7", text, at, role="assistant", kind="agent")


async def test_f8_commitment_is_separate_and_never_completed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f8.db", extractor=_extractor())
    try:
        cut = await tick()
        [commit_src] = await send(eng, [_agent("f8-1", COMMIT, "2026-10-20T09:00:00+01:00")])
        await send(
            eng, [msg("f8-2", "Kim", "Maybe I will read the report soon.", "2026-10-20T09:05Z")]
        )
        await settle(eng)
        [commit] = by_predicate(await claims(eng), "committed_to")
        assert (commit.subject, commit.principal, commit.object) == (
            "agent-7",
            "agent-7",
            "quarterly report",
        )
        # an intention is asserted and active, never fulfilled by being stated
        assert (commit.modality, commit.status, commit.outcome) == ("asserted", "active", "none")
        assert commit.role("obligor") == [("agent-7", "entity", "agent", ())]
        assert commit.role("beneficiary") == [("Kim", "entity", "person", ("Kim",))]
        assert commit.evidence == ((commit_src.record_id, 0, len(COMMIT), COMMIT),)
        # Kim's hedged first mention adds no claim
        assert [c.predicate for c in await claims(eng)] == ["committed_to"]
        mine = edges_of(await edges(eng), commit.record_id)
        assert {e.rel for e in mine} >= {"role:subject", "role:object", "committed_to"}

        assert await semantic_hits(eng, QUERY, recorded_before=cut) == []
        assert await semantic_hits(eng, QUERY) == ["agent-7 committed_to quarterly report"]
        await assert_replay(eng, ["agent-7", "quarterly report", "Kim"])
        await assert_erased(eng, commit_src, [commit.record_id], QUERY)
        assert await claims(eng) == []
    finally:
        await eng.stop()


async def test_f8_explicit_cancellation_closes_the_commitment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f8x.db", extractor=_extractor())
    try:
        [commit_src] = await send(eng, [_agent("x-1", COMMIT, "2026-10-20T09:00:00+01:00")])
        [cancel_src] = await send(eng, [_agent("x-2", CANCEL, "2026-10-21T09:00:00+01:00")])
        [commit] = by_predicate(await claims(eng), "committed_to")
        assert commit.outcome == "declined"
        assert (commit.modality, commit.status, commit.principal) == (
            "asserted",
            "active",
            "agent-7",
        )
        # the closed claim cites the cancellation and the commitment it closes, keeps its roles
        assert set(commit.sources) == {commit_src.record_id, cancel_src.record_id}
        assert commit.role("beneficiary") == [("Kim", "entity", "person", ("Kim",))]
        # the open commitment is kept as superseded history with no outcome
        [open_] = [c for c in await claims(eng, superseded=True) if c.status == "superseded"]
        assert (open_.outcome, open_.sources) == ("none", (commit_src.record_id,))
    finally:
        await eng.stop()
