"""A fenced rules window must undo the correction closure it deposited."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from scenario_harness import boot, claims, edges, edges_of, msg, read_claims, send, settle

from memspine.ingest.windows import _remove_window_derived
from memspine.store.events import EventKind, MemoryEvent
from memspine.workers.windows import WINDOW_STAGE


@pytest.mark.parametrize("fence_kind", ["outbox", "soft_forget"])
async def test_fenced_correction_restores_its_predecessor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fence_kind: str
) -> None:
    db = tmp_path / f"compensated-correction-{fence_kind}.db"
    eng = await boot(monkeypatch, db)
    try:
        [prior_source] = await send(
            eng, [msg("prior", "Asha", "I live in Lisbon.", "2026-10-01T09:00Z")]
        )
        await settle(eng)
        [prior] = await claims(eng)
        [correction] = await eng.ingest_messages(
            [
                msg(
                    "fix",
                    "Asha",
                    "Correction, I meant Coimbra, not Lisbon.",
                    "2026-10-01T09:05Z",
                    reply_to="prior",
                )
            ],
            namespace="a",
            session_id="s1",
            conversation_id="s1",
        )
        pipeline = eng._windows
        assert pipeline is not None and pipeline._deposit_fact is not None
        assert pipeline._outbox is not None
        real_deposit = pipeline._deposit_fact
        deposited: list[Any] = []

        async def deposit(*args: Any, **kwargs: Any) -> object:
            result = await real_deposit(*args, **kwargs)
            deposited.append(result)
            if result is not None and len(deposited) == 1:
                if fence_kind == "soft_forget":
                    await eng.forget(correction.record_id, namespace="a", hard=False)
                else:
                    await pipeline._outbox.erase_source(
                        pipeline._scope("a"), WINDOW_STAGE, correction.record_id
                    )
            return result

        pipeline._deposit_fact = deposit
        await eng.flush_windows("a")
        assert deposited[0] is not None
        assert pipeline._counts["windows_compensated"] == 1
        [current] = await claims(eng)
        assert current.record_id == prior.record_id
        assert current.sources == (prior_source.record_id,)
        assert current.status == "active"
        assert await read_claims(eng, "Where does Asha live") == ["Asha resides_in Lisbon"]
        compiled = edges_of(await edges(eng), prior.record_id)
        assert compiled and all(e.claim_state == "current" for e in compiled)
        await eng.stop()
        eng = await boot(monkeypatch, db)
        [replayed] = await claims(eng)
        assert replayed.record_id == prior.record_id
        assert await read_claims(eng, "Where does Asha live") == ["Asha resides_in Lisbon"]
        assert edges_of(await edges(eng), prior.record_id)
    finally:
        await eng.stop()


@pytest.mark.parametrize("blocked_by", ["later_lifecycle", "lost_support", "live_chain"])
async def test_compensation_does_not_reopen_an_unowned_prior(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, blocked_by: str
) -> None:
    eng = await boot(monkeypatch, tmp_path / f"blocked-{blocked_by}.db")
    try:
        [prior_source] = await send(
            eng, [msg("prior", "Asha", "I live in Lisbon.", "2026-10-01T09:00Z")]
        )
        await settle(eng)
        [prior] = await claims(eng)
        await send(
            eng,
            [
                msg(
                    "fix",
                    "Asha",
                    "Correction, I meant Coimbra, not Lisbon.",
                    "2026-10-01T09:05Z",
                    reply_to="prior",
                )
            ],
        )
        await settle(eng)
        [successor] = await claims(eng)
        successor_record = await eng._storage.get_record(successor.record_id)
        assert successor_record is not None
        [tag] = [value for value in successor_record.tags if value.startswith("window:")]
        if blocked_by == "later_lifecycle":
            current_prior = await eng._storage.get_record(prior.record_id)
            assert current_prior is not None and current_prior.superseded_at is not None
            changed = current_prior.model_copy(
                update={"superseded_at": current_prior.superseded_at + timedelta(seconds=1)}
            )
            await eng._append_and_project(
                MemoryEvent(
                    kind=EventKind.WRITE,
                    namespace="a",
                    actor="system",
                    payload={"record": changed.model_dump(mode="json")},
                )
            )
        elif blocked_by == "lost_support":
            await eng.forget(prior_source.record_id, namespace="a", hard=True)
        else:
            await send(
                eng,
                [
                    msg(
                        "later",
                        "Asha",
                        "Correction, I meant Porto, not Coimbra.",
                        "2026-10-01T09:10Z",
                        reply_to="fix",
                    )
                ],
            )
            await settle(eng)
        await _remove_window_derived(eng)("a", tag.removeprefix("window:"))
        assert not [claim for claim in await claims(eng) if claim.record_id == prior.record_id]
        if blocked_by == "live_chain":
            [remaining] = await claims(eng)
            assert remaining.object == "Porto"
        assert await _restore_count(eng, prior.record_id) == 0
    finally:
        await eng.stop()


@pytest.mark.parametrize("proof_valid", [True, False])
async def test_compensation_preserves_only_valid_independent_committed_support(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, proof_valid: bool
) -> None:
    eng = await boot(monkeypatch, tmp_path / f"independent-successor-{proof_valid}.db")
    try:
        await send(eng, [msg("prior", "Asha", "I live in Lisbon.", "2026-10-01T09:00Z")])
        await settle(eng)
        await send(
            eng,
            [
                msg(
                    "fix",
                    "Asha",
                    "Correction, I meant Coimbra, not Lisbon.",
                    "2026-10-01T09:05Z",
                    reply_to="prior",
                )
            ],
        )
        await settle(eng)
        [successor] = await claims(eng)
        record = await eng._storage.get_record(successor.record_id)
        assert record is not None
        [tag] = [value for value in record.tags if value.startswith("window:")]
        await send(eng, [msg("independent", "Asha", "I live in Coimbra.", "2026-10-01T09:10Z")])
        await settle(eng)
        [supported] = await claims(eng)
        assert supported.record_id == successor.record_id
        if not proof_valid:
            current = await eng._storage.get_record(successor.record_id)
            assert current is not None and current.fingerprint.restatement_support is not None
            forged = current.fingerprint.restatement_support.model_copy(
                update={"claim_digest": "0" * 64}
            )
            invalid = current.model_copy(
                update={
                    "fingerprint": current.fingerprint.model_copy(
                        update={"restatement_support": forged}
                    )
                }
            )
            await eng._append_and_project(
                MemoryEvent(
                    kind=EventKind.WRITE,
                    namespace="a",
                    actor="system",
                    payload={"record": invalid.model_dump(mode="json")},
                )
            )
        await _remove_window_derived(eng)("a", tag.removeprefix("window:"))
        if proof_valid:
            [remaining] = await claims(eng)
            assert remaining.record_id == successor.record_id
            assert remaining.object == "Coimbra"
        else:
            assert await claims(eng) == []
    finally:
        await eng.stop()


async def test_compensation_does_not_erase_support_committed_before_forget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await boot(monkeypatch, tmp_path / "support-between-check-and-forget.db")
    try:
        await send(eng, [msg("prior", "Asha", "I live in Lisbon.", "2026-10-01T09:00Z")])
        await settle(eng)
        await send(
            eng,
            [
                msg(
                    "fix",
                    "Asha",
                    "Correction, I meant Coimbra, not Lisbon.",
                    "2026-10-01T09:05Z",
                    reply_to="prior",
                )
            ],
        )
        await settle(eng)
        [successor] = await claims(eng)
        record = await eng._storage.get_record(successor.record_id)
        assert record is not None
        [tag] = [value for value in record.tags if value.startswith("window:")]
        original_list = eng._storage.list_records
        candidate_listed = asyncio.Event()
        continue_compensation = asyncio.Event()

        async def pause_after_listing(*args: Any, **kwargs: Any) -> Any:
            rows = await original_list(*args, **kwargs)
            if args[:2] == ("a", "semantic") and not candidate_listed.is_set():
                candidate_listed.set()
                await continue_compensation.wait()
            return rows

        with monkeypatch.context() as fault:
            fault.setattr(eng._storage, "list_records", pause_after_listing)
            compensation = asyncio.create_task(
                _remove_window_derived(eng)("a", tag.removeprefix("window:"))
            )
            await candidate_listed.wait()
            try:
                await send(
                    eng,
                    [msg("independent", "Asha", "I live in Coimbra.", "2026-10-01T09:10Z")],
                )
                await settle(eng)
            finally:
                continue_compensation.set()
            assert await compensation == 0
        [remaining] = await claims(eng)
        assert remaining.record_id == successor.record_id
        assert remaining.object == "Coimbra"
    finally:
        await eng.stop()


async def test_compensation_serializes_a_later_support_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import memspine.trust.pipeline.erasure_state as erasure_state

    eng = await boot(monkeypatch, tmp_path / "support-after-lock.db")
    try:
        await send(eng, [msg("prior", "Asha", "I live in Lisbon.", "2026-10-01T09:00Z")])
        await send(
            eng,
            [
                msg(
                    "fix",
                    "Asha",
                    "Correction, I meant Coimbra, not Lisbon.",
                    "2026-10-01T09:05Z",
                    reply_to="prior",
                )
            ],
        )
        [successor] = await claims(eng)
        record = await eng._storage.get_record(successor.record_id)
        assert record is not None
        [tag] = [value for value in record.tags if value.startswith("window:")]
        entered = asyncio.Event()
        release = asyncio.Event()
        original_forget_locked = erasure_state._forget_under_lock

        async def pause_after_guard(*args: Any, **kwargs: Any) -> Any:
            entered.set()
            await release.wait()
            return await original_forget_locked(*args, **kwargs)

        with monkeypatch.context() as fault:
            fault.setattr(erasure_state, "_forget_under_lock", pause_after_guard)
            compensation = asyncio.create_task(
                _remove_window_derived(eng)("a", tag.removeprefix("window:"))
            )
            await entered.wait()
            support = asyncio.create_task(
                send(eng, [msg("independent", "Asha", "I live in Coimbra.", "2026-10-01T09:10Z")])
            )
            await asyncio.sleep(0)
            assert not support.done()
            release.set()
            assert await compensation == 1
            await support
        assert await eng._storage.get_record(successor.record_id) is None
    finally:
        await eng.stop()


async def _restore_count(eng: Any, prior_id: str) -> int:
    count = 0
    offset = 0
    while True:
        batch = await eng._storage.read_events(after_seq=offset, limit=1000)
        if not batch:
            return count
        count += sum(
            event.kind is EventKind.WRITE
            and event.payload.get("correction_erasure_restore", {}).get("prior_id") == prior_id
            for event in batch
        )
        assert batch[-1].seq is not None
        offset = batch[-1].seq


async def _compiled_link_count(eng: Any, prior_id: str) -> int:
    count = 0
    offset = 0
    while True:
        batch = await eng._storage.read_events(after_seq=offset, limit=1000)
        if not batch:
            return count
        count += sum(
            event.kind is EventKind.LINK
            and event.payload.get("reason") == "edge_compiler"
            and event.payload.get("props", {}).get("record_id") == prior_id
            for event in batch
        )
        assert batch[-1].seq is not None
        offset = batch[-1].seq


async def _intent_count(eng: Any, successor_id: str) -> int:
    count = 0
    offset = 0
    while True:
        batch = await eng._storage.read_events(after_seq=offset, limit=1000)
        if not batch:
            return count
        count += sum(
            event.kind is EventKind.MARKER
            and event.payload.get("marker") == "window_correction_compensation"
            and event.payload.get("successor_id") == successor_id
            for event in batch
        )
        assert batch[-1].seq is not None
        offset = batch[-1].seq


@pytest.mark.parametrize("interrupt_at", ["after_forget", "after_restore_write"])
async def test_compensation_restart_finishes_after_successor_forget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, interrupt_at: str
) -> None:
    import memspine.trust.pipeline.erasure_state as erasure_state

    db = tmp_path / f"compensation-restart-{interrupt_at}.db"
    eng = await boot(monkeypatch, db)
    try:
        await send(eng, [msg("prior", "Asha", "I live in Lisbon.", "2026-10-01T09:00Z")])
        await settle(eng)
        [prior] = await claims(eng)
        await send(
            eng,
            [
                msg(
                    "fix",
                    "Asha",
                    "Correction, I meant Coimbra, not Lisbon.",
                    "2026-10-01T09:05Z",
                    reply_to="prior",
                )
            ],
        )
        await settle(eng)
        [successor] = await claims(eng)
        successor_record = await eng._storage.get_record(successor.record_id)
        assert successor_record is not None
        [tag] = [tag for tag in successor_record.tags if tag.startswith("window:")]

        with monkeypatch.context() as fault:
            if interrupt_at == "after_forget":

                async def interrupt(*args: Any) -> None:
                    raise RuntimeError("after successor forget")

                fault.setattr(erasure_state, "_restore_window_compensation", interrupt)
            else:
                original_append = eng._append_and_project
                restored_write = False

                async def interrupt_link(event: MemoryEvent) -> None:
                    nonlocal restored_write
                    if event.kind is EventKind.WRITE:
                        restored_write |= "correction_erasure_restore" in event.payload
                    if restored_write and event.kind is EventKind.LINK:
                        raise RuntimeError("after restore write")
                    await original_append(event)

                fault.setattr(eng, "_append_and_project", interrupt_link)
            with pytest.raises(RuntimeError, match="after successor forget|after restore write"):
                await _remove_window_derived(eng)("a", tag.removeprefix("window:"))
        assert await _restore_count(eng, prior.record_id) == (
            0 if interrupt_at == "after_forget" else 1
        )
        await eng.stop()
        eng = await boot(monkeypatch, db)
        [restored] = await claims(eng)
        assert restored.record_id == prior.record_id
        assert await _restore_count(eng, prior.record_id) == 1
        assert await read_claims(eng, "Where does Asha live") == ["Asha resides_in Lisbon"]
        assert edges_of(await edges(eng), prior.record_id)
        links = await _compiled_link_count(eng, prior.record_id)
        assert links > 0
        assert await _remove_window_derived(eng)("a", tag.removeprefix("window:")) == 0
        await eng.stop()
        eng = await boot(monkeypatch, db)
        assert await _restore_count(eng, prior.record_id) == 1
        assert await _compiled_link_count(eng, prior.record_id) == links
    finally:
        await eng.stop()


async def test_compensation_intent_alone_does_not_restore(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import memspine.trust.pipeline.erasure_state as erasure_state

    db = tmp_path / "intent-before-forget.db"
    eng = await boot(monkeypatch, db)
    try:
        await send(eng, [msg("prior", "Asha", "I live in Lisbon.", "2026-10-01T09:00Z")])
        await send(
            eng,
            [
                msg(
                    "fix",
                    "Asha",
                    "Correction, I meant Coimbra, not Lisbon.",
                    "2026-10-01T09:05Z",
                    reply_to="prior",
                )
            ],
        )
        [successor] = await claims(eng)
        record = await eng._storage.get_record(successor.record_id)
        assert record is not None
        [tag] = [value for value in record.tags if value.startswith("window:")]

        async def interrupt(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("before successor forget")

        with monkeypatch.context() as fault:
            fault.setattr(erasure_state, "_forget_under_lock", interrupt)
            with pytest.raises(RuntimeError, match="before successor forget"):
                await _remove_window_derived(eng)("a", tag.removeprefix("window:"))
        await eng.stop()
        eng = await boot(monkeypatch, db)
        [current] = await claims(eng)
        assert current.record_id == successor.record_id
        assert await read_claims(eng, "Where does Asha live") == ["Asha resides_in Coimbra"]
        assert await _intent_count(eng, successor.record_id) == 1
        await _remove_window_derived(eng)("a", tag.removeprefix("window:"))
        assert await _intent_count(eng, successor.record_id) == 1
        [restored] = await claims(eng)
        assert restored.object == "Lisbon"
    finally:
        await eng.stop()
