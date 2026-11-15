"""F5 later evidence (docs/BI01_CONTRACT_REWRITE_PLAN.md "RM-22 fixture specification";
docs/CLAUDE_CONTEXT_AND_TRACKER_2026-10-20.md section 6, F5 Later evidence).

June: "moved from my home country four years ago"; July: an explicit reply identifies that
country as Sweden. The resolution needs both supports; the June-known view stays unresolved;
no duplicate move or exact date is invented; an erased binding can never re-authorise the
resolution. The rules-only producer requires both visible turns and their explicit reply."""

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

from memspine.schemas.durable.semantic import SemanticEnvelope
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.source_binding import source_binding

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
        [july] = await send(eng, [msg("f5-2", "Asha", JULY, "2026-07-12T09:00Z")])
        await settle(eng)
        found = await claims(eng)
        # neither message alone supports a resolution; nothing is invented
        assert not [c for c in found if c.object == "Sweden"]
        assert by_predicate(found, "move_origin") == by_predicate(found, "home_country") == []
        assert await semantic_hits(eng, QUERY, recorded_before=june_known) == []
        assert july.source.message is not None and july.source.message.get("reply_to") is None

        await assert_replay(eng, ["Asha", "Sweden"])
        # an erased binding cannot re-authorise: re-running the rules after the erasure
        await assert_erased(eng, july, [], QUERY)
        await eng.activate_derivation_policy(
            "a", policy_revision="f5:erasure", registry_digest="f5:erasure", runner_quiesced=True
        )
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
    [False, True],
)
async def test_f5_restated_claim_survives_erasure_of_one_support(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lineage_table: bool
) -> None:
    db_path = tmp_path / "f5s.db"
    eng = await _boot(monkeypatch, db_path, lineage_table=lineage_table)
    try:
        [june] = await send(
            eng, [msg("s-1", "Asha", "I moved to Sweden four years ago.", "2026-06-10T09:00Z")]
        )
        [july] = await send(
            eng, [msg("s-2", "Asha", "Yes, I moved to Sweden four years ago.", "2026-07-12T09:00Z")]
        )
        # Reopen before erasure: the independent proof must survive the SQL fingerprint round-trip.
        await eng.stop()
        eng = await _boot(monkeypatch, db_path, lineage_table=lineage_table)
        [claim] = by_predicate(await claims(eng), "resides_in")
        persisted = await eng._storage.get_record(claim.record_id)
        assert persisted is not None and persisted.fingerprint.restatement_support is not None
        from memspine.trust.restatement import independent_source, valid_proof

        assert set(valid_proof(persisted)) == {june.record_id, july.record_id}
        source_local = dict(persisted.fingerprint.envelope or {})
        source_local["evidence"] = [
            item
            for item in source_local.get("evidence", [])
            if item.get("turn", {}).get("source_id") == july.record_id
        ]
        source_local["evidence"].append(
            {
                "role": "context",
                "turn": {"source_id": "foreign-context", "source_version": 1, "seg_index": 0},
                "span": {"start": 0, "end": 1, "quote": "context"},
                "source_family": "other",
            }
        )
        context_dependent = persisted.model_copy(
            update={
                "source": persisted.source.model_copy(update={"parents": [july.record_id]}),
                "fingerprint": persisted.fingerprint.model_copy(update={"envelope": source_local}),
            }
        )
        assert independent_source(context_dependent) is None
        malformed = persisted.model_copy(
            update={
                "fingerprint": persisted.fingerprint.model_copy(
                    update={
                        "restatement_support": persisted.fingerprint.restatement_support.model_copy(
                            update={"claim_digest": "0" * 64}
                        )
                    }
                )
            }
        )
        assert not valid_proof(malformed)
        if lineage_table:
            edges = (await eng._storage.lineage_parents([claim.record_id])).get(claim.record_id, [])
            june_edge = next(edge for edge in edges if edge.parent == june.record_id)
            # A source may have binding evidence alongside its own support. Under a valid
            # independent proof that source-local role cannot make the whole claim conjunctive.
            await eng._storage.delete_lineage([june_edge])
            await eng._storage.add_lineage([june_edge.model_copy(update={"role": "binding"})], "a")
        await eng.forget(june.record_id, namespace="a", hard=True)
        [claim] = by_predicate(await claims(eng), "resides_in")
        assert claim.object == "Sweden" and july.record_id in claim.sources
        record = await eng._storage.get_record(claim.record_id)
        assert record is not None and record.fingerprint.restatement_support is not None
        assert set(record.fingerprint.restatement_support.source_ids) == {
            june.record_id,
            july.record_id,
        }
        envelope = SemanticEnvelope.model_validate(record.fingerprint.envelope)
        assert not _has_quote_for(record.fingerprint.model_dump(mode="json"), june.record_id)
        july_quotes = [
            item.span.quote
            for item in envelope.evidence
            if item.turn is not None
            and item.turn.source_id == july.record_id
            and item.span is not None
        ]
        assert july_quotes and any(july_quotes)
        for ev in await eng._storage.read_events():
            assert not _has_quote_for(ev.payload, june.record_id)
        await eng.forget(july.record_id, namespace="a", hard=True)
        assert not by_predicate(await claims(eng), "resides_in")
        await assert_replay(eng, ["Asha", "Sweden"])
        await eng.stop()
        eng = await _boot(monkeypatch, db_path, lineage_table=lineage_table)
        assert not by_predicate(await claims(eng), "resides_in")
        await assert_replay(eng, ["Asha", "Sweden"])
    finally:
        await eng.stop()


@pytest.mark.parametrize("lineage_table", [False, True])
@pytest.mark.parametrize("invalid_proof", ["missing", "malformed"])
async def test_f5_multi_parent_without_valid_proof_stays_conjunctive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lineage_table: bool, invalid_proof: str
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f5-unproven.db", lineage_table=lineage_table)
    try:
        [june] = await send(
            eng, [msg("u-1", "Asha", "I moved to Sweden four years ago.", "2026-06-10T09:00Z")]
        )
        await send(
            eng,
            [msg("u-2", "Asha", "Yes, I moved to Sweden four years ago.", "2026-07-12T09:00Z")],
        )
        [claim] = by_predicate(await claims(eng), "resides_in")
        record = await eng._storage.get_record(claim.record_id)
        assert record is not None
        proof = record.fingerprint.restatement_support
        if invalid_proof == "missing":
            forged_proof = None
        else:
            assert proof is not None
            forged_proof = proof.model_copy(update={"claim_digest": "0" * 64})
        unproven = record.model_copy(
            update={
                "fingerprint": record.fingerprint.model_copy(
                    update={"restatement_support": forged_proof}
                )
            }
        )
        await eng._append_and_project(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace="a",
                actor="system",
                payload={"record": unproven.model_dump(mode="json")},
            )
        )
        await eng.forget(june.record_id, namespace="a", hard=True)
        assert not by_predicate(await claims(eng), "resides_in")
        assert any(entry["record_id"] == claim.record_id for entry in eng.erasure_queue())
    finally:
        await eng.stop()


@pytest.mark.parametrize("lineage_table", [False, True])
async def test_f5_missing_source_row_does_not_count_as_remaining_support(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lineage_table: bool
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f5-missing-source.db", lineage_table=lineage_table)
    try:
        [june] = await send(
            eng, [msg("m-1", "Asha", "I moved to Sweden four years ago.", "2026-06-10T09:00Z")]
        )
        [july] = await send(
            eng,
            [msg("m-2", "Asha", "Yes, I moved to Sweden four years ago.", "2026-07-12T09:00Z")],
        )
        [claim] = by_predicate(await claims(eng), "resides_in")
        # Model a stale lineage parent row: it was never a usable remaining source.
        await eng._storage.delete_record(july.record_id)
        await eng.forget(june.record_id, namespace="a", hard=True)
        assert not by_predicate(await claims(eng), "resides_in")
        assert await eng._storage.get_record(claim.record_id) is None
    finally:
        await eng.stop()


def _has_quote_for(node: object, source_id: str) -> bool:
    if isinstance(node, dict):
        turn = node.get("turn")
        span = node.get("span")
        if (
            isinstance(turn, dict)
            and turn.get("source_id") == source_id
            and isinstance(span, dict)
            and span.get("quote")
        ):
            return True
        return any(_has_quote_for(value, source_id) for value in node.values())
    if isinstance(node, list):
        return any(_has_quote_for(value, source_id) for value in node)
    return False


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
        assert set(origin.sources) == {june.record_id, july.record_id}
        stored = await eng._storage.get_record(origin.record_id)
        assert stored is not None and stored.fingerprint.restatement_support is None
        assert origin.time_precision == "year"  # approximately 2022, never an exact day
        assert origin.known_at is not None and origin.known_at > june_known
        assert not by_predicate(await claims(eng), "move_destination")
        assert await semantic_hits(eng, QUERY, recorded_before=june_known) == []
        await assert_replay(eng, ["Asha", "Sweden"])
        await assert_erased(eng, july, [], QUERY)
        assert not by_predicate(await claims(eng), "move_origin")
    finally:
        await eng.stop()


@pytest.mark.parametrize(
    ("earlier", "later", "sender"),
    [
        ("I moved here from somewhere four years ago.", JULY, "Asha"),
        (JUNE, "Sweden is a lovely country.", "Asha"),
        (JUNE, "The home country I mentioned is Sweden, not Norway.", "Asha"),
        (JUNE, JULY, "Ben"),
    ],
)
async def test_f5_link_does_not_supply_missing_or_foreign_meaning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    earlier: str,
    later: str,
    sender: str,
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f5-insufficient.db")
    try:
        await send(eng, [msg("a-1", "Asha", earlier, "2026-06-10T09:00Z")])
        await send(eng, [msg("a-2", sender, later, "2026-07-12T09:00Z", reply_to="a-1")])
        assert by_predicate(await claims(eng), "move_origin") == []
    finally:
        await eng.stop()


async def test_f5_erasing_earlier_turn_removes_conjunctive_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "f5-earlier-erased.db"
    eng = await _boot(monkeypatch, db_path)
    try:
        [june] = await send(eng, [msg("e-1", "Asha", JUNE, "2026-06-10T09:00Z")])
        await send(eng, [msg("e-2", "Asha", JULY, "2026-07-12T09:00Z", reply_to="e-1")])
        assert len(by_predicate(await claims(eng), "move_origin")) == 1
        await eng.forget(june.record_id, namespace="a", hard=True)
        assert by_predicate(await claims(eng), "move_origin") == []
        await eng.stop()
        eng = await _boot(monkeypatch, db_path)
        assert by_predicate(await claims(eng), "move_origin") == []
        await eng.activate_derivation_policy(
            "a", policy_revision="f5:erasure", registry_digest="f5:erasure", runner_quiesced=True
        )
        await eng.flush_windows("a")
        assert by_predicate(await claims(eng), "move_origin") == []
    finally:
        await eng.stop()


async def test_f5_replaced_parent_during_deposit_cannot_authorise_stale_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f5-replaced-parent.db")
    try:
        [june] = await send(eng, [msg("replace-1", "Asha", JUNE, "2026-06-10T09:00Z")])
        deposit = eng._windows._deposit_fact
        assert deposit is not None
        replaced = False

        async def replace_before_deposit(*args: object, **kwargs: object) -> object:
            nonlocal replaced
            if not replaced and kwargs.get("envelope") is not None:
                replaced = True
                changed = june.model_copy(
                    update={
                        "version": june.version + 1,
                        "content": "I never moved from my home country.",
                    }
                )
                await eng._append_and_project(
                    MemoryEvent(
                        kind=EventKind.WRITE,
                        namespace="a",
                        actor="system",
                        payload={"record": changed.model_dump(mode="json")},
                    )
                )
            return await deposit(*args, **kwargs)

        monkeypatch.setattr(eng._windows, "_deposit_fact", replace_before_deposit)
        await send(
            eng,
            [msg("replace-2", "Asha", JULY, "2026-07-12T09:00Z", reply_to="replace-1")],
        )
        assert replaced
        assert by_predicate(await claims(eng), "move_origin") == []
    finally:
        await eng.stop()


@pytest.mark.parametrize("changed_source", ["parent", "reply"])
async def test_f5_deposit_rechecks_both_full_source_snapshots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, changed_source: str
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f5-snapshot.db")
    try:
        [june] = await send(eng, [msg("snapshot-1", "Asha", JUNE, "2026-06-10T09:00Z")])
        deposit = eng._windows._deposit_fact
        assert deposit is not None
        changed = False

        async def change_before_deposit(*args: object, **kwargs: object) -> object:
            nonlocal changed
            if not changed and kwargs.get("envelope") is not None:
                changed = True
                parents = args[4]
                assert isinstance(parents, list)
                source_id = (
                    june.record_id
                    if changed_source == "parent"
                    else next(source_id for source_id in parents if source_id != june.record_id)
                )
                source = await eng._storage.get_record(source_id)
                assert source is not None
                changed_record = source.model_copy(
                    update={
                        # A same-version replacement also changes its source/message hash.
                        "content": "I never moved from my home country.",
                    }
                )
                await eng._append_and_project(
                    MemoryEvent(
                        kind=EventKind.WRITE,
                        namespace="a",
                        actor="system",
                        payload={"record": changed_record.model_dump(mode="json")},
                    )
                )
            return await deposit(*args, **kwargs)

        monkeypatch.setattr(eng._windows, "_deposit_fact", change_before_deposit)
        await send(
            eng,
            [msg("snapshot-2", "Asha", JULY, "2026-07-12T09:00Z", reply_to="snapshot-1")],
        )
        assert changed
        assert by_predicate(await claims(eng), "move_origin") == []
    finally:
        await eng.stop()


async def test_f5_changed_reply_link_during_deposit_cannot_authorise_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f5-reply-link.db")
    try:
        [june] = await send(eng, [msg("link-1", "Asha", JUNE, "2026-06-10T09:00Z")])
        deposit = eng._windows._deposit_fact
        assert deposit is not None
        changed = False

        async def change_link_before_deposit(*args: object, **kwargs: object) -> object:
            nonlocal changed
            if not changed and kwargs.get("envelope") is not None:
                changed = True
                parents = args[4]
                assert isinstance(parents, list)
                reply_id = next(source_id for source_id in parents if source_id != june.record_id)
                reply = await eng._storage.get_record(reply_id)
                assert reply is not None and isinstance(reply.source.message, dict)
                message = {**reply.source.message, "reply_to": "different-parent"}
                changed_reply = reply.model_copy(
                    update={"source": reply.source.model_copy(update={"message": message})}
                )
                # The tag still names June, while the canonical message now names
                # another parent. Version, text, and the generic source hash agree;
                # the deposit door must reject this conflicting reply identity.
                assert changed_reply.tags == reply.tags
                assert (
                    source_binding(changed_reply).binding_hash == source_binding(reply).binding_hash
                )
                await eng._append_and_project(
                    MemoryEvent(
                        kind=EventKind.WRITE,
                        namespace="a",
                        actor="system",
                        payload={"record": changed_reply.model_dump(mode="json")},
                    )
                )
            return await deposit(*args, **kwargs)

        monkeypatch.setattr(eng._windows, "_deposit_fact", change_link_before_deposit)
        await send(
            eng,
            [msg("link-2", "Asha", JULY, "2026-07-12T09:00Z", reply_to="link-1")],
        )
        assert changed
        assert by_predicate(await claims(eng), "move_origin") == []
    finally:
        await eng.stop()


@pytest.mark.parametrize(
    "changed_input",
    ["source_at", "source_tz", "derivation_tag", "recorded_at"],
)
async def test_f5_changed_parent_rule_input_during_deposit_cannot_authorise_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, changed_input: str
) -> None:
    """Every input used to bind the earlier turn must survive through deposit."""
    eng = await _boot(monkeypatch, tmp_path / "f5-parent-input.db")
    try:
        [june] = await send(eng, [msg("input-1", "Asha", JUNE, "2026-06-10T09:00Z")])
        deposit = eng._windows._deposit_fact
        assert deposit is not None
        changed = False

        async def change_before_deposit(*args: object, **kwargs: object) -> object:
            nonlocal changed
            if not changed and kwargs.get("envelope") is not None:
                changed = True
                parents = args[4]
                assert isinstance(parents, list)
                reply_id = next(source_id for source_id in parents if source_id != june.record_id)
                reply = await eng._storage.get_record(reply_id)
                parent = await eng._storage.get_record(june.record_id)
                assert reply is not None and parent is not None
                if changed_input == "source_at":
                    replacement = parent.model_copy(
                        update={"source_at": parent.source_at.replace(year=2025)}
                    )
                elif changed_input == "source_tz":
                    replacement = parent.model_copy(update={"source_tz": "+14:00"})
                elif changed_input == "derivation_tag":
                    replacement = parent.model_copy(
                        update={"tags": [*parent.tags, "derive_use:excluded"]}
                    )
                else:
                    replacement = parent.model_copy(
                        update={"recorded_at": reply.recorded_at.replace(year=2027)}
                    )
                assert replacement.version == parent.version
                assert replacement.content == parent.content
                assert (
                    source_binding(replacement).binding_hash == source_binding(parent).binding_hash
                )
                await eng._append_and_project(
                    MemoryEvent(
                        kind=EventKind.WRITE,
                        namespace="a",
                        actor="system",
                        payload={"record": replacement.model_dump(mode="json")},
                    )
                )
            return await deposit(*args, **kwargs)

        monkeypatch.setattr(eng._windows, "_deposit_fact", change_before_deposit)
        await send(
            eng,
            [msg("input-2", "Asha", JULY, "2026-07-12T09:00Z", reply_to="input-1")],
        )
        assert changed
        assert by_predicate(await claims(eng), "move_origin") == []
    finally:
        await eng.stop()
