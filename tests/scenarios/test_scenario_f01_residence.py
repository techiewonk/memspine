"""F1 residence (docs/BI01_CONTRACT_REWRITE_PLAN.md "RM-22 fixture specification";
docs/CLAUDE_CONTEXT_AND_TRACKER_2026-10-20.md section 6, F1 Move).

An authenticated sender states a prior residence, then a move. The rules-only profile keeps
both residences (multiple homes are allowed, nothing is silently replaced), binds subject and
object to entities, and invents no exact move day. Visits, work places and other homes never
become a residence; a source without a timezone keeps its time unresolved."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from scenario_harness import (
    assert_erased,
    assert_replay,
    by_predicate,
    claims,
    edges,
    edges_of,
    entity_ids,
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
PORTO = "I live in Porto."
MOVE = "I moved to Lisbon last month."


async def test_f1_residence_history_known_time_replay_and_erasure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f1.db")
    try:
        [porto_src] = await send(eng, [msg("f1-1", "Asha", PORTO, "2026-08-01T10:00:00+05:30")])
        cut = await tick()
        [move_src] = await send(eng, [msg("f1-2", "Asha", MOVE, "2026-10-01T10:00:00+05:30")])
        await settle(eng)

        found = await claims(eng)
        porto, lisbon = by_predicate(found, "resides_in")
        # Claims: asserted, active, no outcome; the sender is subject and assertion owner
        for claim, src, quote in ((porto, porto_src, PORTO), (lisbon, move_src, MOVE)):
            assert (claim.modality, claim.status, claim.outcome) == ("asserted", "active", "none")
            assert claim.subject == claim.principal == "Asha" and claim.producer == "rules"
            assert claim.sources == (src.record_id,)
            assert claim.evidence == ((src.record_id, 0, len(quote), quote),)
        assert (porto.object, lisbon.object) == ("Porto", "Lisbon")
        assert move_src.content == MOVE  # the raw source is never rewritten
        # No exact move day invented: the event time stays unresolved
        assert lisbon.time_precision == "unknown" and not lisbon.event_resolved
        assert lisbon.valid_from == datetime(2026, 10, 1, 4, 30, tzinfo=UTC)  # source clock

        # Graph: subject and object bind to entities; one projection edge per residence
        graph = await edges(eng)
        [asha] = await entity_ids(eng, "Asha")
        [porto_id] = await entity_ids(eng, "Porto")
        [lisbon_id] = await entity_ids(eng, "Lisbon")
        projections = sorted((e.src, e.dst) for e in graph if e.rel == "resides_in")
        assert projections == sorted([(asha, porto_id), (asha, lisbon_id)])
        for claim in (porto, lisbon):
            mine = edges_of(graph, claim.record_id)
            assert {e.rel for e in mine} >= {"role:subject", "role:object", "resides_in"}
            assert {(e.claim_state, e.modality, e.polarity, e.tentative) for e in mine} == {
                ("current", "asserted", "positive", False)
            }

        # Reads: both homes now; known-time (before the move was recorded) and as-of
        assert sorted(await semantic_hits(eng, QUERY)) == [
            "Asha resides_in Lisbon",
            "Asha resides_in Porto",
        ]
        assert await semantic_hits(eng, QUERY, recorded_before=cut) == ["Asha resides_in Porto"]
        assert await read_claims(eng, QUERY, recorded_before=cut) == ["Asha resides_in Porto"]
        september = datetime(2026, 9, 1, tzinfo=UTC)
        assert await read_claims(eng, QUERY, as_of=september) == ["Asha resides_in Porto"]

        await assert_replay(eng, ["Asha", "Porto", "Lisbon"])

        # Erasure of the move evidence: the Lisbon claim and its edges go, Porto stays
        await assert_erased(eng, move_src, [lisbon.record_id], QUERY)
        assert [c.object for c in await claims(eng)] == ["Porto"]
        assert await entity_ids(eng, "Asha") == [asha]

        # Re-authorisation: a new, separately cited statement re-derives the claim
        [again] = await send(eng, [msg("f1-3", "Asha", "I live in Lisbon.", "2026-10-20T09:00Z")])
        await settle(eng)
        lisbon_again = [c for c in await claims(eng) if c.object == "Lisbon"]
        assert [c.sources for c in lisbon_again] == [(again.record_id,)]
        assert lisbon_again[0].record_id != lisbon.record_id
        # The erased message identity cannot be reused to resurrect the erased evidence
        with pytest.raises(ValueError, match="erased message"):
            await send(eng, [msg("f1-2", "Asha", MOVE, "2026-10-01T10:00:00+05:30")])
    finally:
        await eng.stop()


async def test_f1_negatives_visit_work_other_home_and_missing_timezone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f1neg.db")
    try:
        await send(eng, [msg("n-1", "Asha", PORTO, "2026-08-01T10:00:00+05:30")])
        await send(
            eng,
            [
                msg("n-2", "Asha", "I visited Madrid last weekend.", "2026-10-03T10:00Z"),
                msg("n-3", "Asha", "I work at the Lisbon office.", "2026-10-04T10:00Z"),
                msg("n-4", "Asha", "I bought a flat in Faro.", "2026-10-05T10:00Z"),
            ],
        )
        [naive] = await send(eng, [msg("n-5", "Asha", "I live in Braga.", "2026-10-06T10:00:00")])
        await settle(eng)
        found = await claims(eng)
        # visit / work / buying another home never replace or add a residence
        residences = sorted(c.object or "" for c in by_predicate(found, "resides_in"))
        assert residences == ["Braga", "Porto"]  # multiple homes: Porto is not replaced
        assert not [c for c in found if c.object in ("Madrid", "Faro")]
        assert all(c.status == "active" for c in by_predicate(found, "resides_in"))
        # missing timezone: the source clock stays civil text and unresolved, never guessed
        assert naive.source.message is not None
        clock = naive.source.message["source_clock"]
        assert clock["timezone"] is None and clock["precision"] == "unknown"
        assert naive.source_at is None
        [braga] = [c for c in found if c.object == "Braga"]
        assert braga.time_precision == "unknown"
        assert not braga.event_resolved
        # no annotation claims a residence either
        assert not [u for u in unresolved(eng) if u.attribute == "home"]
    finally:
        await eng.stop()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "RM-22: the rules-only producer has no move-event rule; 'I moved from Porto to "
        "Lisbon last month' yields no move_origin/move_destination claim and no "
        "month-precision period (core/rule_miner.py matches only 'moved to')"
    ),
)
async def test_f1_move_event_binds_origin_destination_and_month_period(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f1move.db")
    try:
        await send(
            eng,
            [
                msg(
                    "m-1",
                    "Asha",
                    "I moved from Porto to Lisbon last month.",
                    "2026-10-01T10:00:00+05:30",
                )
            ],
        )
        found = await claims(eng)
        [origin] = by_predicate(found, "move_origin")
        [destination] = by_predicate(found, "move_destination")
        assert (origin.object, destination.object) == ("Porto", "Lisbon")
        assert destination.time_precision == "month"  # September 2026, not an exact day
    finally:
        await eng.stop()
