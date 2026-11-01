"""F9 group (docs/BI01_CONTRACT_REWRITE_PLAN.md "Expanded synthetic fixtures (RM-54)").

Three named participants discuss travel; one says "we may attend". Each sender and reply is
kept; nobody is made an attendee from a hedged "we", and names never collapse into one
actor. A candidate that attributes one participant's words to another is rejected."""

from __future__ import annotations

from pathlib import Path

import pytest
from scenario_harness import (
    ScriptedExtractor,
    assert_erased,
    assert_replay,
    candidate,
    claims,
    entity_ids,
    msg,
    send,
    settle,
    support,
    tick,
    unresolved,
)
from scenario_harness import boot as _boot

QUERY = "Who is going to Lisbon"
THREAD = [
    msg("g-1", "Ana", "I live in Porto. Anyone up for Lisbon in May?", "2026-04-01T09:00Z"),
    msg("g-2", "Ben", "We may attend the Lisbon trip.", "2026-04-01T09:01Z", reply_to="g-1"),
    msg("g-3", "Caro", "I live in Lisbon, so I can host.", "2026-04-01T09:02Z", reply_to="g-2"),
]


async def test_f9_each_participant_keeps_their_own_claims(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f9.db")
    try:
        cut = await tick()
        ana, ben, caro = await send(eng, THREAD)
        await settle(eng)
        for rec, sender, parent in ((ana, "Ana", None), (ben, "Ben", "g-1"), (caro, "Caro", "g-2")):
            assert rec.source.message is not None
            assert rec.source.message["sender"]["id"] == sender
            assert rec.source.message.get("reply_to") == parent
        found = await claims(eng)
        assert [(c.subject, c.predicate, c.object, c.sources) for c in found] == [
            ("Ana", "resides_in", "Porto", (ana.record_id,)),
            ("Caro", "resides_in", "Lisbon", (caro.record_id,)),
        ]
        # a hedged "we" makes nobody an attendee and is never one group actor
        assert not [c for c in found if c.predicate in ("attended", "participated_in")]
        assert not [c for c in found if c.subject.lower() in ("we", "ben")]
        # three distinct participants, three distinct entities where they are named
        ids = [await entity_ids(eng, n) for n in ("Ana", "Caro")]
        assert all(len(i) == 1 for i in ids) and ids[0] != ids[1]
        assert await entity_ids(eng, "We") == []

        hits = await eng.search(
            QUERY, namespace="a", memory_types=["semantic"], recorded_before=cut
        )
        assert hits == []
        await assert_replay(eng, ["Ana", "Ben", "Caro", "Porto", "Lisbon"])
        await assert_erased(eng, caro, [found[1].record_id], QUERY)
        assert [c.subject for c in await claims(eng)] == ["Ana"]
    finally:
        await eng.stop()


async def test_f9_cross_participant_attribution_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ex = ScriptedExtractor()
    text = "We may attend the Lisbon trip."
    # a candidate that names Ana as the asserting owner of Ben's words
    ex.on(
        text,
        lambda r, s: [
            candidate(
                r,
                s,
                predicate="attended",
                obj="Lisbon trip",
                object_entity_type="event",
                subject="Ana",
                owner="Ana",
                evidence=support(r, text),
            )
        ],
    )
    eng = await _boot(monkeypatch, tmp_path / "f9x.db", extractor=ex)
    try:
        [ben] = await send(eng, [msg("x-1", "Ben", text, "2026-04-01T09:01Z")])
        assert await claims(eng) == []
        [note] = unresolved(eng)
        assert (note.source.record_id, note.reason) == (ben.record_id, "invalid_assertion_owner")
    finally:
        await eng.stop()
