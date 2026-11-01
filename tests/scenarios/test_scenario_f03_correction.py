"""F3 correction (docs/BI01_CONTRACT_REWRITE_PLAN.md "RM-22 fixture specification";
docs/CLAUDE_CONTEXT_AND_TRACKER_2026-10-20.md section 6, F3 Correction).

An explicit correction reply never fabricates a Lisbon-to-Coimbra move and never deletes the
original source. The rules-only producer cannot resolve a correction target, so the reply is
kept as an unresolved, unasserted annotation; another principal can never replace the
sender's claim."""

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
CORRECTION = "Correction, I meant Coimbra, not Lisbon."


async def test_f3_correction_stays_unresolved_known_time_replay_and_erasure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f3.db")
    try:
        [lisbon_src] = await send(
            eng, [msg("f3-1", "Asha", "I live in Lisbon.", "2026-10-01T09:00Z")]
        )
        cut = await tick()
        [fix] = await send(
            eng, [msg("f3-2", "Asha", CORRECTION, "2026-10-01T09:05Z", reply_to="f3-1")]
        )
        await settle(eng)

        [claim] = await claims(eng)
        assert (claim.predicate, claim.object, claim.status) == ("resides_in", "Lisbon", "active")
        # no fabricated move, no Coimbra claim from a correction the rules cannot resolve
        assert not [c for c in await claims(eng) if c.object == "Coimbra"]
        assert by_predicate(await claims(eng), "move_destination") == []
        [note] = unresolved(eng)
        assert note.source.record_id == fix.record_id
        assert note.reason == "correction_requires_resolution" and note.asserted is False
        # the raw correction and its reply link are kept verbatim
        assert fix.content == CORRECTION
        assert fix.source.message is not None and fix.source.message["reply_to"] == "f3-1"
        assert lisbon_src.content == "I live in Lisbon."

        assert await semantic_hits(eng, QUERY, recorded_before=cut) == ["Asha resides_in Lisbon"]
        assert await read_claims(eng, QUERY) == ["Asha resides_in Lisbon"]
        assert all(e.claim_state == "current" for e in edges_of(await edges(eng), claim.record_id))

        await assert_replay(eng, ["Asha", "Lisbon", "Coimbra"])
        # erasing the correction removes its annotation and leaves the claim alone
        await assert_erased(eng, fix, [], QUERY)
        assert [c.record_id for c in await claims(eng)] == [claim.record_id]
        await assert_erased(eng, lisbon_src, [claim.record_id], QUERY)
        assert await claims(eng) == []
    finally:
        await eng.stop()


async def test_f3_other_principal_cannot_replace_the_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f3p.db")
    try:
        await send(eng, [msg("p-1", "Asha", "I live in Lisbon.", "2026-10-01T09:00Z")])
        [bob_fix, bob_home] = await send(
            eng,
            [
                msg(
                    "p-2",
                    "Bob",
                    "Correction, Asha meant Coimbra, not Lisbon.",
                    "2026-10-01T09:06Z",
                    reply_to="p-1",
                ),
                msg("p-3", "Bob", "I live in Coimbra.", "2026-10-01T09:07Z"),
            ],
        )
        found = await claims(eng)
        asha = [c for c in found if c.subject == "Asha"]
        assert [(c.object, c.status) for c in asha] == [("Lisbon", "active")]
        bob = [c for c in found if c.subject == "Bob"]
        assert [(c.object, c.principal) for c in bob] == [("Coimbra", "Bob")]
        assert bob[0].sources == (bob_home.record_id,)
        assert {u.source.record_id for u in unresolved(eng)} == {bob_fix.record_id}
    finally:
        await eng.stop()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "RM-22: no correction resolution in the rules-only path (derive/non_generative_facts.py "
        "source_ineligible leaves every correction unresolved); the supported prior claim is "
        "never marked superseded nor replaced with a known-time link"
    ),
)
async def test_f3_correction_supersedes_the_supported_prior_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f3x.db")
    try:
        await send(eng, [msg("x-1", "Asha", "I live in Lisbon.", "2026-10-01T09:00Z")])
        cut = await tick()
        await send(eng, [msg("x-2", "Asha", CORRECTION, "2026-10-01T09:05Z", reply_to="x-1")])
        current = await claims(eng)
        assert [(c.object, c.status) for c in current] == [("Coimbra", "active")]
        assert await semantic_hits(eng, QUERY, recorded_before=cut) == ["Asha resides_in Lisbon"]
    finally:
        await eng.stop()
