"""F2 identity (docs/BI01_CONTRACT_REWRITE_PLAN.md "RM-22 fixture specification";
docs/CLAUDE_CONTEXT_AND_TRACKER_2026-10-20.md section 6, F2 Ambiguity; ADR-089).

"I" binds the authenticated sender; a name never selects a canonical entity by nearest-name
merge: with two same-name entities a new mention starts a provisional entity of its own, a
name in another namespace never links, and a value repeated in one source has no unique
support span, so it stays an unresolved annotation."""

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
    entity_ids,
    msg,
    semantic_hits,
    send,
    settle,
    tick,
    unresolved,
)
from scenario_harness import boot as _boot

from memspine.store.records import SourceInfo

QUERY = "Where does Alex live"


async def _alex(eng: object, note: str, ns: str = "a") -> str:
    """A stored Alex identity of its own (``entity_ref:new``: never the known Alex)."""
    rec = await eng.write(  # type: ignore[attr-defined]
        note,
        namespace=ns,
        memory_type="semantic",
        entity="Alex",
        tags=["entity_ref:new", "entity_type:person"],
        source=SourceInfo(role="user", channel="profile"),
    )
    return str(rec.record_id)


async def test_f2_same_name_entities_stay_distinct_known_time_replay_and_erasure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f2.db")
    try:
        await _alex(eng, "Alex Novak plays chess")
        await _alex(eng, "Alex Moreau teaches piano")
        known = await entity_ids(eng, "Alex")
        assert len(known) == 2  # two Alex identities exist

        # The sender Alex (a third identity as far as the evidence goes) states a residence
        cut = await tick()
        [src] = await send(eng, [msg("f2-1", "Alex", "I live in Cambridge.", "2026-10-01T09:00Z")])
        await settle(eng)
        [claim] = by_predicate(await claims(eng), "resides_in")
        assert (claim.subject, claim.principal, claim.object) == ("Alex", "Alex", "Cambridge")
        assert claim.evidence == ((src.record_id, 0, 20, "I live in Cambridge."),)

        # No nearest-name merge: the claim's subject is neither known Alex
        mine = edges_of(await edges(eng), claim.record_id)
        [subject_edge] = [e for e in mine if e.rel == "role:subject"]
        assert subject_edge.dst not in known
        assert len(await entity_ids(eng, "Alex")) == 3

        # Known-time: before the statement was recorded nobody named Alex lives anywhere
        assert await semantic_hits(eng, QUERY, recorded_before=cut) == []
        assert await semantic_hits(eng, QUERY) == ["Alex resides_in Cambridge"]

        await assert_replay(eng, ["Alex", "Cambridge"])
        await assert_erased(eng, src, [claim.record_id], QUERY)
        # the two pre-existing identities are untouched by the erasure
        assert set(known) <= set(await entity_ids(eng, "Alex"))
    finally:
        await eng.stop()


async def test_f2_unique_name_links_and_other_namespace_never_does(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f2u.db")
    try:
        await _alex(eng, "Alex Novak plays chess")
        [only] = await entity_ids(eng, "Alex")
        await _alex(eng, "Alex Weber rows", ns="b")  # cross-namespace namesake
        [other_ns] = await entity_ids(eng, "Alex", ns="b")
        await send(eng, [msg("u-1", "Alex", "I live in Leeds.", "2026-10-01T09:00Z")])
        await settle(eng)
        [claim] = by_predicate(await claims(eng), "resides_in")
        [subject_edge] = [
            e for e in edges_of(await edges(eng), claim.record_id) if e.rel == "role:subject"
        ]
        assert subject_edge.dst == only  # exactly one same-namespace candidate (unique_name)
        assert other_ns != only and other_ns.startswith("ent:b:")
        assert await entity_ids(eng, "Alex", ns="b") == [other_ns]  # never linked across
    finally:
        await eng.stop()


async def test_f2_repeated_and_unicode_spans(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f2s.db")
    try:
        text = "Grüße! I live in Zürich."
        repeated = "I live in Zürich and Zürich is lovely."
        [exact, twice] = await send(
            eng,
            [
                msg("s-1", "Maëlle", text, "2026-10-01T09:00Z"),
                msg("s-2", "Maëlle", repeated, "2026-10-01T09:05Z"),
            ],
        )
        [claim] = await claims(eng)
        [(source_id, start, end, quote)] = claim.evidence
        assert source_id == exact.record_id
        assert text[start:end] == quote == "I live in Zürich."  # code-point offsets
        assert (claim.subject, claim.object) == ("Maëlle", "Zürich")
        # the same value twice in one source has no unique support span
        [note] = [u for u in unresolved(eng) if u.source.record_id == twice.record_id]
        assert (note.reason, note.attribute, note.value) == (
            "ambiguous_support_span",
            "home",
            "Zürich",
        )
        assert note.asserted is False and note.span is None
    finally:
        await eng.stop()


async def test_f2_met_alex_yields_no_identity_guess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f2m.db")
    try:
        await _alex(eng, "Alex Novak plays chess")
        await _alex(eng, "Alex Moreau teaches piano")
        await send(
            eng,
            [
                msg(
                    "m-1",
                    "Asha",
                    "I met Alex in Cambridge yesterday. He gave me a book.",
                    "2026-10-02T09:00Z",
                )
            ],
        )
        await settle(eng)
        assert await claims(eng) == []  # no guessed Alex, city or book
        assert len(await entity_ids(eng, "Alex")) == 2  # no merge, no new canonical Alex
        assert await entity_ids(eng, "Cambridge") == []
    finally:
        await eng.stop()


async def test_f2_pronoun_binds_unique_local_mention(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f2p.db")
    try:
        await send(
            eng,
            [msg("p-1", "Asha", "I met Alex yesterday. He lives in Leeds.", "2026-10-02T09:00Z")],
        )
        [claim] = by_predicate(await claims(eng), "resides_in")
        assert (claim.subject, claim.object, claim.principal) == ("Alex", "Leeds", "Asha")
    finally:
        await eng.stop()
