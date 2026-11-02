"""F5 later evidence (docs/BI01_CONTRACT_REWRITE_PLAN.md "RM-22 fixture specification";
docs/CLAUDE_CONTEXT_AND_TRACKER_2026-10-20.md section 6, F5 Later evidence).

June: "moved from my home country four years ago"; July: an explicit reply identifies that
country as Sweden. The resolution needs both supports; the June-known view stays unresolved;
no duplicate move or exact date is invented; an erased binding can never re-authorise the
resolution. The rules-only producer resolves neither message, so the positive outcome is an
expected failure and the negative guarantees are asserted directly."""

from __future__ import annotations

from pathlib import Path

import pytest
from scenario_harness import (
    assert_erased,
    assert_replay,
    by_predicate,
    claims,
    msg,
    semantic_hits,
    send,
    settle,
    tick,
)
from scenario_harness import boot as _boot

from memspine.ingest import windows as ingest_windows

QUERY = "Where is Asha from"
JUNE = "I moved here from my home country four years ago."
JULY = "The home country I mentioned is Sweden."


async def test_f5_no_single_source_or_invented_resolution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f5.db")
    try:
        [june] = await send(eng, [msg("f5-1", "Asha", JUNE, "2026-06-10T09:00Z")])
        june_known = await tick()
        [july] = await send(eng, [msg("f5-2", "Asha", JULY, "2026-07-12T09:00Z", reply_to="f5-1")])
        await settle(eng)
        found = await claims(eng)
        # neither message alone supports a resolution; nothing is invented
        assert not [c for c in found if c.object == "Sweden"]
        assert by_predicate(found, "move_origin") == by_predicate(found, "home_country") == []
        assert await semantic_hits(eng, QUERY, recorded_before=june_known) == []
        assert july.source.message is not None and july.source.message["reply_to"] == "f5-1"

        await assert_replay(eng, ["Asha", "Sweden"])
        # an erased binding cannot re-authorise: re-running the rules after the erasure
        await assert_erased(eng, july, [], QUERY)
        await ingest_windows.reresolve_unresolved(eng, "a")
        await eng.flush_windows("a")
        assert not [c for c in await claims(eng) if c.object == "Sweden"]
        assert june.content == JUNE
    finally:
        await eng.stop()


async def test_f5_restated_move_is_one_claim_without_an_invented_date(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f5r.db")
    try:
        [june] = await send(
            eng, [msg("r-1", "Asha", "I moved to Sweden four years ago.", "2026-06-10T09:00Z")]
        )
        june_known = await tick()
        [july] = await send(
            eng, [msg("r-2", "Asha", "Yes, I moved to Sweden four years ago.", "2026-07-12T09:00Z")]
        )
        await settle(eng)
        [claim] = by_predicate(await claims(eng), "resides_in")  # no duplicate move
        assert claim.object == "Sweden" and claim.time_precision == "unknown"
        assert not claim.event_resolved  # "four years ago" is not turned into a date
        assert await semantic_hits(eng, QUERY, recorded_before=june_known) == [
            "Asha resides_in Sweden"
        ]
        await assert_replay(eng, ["Asha", "Sweden"])
        assert july.record_id != june.record_id
    finally:
        await eng.stop()


@pytest.mark.parametrize(
    "lineage_table",
    [
        pytest.param(
            False,
            marks=pytest.mark.xfail(
                strict=True,
                reason=(
                    "RM-22: the restated claim keeps both sources (memories/semantic/store.py "
                    "_restated_support), but without write.lineage_table the erasure plan "
                    "reads a record with several source.parents as conjunctive members "
                    "(trust/pipeline/erasure_state.py build_graph fallback role), so erasing "
                    "the June source hides it pending re-derivation although the July source "
                    "still supports it"
                ),
            ),
        ),
        True,
    ],
)
async def test_f5_restated_claim_survives_erasure_of_one_support(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lineage_table: bool
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f5s.db", lineage_table=lineage_table)
    try:
        [june] = await send(
            eng, [msg("s-1", "Asha", "I moved to Sweden four years ago.", "2026-06-10T09:00Z")]
        )
        [july] = await send(
            eng, [msg("s-2", "Asha", "Yes, I moved to Sweden four years ago.", "2026-07-12T09:00Z")]
        )
        await eng.forget(june.record_id, namespace="a", hard=True)
        [claim] = by_predicate(await claims(eng), "resides_in")
        assert claim.object == "Sweden" and july.record_id in claim.sources
    finally:
        await eng.stop()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "RM-22: no cross-message binding in the rules-only path; the July reply naming Sweden "
        "never resolves the June home-country origin into one two-source claim with "
        "year precision (derive/non_generative_facts.py compiles one source at a time)"
    ),
)
async def test_f5_two_source_binding_resolves_sweden(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f5x.db")
    try:
        [june] = await send(eng, [msg("x-1", "Asha", JUNE, "2026-06-10T09:00Z")])
        june_known = await tick()
        [july] = await send(eng, [msg("x-2", "Asha", JULY, "2026-07-12T09:00Z", reply_to="x-1")])
        [origin] = by_predicate(await claims(eng), "move_origin")
        assert origin.object == "Sweden"
        assert {e[0] for e in origin.evidence} == {june.record_id, july.record_id}
        assert origin.time_precision == "year"  # approximately 2022, never an exact day
        assert await semantic_hits(eng, QUERY, recorded_before=june_known) == []
    finally:
        await eng.stop()
