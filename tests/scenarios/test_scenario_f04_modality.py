"""F4 modality (docs/BI01_CONTRACT_REWRITE_PLAN.md "RM-22 fixture specification";
docs/CLAUDE_CONTEXT_AND_TRACKER_2026-10-20.md section 6, F4 Modality).

An agent suggestion is not user intent, a refusal is not a permanent trait, a visit is not a
residence, and quoted or conditional statements are never asserted facts. A stated plan is
kept with its own modality and only tentative graph edges."""

from __future__ import annotations

from pathlib import Path

import pytest
from scenario_harness import (
    assert_erased,
    assert_replay,
    by_predicate,
    claims,
    edges,
    edges_of,
    msg,
    read_claims,
    semantic_hits,
    send,
    settle,
    tick,
    unresolved,
)
from scenario_harness import boot as _boot

QUERY = "Where does Asha live"


async def test_f4_suggestion_refusal_visit_keep_residence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f4.db")
    try:
        [home] = await send(eng, [msg("f4-0", "Asha", "I live in Porto.", "2026-09-01T09:00Z")])
        cut = await tick()
        suggestion, refusal, visit = await send(
            eng,
            [
                msg(
                    "f4-1",
                    "agent-7",
                    "You could move to Lisbon. I live in Lisbon myself.",
                    "2026-10-01T09:00Z",
                    role="assistant",
                    kind="agent",
                ),
                msg("f4-2", "Asha", "No, I am not moving.", "2026-10-01T09:01Z"),
                msg("f4-3", "Asha", "I visited Lisbon last weekend.", "2026-10-01T09:02Z"),
            ],
        )
        await settle(eng)
        found = await claims(eng)
        # residence unchanged; no move plan, no never-moving trait, no visit-as-residence:
        # the visit is its own event claim (ADR-088 amendment 2026-11-02)
        assert [(c.subject, c.object) for c in by_predicate(found, "resides_in")] == [
            ("Asha", "Porto")
        ]
        assert [c.predicate for c in found] == ["resides_in", "visited"]
        # the agent's text is not a user's, never an asserted claim of anyone
        [note] = [u for u in unresolved(eng) if u.source.record_id == suggestion.record_id]
        assert note.reason == "source_not_user" and note.asserted is False
        assert refusal.record_id not in {c.sources[0] for c in found}
        assert [c.predicate for c in found if visit.record_id in c.sources] == ["visited"]

        # the read finds the one residence and, separately, the visit event
        assert sorted(await read_claims(eng, QUERY)) == [
            "Asha resides_in Porto",
            "Asha visited Lisbon",
        ]
        assert await semantic_hits(eng, QUERY, recorded_before=cut) == ["Asha resides_in Porto"]
        await assert_replay(eng, ["Asha", "Porto", "Lisbon"])
        await assert_erased(eng, suggestion, [], QUERY)
        await assert_erased(eng, home, [found[0].record_id], QUERY)
        assert [c.predicate for c in await claims(eng)] == ["visited"]
        await assert_erased(eng, visit, [found[1].record_id], QUERY)
        assert await claims(eng) == []
    finally:
        await eng.stop()


async def test_f4_quoted_conditional_and_reported_are_not_asserted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f4q.db")
    try:
        await send(
            eng,
            [
                msg(
                    "q-1",
                    "Asha",
                    'My friend wrote "I live in Lisbon" on a postcard.',
                    "2026-10-01T09:00Z",
                ),
                msg(
                    "q-2",
                    "Asha",
                    "If I live in Lisbon next year, I will cycle.",
                    "2026-10-01T09:01Z",
                ),
                msg("q-3", "Asha", "Maybe I live in Lisbon by then.", "2026-10-01T09:02Z"),
                msg("q-4", "Asha", "Do I live in Lisbon?", "2026-10-01T09:03Z"),
            ],
        )
        await settle(eng)
        assert await claims(eng) == []
        assert await edges(eng) == []
    finally:
        await eng.stop()


async def test_f4_plan_keeps_planned_modality_and_tentative_edges(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f4p.db")
    try:
        await send(eng, [msg("p-1", "Asha", "I'm planning to visit Lisbon.", "2026-10-01T09:00Z")])
        await settle(eng)
        [plan] = await claims(eng)
        assert (plan.predicate, plan.object) == ("has_goal", "visit Lisbon")
        assert (plan.modality, plan.status, plan.outcome) == ("planned", "active", "none")
        mine = edges_of(await edges(eng), plan.record_id)
        # ADR-080 qualifier vocabulary: a planned claim is held as ``intended``; tentative
        # edges are excluded from default reads
        assert mine and all(e.tentative and e.modality == "intended" for e in mine)
        assert by_predicate(await claims(eng), "resides_in") == []
    finally:
        await eng.stop()


async def test_f4_visit_is_a_separate_event_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f4v.db")
    try:
        await send(eng, [msg("v-1", "Asha", "I visited Lisbon last weekend.", "2026-10-01T09:02Z")])
        [visit] = by_predicate(await claims(eng), "visited")
        assert (visit.object, visit.modality) == ("Lisbon", "asserted")
        assert visit.subject == visit.principal == "Asha"
        assert by_predicate(await claims(eng), "resides_in") == []
    finally:
        await eng.stop()
