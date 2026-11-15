"""F3 correction (docs/BI01_CONTRACT_REWRITE_PLAN.md "RM-22 fixture specification";
docs/CLAUDE_CONTEXT_AND_TRACKER_2026-10-20.md section 6, F3 Correction).

An explicit correction reply never fabricates a Lisbon-to-Coimbra move and never deletes the
original source. The rules-only producer resolves an explicit "I meant X, not Y" of the same
sender to the one prior claim it names (ADR-088 amendment 2026-11-02): the prior claim is kept
as superseded history and the corrected claim is current. A correction the rules cannot read,
or one by another principal, stays an unresolved, unasserted annotation; another principal can
never replace the sender's claim."""

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


async def test_f3_correction_supersedes_known_time_replay_and_erasure(
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
        assert (claim.predicate, claim.object, claim.status) == ("resides_in", "Coimbra", "active")
        assert (claim.subject, claim.principal) == ("Asha", "Asha")
        assert claim.sources == (fix.record_id,)
        assert claim.evidence == ((fix.record_id, 0, len(CORRECTION), CORRECTION),)
        # the prior claim is kept as superseded history: never deleted, never a move
        [prior] = [c for c in await claims(eng, superseded=True) if c.object == "Lisbon"]
        assert (prior.status, prior.sources) == ("superseded", (lisbon_src.record_id,))
        assert by_predicate(await claims(eng), "move_destination") == []
        assert unresolved(eng) == []
        # the raw correction and its reply link are kept verbatim
        assert fix.content == CORRECTION
        assert fix.source.message is not None and fix.source.message["reply_to"] == "f3-1"
        assert lisbon_src.content == "I live in Lisbon."

        # known time: before the correction was recorded, Lisbon is what memory knew
        assert await semantic_hits(eng, QUERY, recorded_before=cut) == ["Asha resides_in Lisbon"]
        assert await read_claims(eng, QUERY) == ["Asha resides_in Coimbra"]
        assert all(e.claim_state == "current" for e in edges_of(await edges(eng), claim.record_id))

        await assert_replay(eng, ["Asha", "Lisbon", "Coimbra"])
        # erasing the correction removes the corrected claim, its edges and hits
        await assert_erased(eng, fix, [claim.record_id], QUERY)
        assert not [c for c in await claims(eng, superseded=True) if c.object == "Coimbra"]
        [restored] = [c for c in await claims(eng) if c.object == "Lisbon"]
        assert (restored.record_id, restored.status, restored.sources) == (
            prior.record_id,
            "active",
            (lisbon_src.record_id,),
        )
        assert await read_claims(eng, QUERY) == ["Asha resides_in Lisbon"]
        assert await semantic_hits(eng, QUERY) == ["Asha resides_in Lisbon"]
        assert all(e.claim_state == "current" for e in edges_of(await edges(eng), prior.record_id))
        await assert_erased(eng, lisbon_src, [prior.record_id], QUERY)
        assert await claims(eng, superseded=True) == []
    finally:
        await eng.stop()


async def test_f3_unreadable_correction_stays_unresolved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f3u.db")
    try:
        await send(eng, [msg("u-1", "Asha", "I live in Lisbon.", "2026-10-01T09:00Z")])
        [vague, other] = await send(
            eng,
            [
                msg("u-2", "Asha", "Correction: wrong city.", "2026-10-01T09:05Z"),
                msg("u-3", "Asha", "Correction, I meant Coimbra, not Faro.", "2026-10-01T09:06Z"),
            ],
        )
        # no target named, or no prior claim with the named value: nothing is replaced
        assert [(c.object, c.status) for c in await claims(eng)] == [("Lisbon", "active")]
        notes = {u.source.record_id: (u.reason, u.asserted) for u in unresolved(eng)}
        assert notes == {
            vague.record_id: ("correction_requires_resolution", False),
            other.record_id: ("correction_target_not_found", False),
        }
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


async def test_f3_erasing_earlier_correction_does_not_reopen_a_claim_under_a_live_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f3-chain.db")
    try:
        await send(eng, [msg("chain-1", "Asha", "I live in Lisbon.", "2026-10-01T09:00Z")])
        [first_fix] = await send(
            eng,
            [msg("chain-2", "Asha", CORRECTION, "2026-10-01T09:05Z", reply_to="chain-1")],
        )
        await settle(eng)
        [first_claim] = await claims(eng)
        [second_fix] = await send(
            eng,
            [
                msg(
                    "chain-3",
                    "Asha",
                    "Correction, I meant Porto, not Coimbra.",
                    "2026-10-01T09:10Z",
                    reply_to="chain-2",
                )
            ],
        )
        await settle(eng)
        [current] = await claims(eng)
        assert current.object == "Porto"

        await assert_erased(eng, first_fix, [first_claim.record_id], QUERY)
        [remaining] = await claims(eng)
        assert (remaining.object, remaining.sources) == ("Porto", (second_fix.record_id,))
        assert not [c for c in await claims(eng) if c.object == "Lisbon"]
    finally:
        await eng.stop()


async def test_f3_erasure_keeps_a_corrected_claim_with_independent_surviving_support(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f3-supported-successor.db")
    try:
        await send(eng, [msg("support-1", "Asha", "I live in Lisbon.", "2026-10-01T09:00Z")])
        [fix] = await send(
            eng,
            [msg("support-2", "Asha", CORRECTION, "2026-10-01T09:05Z", reply_to="support-1")],
        )
        await settle(eng)
        [correction_claim] = await claims(eng)
        [independent] = await send(
            eng, [msg("support-3", "Asha", "I live in Coimbra.", "2026-10-01T09:10Z")]
        )
        await settle(eng)
        [supported] = await claims(eng)
        assert supported.object == "Coimbra"
        assert independent.record_id in supported.sources

        await eng.forget(fix.record_id, namespace="a", hard=True)
        [remaining] = await claims(eng)
        assert remaining.record_id == correction_claim.record_id
        assert remaining.object == "Coimbra"
        assert not [c for c in await claims(eng) if c.object == "Lisbon"]
    finally:
        await eng.stop()


async def test_f3_erased_correction_restores_prior_claim_with_remaining_proven_support(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f3-supported-prior.db")
    try:
        [first] = await send(
            eng, [msg("prior-1", "Asha", "I moved to Sweden four years ago.", "2026-06-10T09:00Z")]
        )
        [second] = await send(
            eng,
            [msg("prior-2", "Asha", "Yes, I moved to Sweden four years ago.", "2026-07-12T09:00Z")],
        )
        await settle(eng)
        [prior] = by_predicate(await claims(eng), "resides_in")
        [fix] = await send(
            eng,
            [
                msg(
                    "prior-3",
                    "Asha",
                    "Correction, I meant Portugal, not Sweden.",
                    "2026-07-13T09:00Z",
                    reply_to="prior-2",
                )
            ],
        )
        await settle(eng)

        await eng.forget(first.record_id, namespace="a", hard=True)
        await eng.forget(fix.record_id, namespace="a", hard=True)
        [restored] = by_predicate(await claims(eng), "resides_in")
        assert restored.record_id == prior.record_id
        assert restored.object == "Sweden"
        assert second.record_id in restored.sources
        assert all(
            source_id != first.record_id or not quote
            for source_id, _start, _end, quote in restored.evidence
        )
        assert await read_claims(eng, QUERY) == ["Asha resides_in Sweden"]
    finally:
        await eng.stop()


async def test_f3_restore_does_not_override_a_later_prior_lifecycle_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from datetime import timedelta

    from memspine.store.events import EventKind, MemoryEvent

    eng = await _boot(monkeypatch, tmp_path / "f3-later-lifecycle.db")
    try:
        [source] = await send(
            eng, [msg("life-1", "Asha", "I live in Lisbon.", "2026-10-01T09:00Z")]
        )
        [fix] = await send(
            eng, [msg("life-2", "Asha", CORRECTION, "2026-10-01T09:05Z", reply_to="life-1")]
        )
        await settle(eng)
        [prior] = [c for c in await claims(eng, superseded=True) if c.object == "Lisbon"]
        stored = await eng._storage.get_record(prior.record_id)
        assert stored is not None and stored.superseded_at is not None
        later = stored.model_copy(
            update={"superseded_at": stored.superseded_at + timedelta(seconds=1)}
        )
        await eng._append_and_project(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace="a",
                actor="system",
                payload={"record": later.model_dump(mode="json")},
            )
        )

        await eng.forget(fix.record_id, namespace="a", hard=True)
        assert not [c for c in await claims(eng) if c.record_id == prior.record_id]
        assert source.record_id not in {c.sources[0] for c in await claims(eng)}
    finally:
        await eng.stop()


async def test_f3_soft_forgotten_support_does_not_restore_prior_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f3-soft-forgotten-support.db")
    try:
        [source] = await send(
            eng, [msg("soft-1", "Asha", "I live in Lisbon.", "2026-10-01T09:00Z")]
        )
        [fix] = await send(
            eng, [msg("soft-2", "Asha", CORRECTION, "2026-10-01T09:05Z", reply_to="soft-1")]
        )
        await settle(eng)
        [prior] = [c for c in await claims(eng, superseded=True) if c.object == "Lisbon"]

        await eng.forget(source.record_id, namespace="a", hard=False)
        await eng.forget(fix.record_id, namespace="a", hard=True)
        assert not [c for c in await claims(eng) if c.record_id == prior.record_id]
        graph_edges = [
            edge
            for edge in await eng._graph.edge_list("a")
            if edge.properties.get("record_id") == prior.record_id
            and edge.properties.get("compiled") == "edge_compiler"
        ]
        assert not [edge for edge in graph_edges if edge.properties.get("status") == "active"]
    finally:
        await eng.stop()


async def test_f3_interrupted_restore_finishes_after_prior_support_scrub(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from memspine.store.events import EventKind

    eng = await _boot(monkeypatch, tmp_path / "f3-interrupted-source-scrub.db")
    try:
        [first] = await send(
            eng, [msg("scrub-1", "Asha", "I moved to Sweden four years ago.", "2026-06-10T09:00Z")]
        )
        [second] = await send(
            eng,
            [msg("scrub-2", "Asha", "Yes, I moved to Sweden four years ago.", "2026-07-12T09:00Z")],
        )
        await settle(eng)
        [prior] = by_predicate(await claims(eng), "resides_in")
        [fix] = await send(
            eng,
            [
                msg(
                    "scrub-3",
                    "Asha",
                    "Correction, I meant Portugal, not Sweden.",
                    "2026-07-13T09:00Z",
                    reply_to="scrub-2",
                )
            ],
        )
        await settle(eng)
        original_append = eng._append_and_project
        restored_write = False

        async def fail_before_first_link(event: object) -> None:
            nonlocal restored_write
            kind = getattr(event, "kind", None)
            payload = getattr(event, "payload", {})
            if kind is EventKind.WRITE and isinstance(payload, dict):
                restored_write |= "correction_erasure_restore" in payload
            if restored_write and kind is EventKind.LINK:
                raise RuntimeError("injected after restoration WRITE")
            await original_append(event)  # type: ignore[arg-type]  # test injection

        with monkeypatch.context() as fault:
            fault.setattr(eng, "_append_and_project", fail_before_first_link)
            with pytest.raises(RuntimeError):
                await eng.forget(fix.record_id, namespace="a", hard=True)

        await eng.forget(first.record_id, namespace="a", hard=True)
        await eng.forget(fix.record_id, namespace="a", hard=True)
        [restored] = by_predicate(await claims(eng), "resides_in")
        assert (restored.record_id, restored.object) == (prior.record_id, "Sweden")
        assert second.record_id in restored.sources
        assert all(
            source_id != first.record_id or not quote
            for source_id, _start, _end, quote in restored.evidence
        )
        stored_edges = [
            edge
            for edge in await eng._graph.edge_list("a")
            if edge.properties.get("record_id") == prior.record_id
            and edge.properties.get("compiled") == "edge_compiler"
        ]
        assert stored_edges
        assert all(
            first.record_id not in edge.properties.get("evidence_ids", ()) for edge in stored_edges
        )
        assert all(
            first.record_id in edge.properties.get("evidence_erased", ()) for edge in stored_edges
        )
    finally:
        await eng.stop()


async def test_f3_invalid_restatement_proof_cannot_become_valid_on_supersede(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from memspine.store.events import EventKind, MemoryEvent
    from memspine.trust.restatement import claim_digest, valid_proof

    eng = await _boot(monkeypatch, tmp_path / "f3-invalid-proof.db")
    try:
        await send(eng, [msg("proof-1", "Asha", "I live in Lisbon.", "2026-10-01T09:00Z")])
        await send(eng, [msg("proof-2", "Asha", "Yes, I live in Lisbon.", "2026-10-01T09:01Z")])
        await settle(eng)
        [prior] = by_predicate(await claims(eng), "resides_in")
        record = await eng._storage.get_record(prior.record_id)
        assert record is not None and record.fingerprint.restatement_support is not None
        active_envelope = dict(record.fingerprint.envelope or {})
        superseded_envelope = {**active_envelope, "status": "superseded"}
        forged = record.fingerprint.restatement_support.model_copy(
            update={"claim_digest": claim_digest(superseded_envelope)}
        )
        invalid = record.model_copy(
            update={
                "fingerprint": record.fingerprint.model_copy(update={"restatement_support": forged})
            }
        )
        assert not valid_proof(invalid)
        await eng._append_and_project(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace="a",
                actor="system",
                payload={"record": invalid.model_dump(mode="json")},
            )
        )
        await send(
            eng,
            [msg("proof-3", "Asha", CORRECTION, "2026-10-01T09:05Z", reply_to="proof-2")],
        )
        await settle(eng)
        archived = await eng._storage.get_record(prior.record_id)
        assert archived is not None and archived.fingerprint.restatement_support is None
    finally:
        await eng.stop()


@pytest.mark.parametrize("interrupt_at", ["manifest", "restore_write", "first_link"])
@pytest.mark.parametrize("recovery_mode", ["retry", "restart"])
async def test_f3_correction_restore_resumes_after_interruption(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, interrupt_at: str, recovery_mode: str
) -> None:
    import memspine.trust.pipeline.erasure_state as erasure_state
    from memspine.store.events import EventKind

    db_path = tmp_path / f"f3-interrupt-{interrupt_at}.db"
    eng = await _boot(monkeypatch, db_path)
    try:
        [lisbon_src] = await send(
            eng, [msg("interrupt-1", "Asha", "I live in Lisbon.", "2026-10-01T09:00Z")]
        )
        [fix] = await send(
            eng,
            [msg("interrupt-2", "Asha", CORRECTION, "2026-10-01T09:05Z", reply_to="interrupt-1")],
        )
        await settle(eng)
        [corrected] = await claims(eng)
        [prior] = [c for c in await claims(eng, superseded=True) if c.object == "Lisbon"]

        with monkeypatch.context() as fault:
            if interrupt_at == "manifest":
                original_restore = erasure_state._restore_erased_correction

                async def fail_after_manifest(*args: object, **kwargs: object) -> bool:
                    raise RuntimeError("injected after manifest")

                fault.setattr(erasure_state, "_restore_erased_correction", fail_after_manifest)
            else:
                original_append = eng._append_and_project
                restored_write = False
                links = 0

                async def fail_during_restore(event: object) -> None:
                    nonlocal restored_write, links
                    kind = getattr(event, "kind", None)
                    payload = getattr(event, "payload", {})
                    if kind is EventKind.WRITE and isinstance(payload, dict):
                        restored_write |= "correction_erasure_restore" in payload
                    if restored_write and kind is EventKind.LINK:
                        links += 1
                        if interrupt_at == "restore_write" or (
                            interrupt_at == "first_link" and links == 2
                        ):
                            raise RuntimeError("injected during restoration LINK projection")
                    await original_append(event)  # type: ignore[arg-type]  # test injection

                fault.setattr(eng, "_append_and_project", fail_during_restore)
            with pytest.raises(RuntimeError):
                await eng.forget(fix.record_id, namespace="a", hard=True)
        if recovery_mode == "retry":
            # Same-process retry must finish missing LINK events without a second restore.
            await eng.forget(fix.record_id, namespace="a", hard=True)
            [same_process] = [c for c in await claims(eng) if c.object == "Lisbon"]
            assert same_process.record_id == prior.record_id
            assert all(
                e.claim_state == "current" for e in edges_of(await edges(eng), prior.record_id)
            )
        await eng.stop()

        eng = await _boot(monkeypatch, db_path)
        [restored] = [c for c in await claims(eng) if c.object == "Lisbon"]
        assert (restored.record_id, restored.sources) == (prior.record_id, (lisbon_src.record_id,))
        assert await read_claims(eng, QUERY) == ["Asha resides_in Lisbon"]
        assert all(e.claim_state == "current" for e in edges_of(await edges(eng), prior.record_id))

        assert not [c for c in await claims(eng) if c.record_id == corrected.record_id]
        [restored] = [c for c in await claims(eng) if c.object == "Lisbon"]
        assert restored.record_id == prior.record_id
        before = 0
        offset = 0
        while True:
            batch = await eng._storage.read_events(after_seq=offset, limit=1000)
            if not batch:
                break
            before += sum(
                event.kind is EventKind.LINK
                and event.payload.get("props", {}).get("record_id") == prior.record_id
                and event.payload.get("reason") == "edge_compiler"
                for event in batch
            )
            assert batch[-1].seq is not None
            offset = batch[-1].seq
        await eng.stop()
        eng = await _boot(monkeypatch, db_path)
        after = 0
        offset = 0
        while True:
            batch = await eng._storage.read_events(after_seq=offset, limit=1000)
            if not batch:
                break
            after += sum(
                event.kind is EventKind.LINK
                and event.payload.get("props", {}).get("record_id") == prior.record_id
                and event.payload.get("reason") == "edge_compiler"
                for event in batch
            )
            assert batch[-1].seq is not None
            offset = batch[-1].seq
        assert after == before
        if recovery_mode == "restart":
            # Finish hard erasure once startup has completed the restoration projection.
            await eng.forget(fix.record_id, namespace="a", hard=True)
    finally:
        await eng.stop()
