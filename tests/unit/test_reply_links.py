"""G27 (plan v3.2, ADR-061): ``reply_to`` links and their replay expansion."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_window_write import FakeWindowLLM, _facts

from memspine import Engine
from memspine.config import constants
from memspine.config.schema import MemspineConfig
from memspine.exceptions import ConflictError, MemspineError
from memspine.ingest import gateway
from memspine.ingest.message_parent import ReplyParentUnavailable
from memspine.ingest.message_store import lookup_messages
from memspine.services.llm.base import LLMRouter
from memspine.store.events import EventKind

T0 = datetime(2023, 5, 8, 13, 0, tzinfo=UTC)


def _engine(
    associative: bool = False,
    *,
    path: Path | None = None,
    write: dict[str, Any] | None = None,
    firewall: dict[str, Any] | None = None,
    episodic_policies: dict[str, Any] | None = None,
    **read: Any,
) -> Engine:
    memories: dict[str, Any] = {"episodic": {"enabled": True, "policies": episodic_policies or {}}}
    if associative:
        memories |= {"semantic": {"enabled": True}, "associative": {"enabled": True}}
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": str(path) if path else ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories=memories,
        read={"hybrid": False, "record_access": False, **read},
        write=write or {},
        firewall=firewall or {},
    )


async def test_write_reply_to_tags_the_reply_and_refuses_unknown_targets() -> None:
    eng = _engine()
    await eng.start()
    try:
        asked = await eng.write("Which venue should we book?", namespace="a")
        reply = await eng.write(
            "The Old Mill, it has parking.", namespace="a", reply_to=asked.record_id
        )
        assert f"{constants.REPLY_TO_PREFIX}{asked.record_id}" in reply.tags
        with pytest.raises(ConflictError):
            await eng.write("orphan", namespace="a", reply_to="no-such-id")
        with pytest.raises(ConflictError):
            await eng.write("foreign", namespace="b", reply_to=asked.record_id)
    finally:
        await eng.stop()


async def test_ingest_messages_reply_to_by_index_or_id() -> None:
    eng = _engine()
    await eng.start()
    try:
        prior = await eng.ingest_messages(
            [
                {
                    "message_id": "prior-note",
                    "session_id": "s1",
                    "conversation_id": "venue-thread",
                    "role": "user",
                    "content": "Earlier note about the venue",
                }
            ],
            namespace="a",
            session_id="s1",
            conversation_id="venue-thread",
        )
        first = prior[0]
        msgs = [
            {
                "message_id": "venue-question",
                "role": "user",
                "content": "Which venue should we book?",
            },
            {
                "message_id": "venue-answer",
                "role": "assistant",
                "content": "The Old Mill.",
                "reply_to": "venue-question",
            },
            {
                "message_id": "venue-followup",
                "role": "user",
                "content": "Same as my note?",
                "reply_to": "prior-note",
            },
        ]
        records = await eng.ingest_messages(
            msgs, namespace="a", session_id="s1", conversation_id="venue-thread"
        )
        assert f"{constants.REPLY_TO_PREFIX}{records[0].record_id}" in records[1].tags
        assert f"{constants.REPLY_TO_PREFIX}{first.record_id}" in records[2].tags
        with pytest.raises(ValueError, match="reply_to"):
            await eng.ingest_messages(
                [{"role": "user", "content": "x", "reply_to": 3}],  # type: ignore[list-item]
                namespace="a",
            )
    finally:
        await eng.stop()


@pytest.mark.parametrize("hard", [False, True])
async def test_ingest_refuses_erased_stored_reply_parent_without_accepting_child(
    hard: bool,
) -> None:
    eng = _engine(associative=True)
    await eng.start()
    try:
        [parent] = await eng.ingest_messages(
            [{"message_id": "erased-parent", "role": "user", "content": "private question"}],
            namespace="a",
            session_id="parent-session",
            conversation_id="parent-conversation",
        )
        await eng.forget(parent.record_id, namespace="a", hard=hard)
        before = [r.record_id for r in await eng.timeline(namespace="a")]
        with pytest.raises(ValueError, match="reply parent"):
            await eng.ingest_messages(
                [
                    {
                        "message_id": "refused-child",
                        "role": "assistant",
                        "content": "answer",
                        "reply_to": "erased-parent",
                    }
                ],
                namespace="a",
            )
        ledger = await gateway.gateway_of(eng)._ledger()
        assert await lookup_messages(ledger, "a", ["refused-child"]) == {}
        assert [r.record_id for r in await eng.timeline(namespace="a")] == before
    finally:
        await eng.stop()


async def test_stored_reply_parent_visibility_uses_caller_actor_not_forged_sender() -> None:
    eng = _engine(write={"participants": "session"})
    await eng.start()
    try:
        [parent] = await eng.ingest_messages(
            [
                {
                    "message_id": "private-parent",
                    "role": "user",
                    "content": "question",
                    "sender": {"name": "Alice"},
                    "participants": ["Alice"],
                    "visibility": "private",
                }
            ],
            namespace="a",
            actor="Alice",
            session_id="s",
            conversation_id="c",
        )
        assert parent.source.message["message_id"] == "private-parent"
        child = {
            "message_id": "denied-child",
            "role": "assistant",
            "content": "answer",
            "reply_to": "private-parent",
            "sender": {"id": "Alice"},
        }
        with pytest.raises(ValueError, match="reply parent is unavailable"):
            await eng.ingest_messages([child], namespace="a", actor="Bob")
        ledger = await gateway.gateway_of(eng)._ledger()
        assert await lookup_messages(ledger, "a", ["denied-child"]) == {}
        [accepted] = await eng.ingest_messages(
            [{**child, "message_id": "allowed-child"}], namespace="a", actor="Alice"
        )
        assert accepted.source.message["session_id"] == "s"
        assert accepted.source.message["conversation_id"] == "c"
        assert accepted.source.message["reply_to"] == "private-parent"
    finally:
        await eng.stop()


async def test_cross_namespace_same_public_message_id_cannot_supply_reply_parent() -> None:
    eng = _engine()
    await eng.start()
    try:
        await eng.ingest_messages(
            [{"message_id": "shared-public-id", "role": "user", "content": "only in a"}],
            namespace="a",
            session_id="a-session",
            conversation_id="a-conversation",
        )
        with pytest.raises(ValueError, match="unresolved"):
            await eng.ingest_messages(
                [
                    {
                        "message_id": "b-child",
                        "role": "assistant",
                        "content": "answer",
                        "reply_to": "shared-public-id",
                    }
                ],
                namespace="b",
            )
        ledger = await gateway.gateway_of(eng)._ledger()
        assert await lookup_messages(ledger, "b", ["b-child"]) == {}
    finally:
        await eng.stop()


@pytest.mark.parametrize("skip_kind", ["role", "recall"])
async def test_same_batch_skipped_reply_parent_refuses_before_identity_reservation(
    skip_kind: str,
) -> None:
    parent = {
        "message_id": "skipped-parent",
        "role": "system" if skip_kind == "role" else "user",
        "content": (
            "injected context " + constants.UNTRUSTED_NOTE_MARKER
            if skip_kind == "recall"
            else "system instruction"
        ),
    }
    firewall = (
        {"skip_message_roles": ["system"]}
        if skip_kind == "role"
        else {"skip_injected_recall": True}
    )
    eng = _engine(firewall=firewall)
    await eng.start()
    try:
        child = {
            "message_id": "skipped-child",
            "role": "user",
            "content": "Reply to the dropped parent",
            "reply_to": "skipped-parent",
        }
        with pytest.raises(ReplyParentUnavailable):
            await eng.ingest_messages([parent, child], namespace="a")
        ledger = await gateway.gateway_of(eng)._ledger()
        assert await lookup_messages(ledger, "a", ["skipped-parent", "skipped-child"]) == {}
        assert await eng.timeline(namespace="a") == []
        [accepted] = await eng.ingest_messages(
            [{"message_id": "skipped-child", "role": "user", "content": "Now independent"}],
            namespace="a",
        )
        assert accepted.source.message["message_id"] == "skipped-child"
    finally:
        await eng.stop()


async def test_role_policy_reply_parent_recovers_after_post_commit_crash(
    tmp_path: Path,
) -> None:
    batch = [
        {"message_id": "sys-parent", "role": "system", "content": "system context"},
        {
            "message_id": "reply-child",
            "role": "user",
            "content": "follow-up",
            "reply_to": "sys-parent",
        },
    ]
    opts = {"write": {"role_policy": True}, "firewall": {"skip_message_roles": ["system"]}}
    clean = _engine(path=tmp_path / "clean.db", **opts)
    await clean.start()
    try:
        clean_records = await clean.ingest_messages(
            batch, namespace="a", session_id="s", conversation_id="c"
        )
        assert len(clean_records) == 2
        assert clean_records[1].source.message["reply_to"] == "sys-parent"
        assert f"{constants.REPLY_TO_PREFIX}{clean_records[0].record_id}" in clean_records[1].tags
    finally:
        await clean.stop()

    path = tmp_path / "crashed.db"
    first = _engine(path=path, **opts)
    await first.start()
    try:
        gw = gateway.gateway_of(first)

        def crash_after_commit(boundary: str) -> None:
            if boundary == "post_commit":
                raise RuntimeError("simulated post-commit crash")

        gw.fault = crash_after_commit
        with pytest.raises(RuntimeError, match="post-commit crash"):
            await first.ingest_messages(batch, namespace="a", session_id="s", conversation_id="c")
        ledger = await gw._ledger()
        rows = await lookup_messages(ledger, "a", ["sys-parent", "reply-child"])
        assert {row["intent_status"] for row in rows.values()} == {"accepted"}
        assert all(row["record_id"] is None for row in rows.values())
    finally:
        await first.stop()

    restarted = _engine(path=path, **opts)
    await restarted.start()
    try:
        ledger = await gateway.gateway_of(restarted)._ledger()
        rows = await lookup_messages(ledger, "a", ["sys-parent", "reply-child"])
        assert {row["intent_status"] for row in rows.values()} == {"applied"}
        parent_id = rows["sys-parent"]["record_id"]
        child_id = rows["reply-child"]["record_id"]
        assert parent_id and child_id
        timeline = await restarted.timeline(namespace="a")
        assert [r.record_id for r in timeline] == [parent_id, child_id]
        assert timeline[1].source.message["reply_to"] == "sys-parent"
        assert f"{constants.REPLY_TO_PREFIX}{parent_id}" in timeline[1].tags
        retried = await restarted.ingest_messages(
            batch, namespace="a", session_id="s", conversation_id="c"
        )
        assert [r.record_id for r in retried] == [parent_id, child_id]
        assert [r.record_id for r in await restarted.timeline(namespace="a")] == [
            parent_id,
            child_id,
        ]
    finally:
        await restarted.stop()


async def test_quarantined_stored_reply_parent_is_refused_before_acceptance() -> None:
    eng = _engine()
    await eng.start()
    try:
        [parent] = await eng.ingest_messages(
            [{"message_id": "held-parent", "role": "user", "content": "question"}],
            namespace="a",
            session_id="s",
            conversation_id="c",
        )
        await eng.quarantine(parent.record_id, namespace="a")
        with pytest.raises(ValueError, match="reply parent is unavailable"):
            await eng.ingest_messages(
                [
                    {
                        "message_id": "held-child",
                        "role": "assistant",
                        "content": "answer",
                        "reply_to": "held-parent",
                    }
                ],
                namespace="a",
            )
        ledger = await gateway.gateway_of(eng)._ledger()
        assert await lookup_messages(ledger, "a", ["held-child"]) == {}
    finally:
        await eng.stop()


async def test_concurrent_parent_forget_never_strands_reply_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng = _engine()
    await eng.start()
    accepted = asyncio.Event()
    resume = asyncio.Event()
    child_task: asyncio.Task[list[Any]] | None = None
    forget_task: asyncio.Task[None] | None = None
    try:
        [parent] = await eng.ingest_messages(
            [{"message_id": "race-parent", "role": "user", "content": "question"}],
            namespace="a",
            session_id="s",
            conversation_id="c",
        )
        real_submit = gateway.ingest_resolved_messages

        async def paused_submit(*args: Any, **kwargs: Any) -> Any:
            accepted.set()
            await resume.wait()
            return await real_submit(*args, **kwargs)

        monkeypatch.setattr(gateway, "ingest_resolved_messages", paused_submit)
        child_task = asyncio.create_task(
            eng.ingest_messages(
                [
                    {
                        "message_id": "race-child",
                        "role": "assistant",
                        "content": "answer",
                        "reply_to": "race-parent",
                    }
                ],
                namespace="a",
            )
        )
        await asyncio.wait_for(accepted.wait(), timeout=2)
        forget_task = asyncio.create_task(eng.forget(parent.record_id, namespace="a"))
        try:
            await asyncio.wait_for(asyncio.shield(forget_task), timeout=0.15)
            forgotten_before_acceptance = True
        except TimeoutError:
            forgotten_before_acceptance = False
        resume.set()
        result = await asyncio.gather(child_task, return_exceptions=True)
        await forget_task
        ledger = await gateway.gateway_of(eng)._ledger()
        if forgotten_before_acceptance:
            assert isinstance(result[0], Exception)
            rows = await lookup_messages(ledger, "a", ["race-child"])
            assert rows["race-child"]["intent_status"] == "failed"
            assert rows["race-child"]["record_id"] is None
            assert await ledger.pending_intents() == []
            status = await eng.ingestion_status(["race-child"], namespace="a")
            assert status["race-child"].layers["L0"] == "failed"
            with pytest.raises(ValueError, match="reply parent is unavailable"):
                await eng.ingest_messages(
                    [
                        {
                            "message_id": "race-child",
                            "role": "assistant",
                            "content": "answer",
                            "reply_to": "race-parent",
                        }
                    ],
                    namespace="a",
                )
            with pytest.raises(ValueError, match="reply parent is unavailable"):
                await eng.ingest_messages(
                    [
                        {
                            "message_id": "race-child",
                            "role": "assistant",
                            "content": "changed",
                            "reply_to": "race-parent",
                        }
                    ],
                    namespace="a",
                )
        else:
            [child] = result[0]
            assert child.source.message["reply_to"] == "race-parent"
    finally:
        resume.set()
        if child_task is not None:
            await asyncio.gather(child_task, return_exceptions=True)
        if forget_task is not None:
            await asyncio.gather(forget_task, return_exceptions=True)
        await eng.stop()


async def test_permanent_reply_failure_keeps_only_landed_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "failed-prefix.db"
    eng = _engine(path=path)
    await eng.start()
    try:
        [parent] = await eng.ingest_messages(
            [{"message_id": "prefix-parent", "role": "user", "content": "question"}],
            namespace="a",
            session_id="s",
            conversation_id="c",
        )
        real_write = eng.write
        count = 0

        async def forget_after_first(*args: Any, **kwargs: Any) -> Any:
            nonlocal count
            result = await real_write(*args, **kwargs)
            count += 1
            if count == 1:
                await eng.forget(parent.record_id, namespace="a")
            return result

        monkeypatch.setattr(eng, "write", forget_after_first)
        batch = [
            {
                "message_id": f"prefix-child-{i}",
                "role": "assistant",
                "content": f"answer {i}",
                "reply_to": "prefix-parent",
            }
            for i in range(2)
        ]
        with pytest.raises(ReplyParentUnavailable):
            await eng.ingest_messages(batch, namespace="a")
        ledger = await gateway.gateway_of(eng)._ledger()
        rows = await lookup_messages(ledger, "a", ["prefix-child-0", "prefix-child-1"])
        assert [rows[mid]["intent_status"] for mid in rows] == ["failed", "failed"]
        failed_intent_id = rows["prefix-child-0"]["intent_id"]
        first_id = rows["prefix-child-0"]["record_id"]
        assert first_id and rows["prefix-child-1"]["record_id"] is None
        receipt = gateway.gateway_of(eng).last_receipt
        assert receipt is not None and receipt.record_ids == [first_id]
        assert receipt.readiness.raw_search == "failed"
        failed_receipt = (await ledger.intents_by_id([failed_intent_id]))[failed_intent_id][
            "receipt"
        ]
        assert await ledger.pending_intents() == []
        assert [r.record_id for r in await eng.timeline(namespace="a")] == [first_id]
    finally:
        await eng.stop()

    restarted = _engine(path=path)
    await restarted.start()
    try:
        ledger = await gateway.gateway_of(restarted)._ledger()
        assert await ledger.pending_intents() == []
        assert [r.record_id for r in await restarted.timeline(namespace="a")] == [first_id]
        with pytest.raises(ReplyParentUnavailable):
            await restarted.ingest_messages(batch, namespace="a")
        assert [r.record_id for r in await restarted.timeline(namespace="a")] == [first_id]
        [reply_to_prefix] = await restarted.ingest_messages(
            [
                {
                    "message_id": "reply-to-landed-prefix",
                    "role": "user",
                    "content": "Following up on the landed answer",
                    "reply_to": "prefix-child-0",
                }
            ],
            namespace="a",
        )
        assert reply_to_prefix.source.message["reply_to"] == "prefix-child-0"
        assert f"{constants.REPLY_TO_PREFIX}{first_id}" in reply_to_prefix.tags
        with pytest.raises(ReplyParentUnavailable):
            await restarted.ingest_messages(
                [
                    {
                        "message_id": "reply-to-failed-suffix",
                        "role": "user",
                        "content": "This parent was never written",
                        "reply_to": "prefix-child-1",
                    }
                ],
                namespace="a",
            )
        assert await lookup_messages(ledger, "a", ["reply-to-failed-suffix"]) == {}
        failed_row = (await ledger.intents_by_id([failed_intent_id]))[failed_intent_id]
        assert failed_row["status"] == "failed"
        assert failed_row["apply"] is None
        assert failed_row["receipt"] == failed_receipt
    finally:
        await restarted.stop()


async def test_failed_reply_batch_discovers_only_its_landed_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid raw prefix keeps its durable derivation obligation after the suffix fails."""
    path = tmp_path / "failed-prefix-outbox.db"

    async def router(
        config: MemspineConfig, hooks: object = None, wiring: object = None
    ) -> LLMRouter:
        return LLMRouter({"window_extract": FakeWindowLLM()})  # type: ignore[dict-item]

    monkeypatch.setattr("memspine.runtime.container.build_llm_router", router)

    def make_engine() -> Engine:
        return Engine(
            template="core",
            dotenv_path=None,
            deployment={"id": "test-deployment"},
            storage={
                "records": {
                    "provider": "sqlite",
                    "settings": {"path": str(path), "busy_timeout_s": 5.0},
                }
            },
            model={
                "embedding": {"provider": "hash", "settings": {}},
                "llm": {
                    "window_extract": {
                        "provider": "openai_compatible",
                        "timeout_s": 30.0,
                        "settings": {"api_base": "http://test.invalid/v1", "model": "fake/window"},
                    }
                },
            },
            memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
            write={"window_extract": "facts", "window_async": False},
            workers={"outbox": True},
        )

    eng = make_engine()
    await eng.start()
    try:
        [parent] = await eng.ingest_messages(
            [{"message_id": "outbox-parent", "role": "user", "content": "question"}],
            namespace="a",
            session_id="s",
            conversation_id="c",
        )
        real_write = eng.write
        count = 0

        async def forget_after_first(*args: Any, **kwargs: Any) -> Any:
            nonlocal count
            result = await real_write(*args, **kwargs)
            count += 1
            if count == 1:
                await eng.forget(parent.record_id, namespace="a")
            return result

        monkeypatch.setattr(eng, "write", forget_after_first)
        with pytest.raises(ReplyParentUnavailable):
            await eng.ingest_messages(
                [
                    {
                        "message_id": f"outbox-child-{i}",
                        "role": "assistant",
                        "content": f"Mira: booked venue {i}",
                        "reply_to": "outbox-parent",
                    }
                    for i in range(2)
                ],
                namespace="a",
            )
        ledger = await gateway.gateway_of(eng)._ledger()
        identities = await lookup_messages(ledger, "a", ["outbox-child-0", "outbox-child-1"])
        prefix_id = identities["outbox-child-0"]["record_id"]
        assert prefix_id and identities["outbox-child-1"]["record_id"] is None
        failed_intent_id = identities["outbox-child-0"]["intent_id"]
        with sqlite3.connect(path) as conn:
            payloads = [
                json.loads(row[0])
                for row in conn.execute(
                    "SELECT payload FROM outbox_item WHERE stage = 'window.discovery'"
                )
            ]
        failed_payloads = [p for p in payloads if p.get("intent_id") == failed_intent_id]
        assert len(failed_payloads) == 1
        assert failed_payloads[0]["manifest"]["source_record_ids"] == [prefix_id]
        await eng.flush_windows("a")
        facts = await _facts(eng)
        assert len(facts) == 1
        assert "booked venue 0" in facts[0].content
        assert "booked venue 1" not in facts[0].content
    finally:
        await eng.stop()

    restarted = make_engine()
    await restarted.start()
    try:
        await restarted.flush_windows("a")
        facts = await _facts(restarted)
        assert len(facts) == 1
        assert "booked venue 0" in facts[0].content
        assert "booked venue 1" not in facts[0].content
    finally:
        await restarted.stop()


async def test_erased_landed_prefix_is_not_relinked_by_failed_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng = _engine()
    await eng.start()
    try:
        [parent] = await eng.ingest_messages(
            [{"message_id": "erased-prefix-parent", "role": "user", "content": "question"}],
            namespace="a",
            session_id="s",
            conversation_id="c",
        )
        real_write = eng.write
        count = 0

        async def erase_after_first(*args: Any, **kwargs: Any) -> Any:
            nonlocal count
            written = await real_write(*args, **kwargs)
            count += 1
            if count == 1:
                await eng.forget(written.record_id, namespace="a", hard=True)
                await eng.forget(parent.record_id, namespace="a", hard=True)
            return written

        monkeypatch.setattr(eng, "write", erase_after_first)
        with pytest.raises(ReplyParentUnavailable):
            await eng.ingest_messages(
                [
                    {
                        "message_id": f"erased-prefix-child-{i}",
                        "role": "assistant",
                        "content": f"answer {i}",
                        "reply_to": "erased-prefix-parent",
                    }
                    for i in range(2)
                ],
                namespace="a",
            )
        ledger = await gateway.gateway_of(eng)._ledger()
        rows = await lookup_messages(
            ledger, "a", ["erased-prefix-child-0", "erased-prefix-child-1"]
        )
        assert rows["erased-prefix-child-0"]["erased"] is True
        assert rows["erased-prefix-child-0"]["record_id"] is None
        assert rows["erased-prefix-child-1"]["intent_status"] == "failed"
        assert rows["erased-prefix-child-1"]["record_id"] is None
        receipt = gateway.gateway_of(eng).last_receipt
        assert receipt is not None and receipt.record_ids == []
        assert await eng.timeline(namespace="a") == []
    finally:
        await eng.stop()


async def test_unmapped_child_tombstone_survives_failed_manifest_and_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "unmapped-erasure.db"
    first = _engine(path=path)
    await first.start()
    child_id: str | None = None
    try:
        await first.ingest_messages(
            [{"message_id": "interrupted-parent", "role": "user", "content": "question"}],
            namespace="a",
            session_id="s",
            conversation_id="c",
        )
        real_write = first.write
        real_append = first._append_and_project

        async def fail_manifest(event: Any) -> None:
            if event.kind is EventKind.MARKER and event.payload.get("marker") == "erasure_manifest":
                raise RuntimeError("interrupt before erasure manifest")
            await real_append(event)

        async def erase_unmapped_child(*args: Any, **kwargs: Any) -> Any:
            nonlocal child_id
            written = await real_write(*args, **kwargs)
            child_id = written.record_id
            monkeypatch.setattr(first, "_append_and_project", fail_manifest)
            await first.forget(child_id, namespace="a", hard=True, cascade=False)
            return written

        monkeypatch.setattr(first, "write", erase_unmapped_child)
        with pytest.raises(RuntimeError, match="interrupt before erasure manifest"):
            await first.ingest_messages(
                [
                    {
                        "message_id": "interrupted-child",
                        "role": "assistant",
                        "content": "answer",
                        "reply_to": "interrupted-parent",
                    }
                ],
                namespace="a",
            )
        assert child_id is not None
        ledger = await gateway.gateway_of(first)._ledger()
        identity = (await lookup_messages(ledger, "a", ["interrupted-child"]))["interrupted-child"]
        assert identity["erased"] is True and identity["record_id"] is None
        assert await first._require_started().get_record(child_id) is not None
    finally:
        monkeypatch.undo()
        await first.stop()

    restarted = _engine(path=path)
    await restarted.start()
    try:
        assert child_id is not None
        ledger = await gateway.gateway_of(restarted)._ledger()
        identity = (await lookup_messages(ledger, "a", ["interrupted-child"]))["interrupted-child"]
        assert identity["erased"] is True and identity["record_id"] is None
        await restarted.forget(child_id, namespace="a", hard=True, cascade=False)
        assert await restarted._require_started().get_record(child_id) is None
        identity = (await lookup_messages(ledger, "a", ["interrupted-child"]))["interrupted-child"]
        assert identity["erased"] is True and identity["record_id"] is None
        with pytest.raises(ValueError, match="erased"):
            await restarted.ingest_messages(
                [{"message_id": "interrupted-child", "role": "assistant", "content": "again"}],
                namespace="a",
            )
    finally:
        await restarted.stop()


async def test_hard_erase_message_tombstone_survives_failure_before_raw_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "erase-barrier.db"
    first = _engine(path=path)
    await first.start()
    try:
        [record] = await first.ingest_messages(
            [{"message_id": "barrier-message", "role": "user", "content": "private"}],
            namespace="a",
            session_id="s",
            conversation_id="c",
        )
        storage = first._require_started()
        real_list = storage.list_records
        failed = False

        async def fail_after_tombstone(*args: Any, **kwargs: Any) -> Any:
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("interrupt after identity tombstone")
            return await real_list(*args, **kwargs)

        monkeypatch.setattr(storage, "list_records", fail_after_tombstone)
        with pytest.raises(RuntimeError, match="interrupt after identity tombstone"):
            await first.forget(record.record_id, namespace="a", hard=True, cascade=False)
        ledger = await gateway.gateway_of(first)._ledger()
        erased = (await lookup_messages(ledger, "a", ["barrier-message"]))["barrier-message"]
        assert erased["erased"] is True and erased["record_id"] is None
    finally:
        monkeypatch.undo()
        await first.stop()

    restarted = _engine(path=path)
    await restarted.start()
    try:
        await restarted.forget(record.record_id, namespace="a", hard=True, cascade=False)
        ledger = await gateway.gateway_of(restarted)._ledger()
        erased = (await lookup_messages(ledger, "a", ["barrier-message"]))["barrier-message"]
        assert erased["erased"] is True and erased["record_id"] is None
        assert await restarted._require_started().get_record(record.record_id) is None
    finally:
        await restarted.stop()


async def test_legal_hold_refusal_keeps_message_identity_live() -> None:
    eng = _engine(episodic_policies={"retention": {"legal_hold_namespaces": ["a"]}})
    await eng.start()
    try:
        [record] = await eng.ingest_messages(
            [{"message_id": "held-message", "role": "user", "content": "held"}],
            namespace="a",
            session_id="s",
            conversation_id="c",
        )
        with pytest.raises(MemspineError, match="legal hold"):
            await eng.forget(record.record_id, namespace="a", hard=True, cascade=False)
        ledger = await gateway.gateway_of(eng)._ledger()
        identity = (await lookup_messages(ledger, "a", ["held-message"]))["held-message"]
        assert identity["record_id"] == record.record_id and not identity.get("erased")
    finally:
        await eng.stop()


async def test_crash_during_reply_failure_settlement_recovers_terminally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "failed-transition.db"
    first = _engine(path=path)
    await first.start()
    try:
        [parent] = await first.ingest_messages(
            [{"message_id": "transition-parent", "role": "user", "content": "question"}],
            namespace="a",
            session_id="s",
            conversation_id="c",
        )
        gw = gateway.gateway_of(first)
        ledger = await gw._ledger()
        real_submit = gateway.ingest_resolved_messages

        async def forget_before_commit(*args: Any, **kwargs: Any) -> Any:
            await first.forget(parent.record_id, namespace="a")
            return await real_submit(*args, **kwargs)

        async def fail_once(**kwargs: Any) -> None:
            raise RuntimeError("simulated failure during terminal transition")

        monkeypatch.setattr(gateway, "ingest_resolved_messages", forget_before_commit)
        monkeypatch.setattr(ledger, "fail_intent", fail_once)
        with pytest.raises(RuntimeError, match="simulated failure"):
            await first.ingest_messages(
                [
                    {
                        "message_id": "transition-child",
                        "role": "assistant",
                        "content": "answer",
                        "reply_to": "transition-parent",
                    }
                ],
                namespace="a",
            )
        rows = await lookup_messages(ledger, "a", ["transition-child"])
        assert rows["transition-child"]["intent_status"] == "accepted"
        assert len(await ledger.pending_intents()) == 1
    finally:
        await first.stop()
        monkeypatch.undo()

    restarted = _engine(path=path)
    await restarted.start()
    try:
        ledger = await gateway.gateway_of(restarted)._ledger()
        rows = await lookup_messages(ledger, "a", ["transition-child"])
        assert rows["transition-child"]["intent_status"] == "failed"
        assert rows["transition-child"]["record_id"] is None
        assert await ledger.pending_intents() == []
        with pytest.raises(ReplyParentUnavailable):
            await restarted.ingest_messages(
                [
                    {
                        "message_id": "transition-child",
                        "role": "assistant",
                        "content": "answer",
                        "reply_to": "transition-parent",
                    }
                ],
                namespace="a",
            )
    finally:
        await restarted.stop()


async def test_post_commit_parent_forget_settles_reply_without_raw_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng = _engine()
    await eng.start()
    try:
        [parent] = await eng.ingest_messages(
            [{"message_id": "postcommit-parent", "role": "user", "content": "question"}],
            namespace="a",
            session_id="s",
            conversation_id="c",
        )
        gw = gateway.gateway_of(eng)
        real_finish = gw._finish

        async def erase_before_apply(*args: Any, **kwargs: Any) -> Any:
            await eng.forget(parent.record_id, namespace="a")
            return await real_finish(*args, **kwargs)

        monkeypatch.setattr(gw, "_finish", erase_before_apply)
        with pytest.raises(ReplyParentUnavailable):
            await eng.ingest_messages(
                [
                    {
                        "message_id": "postcommit-child",
                        "role": "assistant",
                        "content": "answer",
                        "reply_to": "postcommit-parent",
                    }
                ],
                namespace="a",
            )
        ledger = await gw._ledger()
        rows = await lookup_messages(ledger, "a", ["postcommit-child"])
        assert rows["postcommit-child"]["intent_status"] == "failed"
        assert rows["postcommit-child"]["record_id"] is None
        assert await ledger.pending_intents() == []
        assert await eng.timeline(namespace="a") == []
    finally:
        await eng.stop()


async def test_cross_session_reply_survives_timeline_graph_retry_and_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rm36-reply.db"
    parent_input = {
        "message_id": "cross-parent",
        "role": "user",
        "content": "Which venue?",
        "created_at": T0.isoformat(),
    }
    child_input = {
        "message_id": "cross-child",
        "role": "assistant",
        "content": "The Old Mill.",
        "reply_to": "cross-parent",
        "created_at": (T0 + timedelta(minutes=2)).isoformat(),
    }
    eng = _engine(associative=True, path=path)
    await eng.start()
    try:
        [parent] = await eng.ingest_messages(
            [parent_input], namespace="a", session_id="session-a", conversation_id="thread-a"
        )
        [child] = await eng.ingest_messages([child_input], namespace="a", session_id="session-b")
        [middle] = await eng.ingest_messages(
            [
                {
                    "message_id": "middle-root",
                    "role": "user",
                    "content": "A separate note",
                    "created_at": (T0 + timedelta(minutes=1)).isoformat(),
                }
            ],
            namespace="a",
            session_id="session-b",
        )
        meta = child.source.message
        assert meta["reply_to"] == "cross-parent"
        assert meta["session_id"] == "session-b"
        assert meta["conversation_id"] != "thread-a"
        assert f"{constants.REPLY_TO_PREFIX}{parent.record_id}" in child.tags
        edges = [
            e
            for e in await eng._graph.edges_of(child.record_id)
            if e.rel_type == constants.REPLY_TO_REL
        ]
        assert [(e.src, e.dst) for e in edges] == [(child.record_id, parent.record_id)]
        assert [r.record_id for r in await eng.timeline(namespace="a")] == [
            parent.record_id,
            middle.record_id,
            child.record_id,
        ]
        [retry] = await eng.ingest_messages([child_input], namespace="a", session_id="session-b")
        assert retry.record_id == child.record_id
        child_id, parent_id, middle_id = child.record_id, parent.record_id, middle.record_id
        conversation_id = meta["conversation_id"]
    finally:
        await eng.stop()

    restarted = _engine(associative=True, path=path)
    await restarted.start()
    try:
        [retry] = await restarted.ingest_messages(
            [child_input], namespace="a", session_id="session-b"
        )
        assert retry.record_id == child_id
        assert retry.source.message["conversation_id"] == conversation_id
        assert f"{constants.REPLY_TO_PREFIX}{parent_id}" in retry.tags
        edges = [
            e
            for e in await restarted._graph.edges_of(child_id)
            if e.rel_type == constants.REPLY_TO_REL
        ]
        assert [(e.src, e.dst) for e in edges] == [(child_id, parent_id)]
        assert [r.record_id for r in await restarted.timeline(namespace="a")] == [
            parent_id,
            middle_id,
            child_id,
        ]
    finally:
        await restarted.stop()


async def test_reply_link_edge_is_written_with_associative_memory() -> None:
    eng = _engine(associative=True)
    await eng.start()
    try:
        asked = await eng.write(
            "Which venue should we book?", namespace="a", memory_type="episodic"
        )
        reply = await eng.write(
            "The Old Mill.", namespace="a", memory_type="episodic", reply_to=asked.record_id
        )
        edges = [
            e
            for e in await eng._graph.edges_of(reply.record_id)
            if e.rel_type == constants.REPLY_TO_REL
        ]
        assert [(e.src, e.dst) for e in edges] == [(reply.record_id, asked.record_id)]
    finally:
        await eng.stop()


async def _thread(eng: Engine) -> tuple[str, str]:
    """A question, far-away chatter, then its answer replying to it."""
    msgs: list[dict[str, Any]] = [
        {
            "message_id": "thread-question",
            "conversation_id": "venue-thread",
            "role": "user",
            "content": "Which venue should we book?",
        }
    ]
    msgs += [
        {
            "message_id": f"thread-chatter-{i}",
            "conversation_id": "venue-thread",
            "role": "user",
            "content": f"unrelated chatter number {i}",
        }
        for i in range(8)
    ]
    msgs.append(
        {
            "message_id": "thread-answer",
            "conversation_id": "venue-thread",
            "role": "assistant",
            "content": "The Old Mill has parking.",
            "reply_to": "thread-question",
        }
    )
    for i, msg in enumerate(msgs):
        msg["created_at"] = (T0 + timedelta(minutes=i)).isoformat()
    records = await eng.ingest_messages(
        msgs, namespace="a", session_id="s1", conversation_id="venue-thread"
    )
    return records[0].content, records[-1].content


@pytest.mark.parametrize("on", [False, True])
async def test_replay_pulls_the_answered_message_only_when_on(on: bool) -> None:
    eng = _engine(reply_links=on)
    await eng.start()
    try:
        question, answer = await _thread(eng)
        result = await eng.read(
            "Old Mill parking", namespace="a", mode="replay", top_k=1, replay_window=1
        )
        assert result.mode == "replay"
        shown = [r.content for r in result.context.records]
        assert answer in shown
        assert (question in shown) is on
    finally:
        await eng.stop()
