"""RM-42 (ADR-090 item 5): unresolved fact candidates persist as source-bound, unasserted
annotations: visible to inspection, never facts or search hits, re-resolved after an
activation, erased with their source."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.derive import non_generative_facts
from memspine.derive.non_generative_facts import UnresolvedAnnotation
from memspine.exceptions import MemspineError
from memspine.ingest import windows as ingest_windows
from memspine.observability import bindings, tap
from memspine.observability import inspect as insp
from memspine.scope import ScopeRefused
from memspine.services.llm.base import LLMRouter
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import RecordStatus
from memspine.workers.windows import WindowPipeline

UNMAPPED = "I grew up in Porto."  # the rule miner reads ``origin``: no predicate maps it


async def _engine(
    monkeypatch: pytest.MonkeyPatch,
    path: Path,
    *,
    participants: bool = False,
    viewer_required: bool = False,
    **observability: Any,
) -> Engine:
    async def router(
        config: MemspineConfig, hooks: object = None, wiring: object = None
    ) -> LLMRouter:
        return LLMRouter({})

    monkeypatch.setattr("memspine.runtime.container.build_llm_router", router)
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {"provider": "sqlite", "settings": {"path": str(path), "busy_timeout_s": 5}}
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        write={
            "online_extractor": True,
            "window_extract": "facts",
            "window_async": False,
            "window_max_turns": 2,
            "window_idle_s": 0.0,
            "participants": "session" if participants else "off",
        },
        workers={"outbox": True},
        isolation={"viewer_required": viewer_required},
        **({"observability": observability} if observability else {}),
    )
    await eng.start()
    return eng


async def _ingest(eng: Engine, namespace: str = "a", *, private: bool = False) -> list[Any]:
    private_fields: dict[str, Any] = (
        {"participants": ["Mira", "Other"], "visibility": "private"} if private else {}
    )
    records = await eng.ingest_messages(
        [
            {
                "message_id": "m-1",
                "role": "user",
                "sender": {"id": "Mira"},
                "content": "I work for Acme.",
                "created_at": "2026-10-24T10:00:00+00:00",
                **private_fields,
            },
            {
                "message_id": "m-2",
                "role": "user",
                "sender": {"id": "Mira"},
                "content": UNMAPPED,
                "created_at": "2026-10-24T10:01:00+00:00",
                **private_fields,
            },
        ],
        namespace=namespace,
        session_id="s1",
        conversation_id="s1",
    )
    await eng.flush_windows(namespace)
    return list(records)


def _pipeline(eng: Engine) -> WindowPipeline:
    assert eng._windows is not None
    return eng._windows


async def _semantic(eng: Engine) -> list[Any]:
    return [
        r
        for r in await eng._require_started().list_records("a", "semantic")
        if r.status.value != "deleted"
    ]


async def test_unresolved_candidates_persist_source_bound_and_unasserted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _engine(monkeypatch, tmp_path / "persist.db")
    try:
        records = await _ingest(eng)
        [item] = ingest_windows.unresolved_annotations(eng, "a")
        assert await eng.unresolved_annotations("a") == [UnresolvedAnnotation.model_validate(item)]
        annotation = UnresolvedAnnotation.model_validate(item)
        source = records[1]
        assert annotation.source.record_id == source.record_id
        assert annotation.source.source_version == source.version
        assert annotation.reason == "unsupported_predicate"
        assert (annotation.attribute, annotation.value) == ("origin", "Porto")
        assert annotation.asserted is False
        assert annotation.producer_revision == "personal-rules:1"
        assert annotation.span is not None
        assert UNMAPPED[annotation.span.start : annotation.span.end] == UNMAPPED
        rows = [
            r for r in _pipeline(eng).derivation_effect_status("a") if r["layer"] == "unresolved"
        ]
        assert all(r["state"] == "complete" for r in rows)
        assert sorted(r["count"] for r in rows) == [0, 1]  # the window, then the session close
    finally:
        await eng.stop()


async def test_unresolved_annotations_are_never_facts_or_search_hits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _engine(monkeypatch, tmp_path / "facts.db")
    try:
        await _ingest(eng)
        semantic = await _semantic(eng)
        assert [r.content for r in semantic] == ["Mira employed_by Acme"]
        hits = await eng.search("Porto", namespace="a")
        found = [h[0] if isinstance(h, tuple) else h for h in hits]
        assert all(
            r.memory_type == "episodic" or "Porto" not in r.content for r in found
        )  # at most the raw turn (L0)
        status = (await eng.ingestion_status(["m-2"], namespace="a"))["m-2"]
        assert status.counts.get("L1", 0) == 1  # only the resolved fact counts as L1
    finally:
        await eng.stop()


def _flat(trace: dict[str, Any]) -> list[dict[str, Any]]:
    out = list(trace["events"])
    for child in trace["children"]:
        out += _flat(child)
    return out


async def test_unresolved_annotations_are_visible_to_the_inspector(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tap.shutdown()
    eng = await _engine(
        monkeypatch,
        tmp_path / "inspect.db",
        inspect=True,
        tap={"enabled": True, "sink": "memory"},
    )
    try:
        await _ingest(eng)
        with bindings.bound(eng._binding):
            traces = tap.memory_traces()
        events = [
            e["fields"]
            for t in traces
            for e in _flat(t)
            if e["stage"] == "write.unresolved.annotation"
        ]
        [event] = events
        assert event["reason"] == "unsupported_predicate" and event["asserted"] is False
        assert event["attribute"] == "origin" and event["span"] is not None
    finally:
        await eng.stop()
        tap.shutdown()
        insp.configure(False)


async def test_activation_re_resolves_the_unresolved_candidates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _engine(monkeypatch, tmp_path / "reresolve.db")
    try:
        await _ingest(eng)
        assert len(ingest_windows.unresolved_annotations(eng, "a")) == 1
        # the activated vocabulary maps ``origin`` now (a registry activation)
        monkeypatch.setitem(non_generative_facts._RULE_TO_PREDICATE, "origin", "resides_in")
        assert len(await eng.unresolved_annotations("a")) == 1
        assert (
            await eng.activate_derivation_policy(
                "a", policy_revision="rules:2", registry_digest="registry:2", runner_quiesced=True
            )
            == 1
        )
        assert ingest_windows.unresolved_annotations(eng, "a") == []
        assert await eng.unresolved_annotations("a") == []
        contents = sorted(r.content for r in await _semantic(eng))
        assert contents == ["Mira employed_by Acme", "Mira resides_in Porto"]
        assert (
            await eng.activate_derivation_policy(
                "a", policy_revision="rules:2", registry_digest="registry:2", runner_quiesced=True
            )
            == 0
        )
    finally:
        await eng.stop()


async def test_activation_identity_and_digest_change_are_durable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "identity.db"
    eng = await _engine(monkeypatch, db)
    try:
        await _ingest(eng)
        activate = eng.activate_derivation_policy
        assert (
            await activate(
                "a", policy_revision="rules:1", registry_digest="digest:1", runner_quiesced=True
            )
            == 1
        )
        assert len(await eng.unresolved_annotations("a")) == 1
        assert (
            await activate(
                "a", policy_revision="rules:1", registry_digest="digest:1", runner_quiesced=True
            )
            == 0
        )
        assert (
            await activate(
                "a", policy_revision="rules:1", registry_digest="digest:2", runner_quiesced=True
            )
            == 1
        )
        monkeypatch.setitem(non_generative_facts._RULE_TO_PREDICATE, "origin", "resides_in")
        assert (
            await activate(
                "a", policy_revision="rules:2", registry_digest="digest:2", runner_quiesced=True
            )
            == 1
        )
        assert await eng.unresolved_annotations("a") == []
        assert (
            await activate(
                "a", policy_revision="rules:3", registry_digest="digest:3", runner_quiesced=True
            )
            == 0
        )
        assert sorted(r.content for r in await _semantic(eng)) == [
            "Mira employed_by Acme",
            "Mira resides_in Porto",
        ]
    finally:
        await eng.stop()
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "select policy_revision, registry_digest from intake_derivation_policy "
            "where stage = 'window.discovery'"
        ).fetchall()
    assert rows == [("rules:3", "digest:3")]


async def test_interrupted_activation_supersedes_pending_replay_and_reopens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "interrupted.db"
    eng = await _engine(monkeypatch, db)
    try:
        await _ingest(eng)
        pipeline = _pipeline(eng)

        async def interrupted(namespace: str) -> None:
            raise RuntimeError("after activation commit")

        monkeypatch.setattr(pipeline, "drain_activation", interrupted)
        with pytest.raises(RuntimeError, match="after activation commit"):
            await eng.activate_derivation_policy(
                "a", policy_revision="rules:2", registry_digest="digest:1", runner_quiesced=True
            )
        assert (
            await eng.activate_derivation_policy(
                "a", policy_revision="rules:2", registry_digest="digest:1", runner_quiesced=True
            )
            == 0
        )
        with pytest.raises(RuntimeError, match="after activation commit"):
            await eng.activate_derivation_policy(
                "a", policy_revision="rules:2", registry_digest="digest:2", runner_quiesced=True
            )
        with sqlite3.connect(db) as conn:
            rows = [
                json.loads(row[0])
                for row in conn.execute("select payload from outbox_item where stage = 'window'")
                if "activation" in json.loads(row[0])
            ]
        assert len(rows) == 2
        assert rows[0]["cancelled_by_policy_revision"] == "rules:2"
        assert rows[1]["activation"]["registry_digest"] == "digest:2"
    finally:
        await eng.stop()

    monkeypatch.setitem(non_generative_facts._RULE_TO_PREDICATE, "origin", "resides_in")
    reopened = await _engine(monkeypatch, db)
    try:
        await _pipeline(reopened).drain_activation("a")
        assert await reopened.unresolved_annotations("a") == []
        assert sorted(r.content for r in await _semantic(reopened)) == [
            "Mira employed_by Acme",
            "Mira resides_in Porto",
        ]
    finally:
        await reopened.stop()


@pytest.mark.parametrize("prior_state", ["retryable_hole", "waived_hole", "completed"])
async def test_activation_cancels_only_runnable_prior_replay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, prior_state: str
) -> None:
    db = tmp_path / "hole-supersession.db"
    eng = await _engine(monkeypatch, db)
    try:
        await _ingest(eng)

        async def interrupted(namespace: str) -> None:
            raise RuntimeError("after activation commit")

        monkeypatch.setattr(_pipeline(eng), "drain_activation", interrupted)
        with pytest.raises(RuntimeError, match="after activation commit"):
            await eng.activate_derivation_policy(
                "a", policy_revision="rules:2", registry_digest="digest:1", runner_quiesced=True
            )
        with sqlite3.connect(db) as conn:
            [(old_offset,)] = conn.execute(
                "select item_offset from outbox_item where stage = 'window' "
                "and payload like '%\"activation\"%'"
            ).fetchall()
            conn.execute(
                "update outbox_progress set watermark = ?, target_offset = ?, holes = ? "
                "where owner = 'a' and job_kind = 'window'",
                (
                    old_offset,
                    old_offset,
                    json.dumps(
                        []
                        if prior_state == "completed"
                        else [
                            {
                                "offset": old_offset,
                                "reason": "raised",
                                "attempts": 0,
                                "waived": prior_state == "waived_hole",
                            }
                        ]
                    ),
                ),
            )
        with pytest.raises(RuntimeError, match="after activation commit"):
            await eng.activate_derivation_policy(
                "a", policy_revision="rules:2", registry_digest="digest:2", runner_quiesced=True
            )
        with sqlite3.connect(db) as conn:
            rows = [
                json.loads(row[0])
                for row in conn.execute(
                    "select payload from outbox_item where stage = 'window' order by item_offset"
                )
                if "activation" in json.loads(row[0])
            ]
        assert len(rows) == 2
        assert (rows[0].get("cancelled_by_policy_revision") == "rules:2") == (
            prior_state == "retryable_hole"
        )
        assert rows[1]["activation"]["registry_digest"] == "digest:2"
    finally:
        await eng.stop()


async def test_activation_keeps_session_context_and_excludes_erased_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "sessions.db"
    eng = await _engine(monkeypatch, db)
    try:
        for session, city in (("s1", "Porto"), ("s2", "Lisbon")):
            await eng.ingest_messages(
                [
                    {
                        "message_id": f"message-{session}",
                        "role": "user",
                        "sender": {"id": "Mira"},
                        "content": f"I grew up in {city}.",
                        "created_at": "2026-10-24T10:00:00+00:00",
                    }
                ],
                namespace="a",
                session_id=session,
                conversation_id=f"conversation-{session}",
            )
        await eng.flush_windows("a")
        annotations = await eng.unresolved_annotations("a")
        assert len(annotations) == 2

        async def interrupted(namespace: str) -> None:
            raise RuntimeError("after activation commit")

        monkeypatch.setattr(_pipeline(eng), "drain_activation", interrupted)
        with pytest.raises(RuntimeError, match="after activation commit"):
            await eng.activate_derivation_policy(
                "a", policy_revision="rules:2", registry_digest="digest:1", runner_quiesced=True
            )
        with sqlite3.connect(db) as conn:
            replays = [
                json.loads(row[0])
                for row in conn.execute("select payload from outbox_item where stage = 'window'")
                if "activation" in json.loads(row[0])
            ]
        assert len(replays) == 2
        assert {(item["session"], item["conversation_id"]) for item in replays} == {
            ("s1", "conversation-s1"),
            ("s2", "conversation-s2"),
        }
        assert all(len(item["record_ids"]) == 1 for item in replays)

        erased = annotations[0]
        await eng.forget(erased.source.record_id, namespace="a", hard=True)
        with pytest.raises(RuntimeError, match="after activation commit"):
            await eng.activate_derivation_policy(
                "a", policy_revision="rules:2", registry_digest="digest:2", runner_quiesced=True
            )
        with sqlite3.connect(db) as conn:
            replays = [
                json.loads(row[0])
                for row in conn.execute("select payload from outbox_item where stage = 'window'")
                if "activation" in json.loads(row[0])
                and json.loads(row[0])["activation"]["registry_digest"] == "digest:2"
            ]
        assert len(replays) == 1
        assert erased.source.record_id not in replays[0]["record_ids"]
        assert len(replays[0]["record_ids"]) == 1
        assert (replays[0]["session"], replays[0]["conversation_id"]) in {
            ("s1", "conversation-s1"),
            ("s2", "conversation-s2"),
        }
    finally:
        await eng.stop()


@pytest.mark.parametrize("membership_change", ["inactive", "stale_version"])
async def test_activation_rechecks_membership_at_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, membership_change: str
) -> None:
    db = tmp_path / f"{membership_change}.db"
    eng = await _engine(monkeypatch, db)
    try:
        await _ingest(eng)
        [annotation] = await eng.unresolved_annotations("a")
        with sqlite3.connect(db) as conn:
            if membership_change == "inactive":
                conn.execute(
                    "update outbox_source_membership set active = 0 "
                    "where stage = 'window' and record_id = ?",
                    (annotation.source.record_id,),
                )
            else:
                conn.execute(
                    "update outbox_source_membership set source_version = source_version + 1 "
                    "where stage = 'window' and record_id = ?",
                    (annotation.source.record_id,),
                )
        assert (
            await eng.activate_derivation_policy(
                "a", policy_revision="rules:2", registry_digest="digest:2", runner_quiesced=True
            )
            == 0
        )
        with sqlite3.connect(db) as conn:
            replays = [
                json.loads(row[0])
                for row in conn.execute("select payload from outbox_item where stage = 'window'")
                if "activation" in json.loads(row[0])
            ]
        assert replays == []
    finally:
        await eng.stop()


async def test_unresolved_annotations_are_erased_with_their_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "erase.db"
    eng = await _engine(monkeypatch, db)
    try:
        records = await _ingest(eng)
        await eng.forget(records[1].record_id, namespace="a", hard=True)
        assert ingest_windows.unresolved_annotations(eng, "a") == []
        assert await eng.unresolved_annotations("a") == []
        rows = _pipeline(eng).derivation_effect_status("a")
        assert not any("Porto" in str(row["artifact"]) for row in rows)
    finally:
        await eng.stop()
    import sqlite3

    con = sqlite3.connect(db)
    try:
        artifacts = " ".join(str(r[0]) for r in con.execute("select artifact from outbox_effect"))
    finally:
        con.close()
    assert "Porto" not in artifacts


async def test_public_inspection_is_namespace_scoped_and_survives_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "public.db"
    eng = await _engine(monkeypatch, db, participants=True)
    try:
        a_records = await _ingest(eng, "a", private=True)
        b_records = await _ingest(eng, "b")
        [a] = await eng.unresolved_annotations("a")
        [b] = await eng.unresolved_annotations("b")
        assert a.source.record_id == a_records[1].record_id
        assert b.source.record_id == b_records[1].record_id
        assert a.source.namespace == "a" and b.source.namespace == "b"
        assert a.asserted is False and b.asserted is False
        assert await eng.unresolved_annotations("c") == []
        assert await eng.unresolved_annotations("a", viewer="Mira") == [a]
        assert await eng.unresolved_annotations("a", viewer="Other") == []
    finally:
        await eng.stop()

    restarted = await _engine(monkeypatch, db, participants=True)
    try:
        assert await restarted.unresolved_annotations("a") == [a]
        assert await restarted.unresolved_annotations("b") == [b]
        await restarted.forget(a.source.record_id, namespace="a", hard=True)
        assert await restarted.unresolved_annotations("a") == []
        assert await restarted.unresolved_annotations("b") == [b]
    finally:
        await restarted.stop()


async def test_public_inspection_rejects_malformed_and_stale_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _engine(monkeypatch, tmp_path / "boundary.db")
    try:
        await _ingest(eng)
        [valid] = ingest_windows.unresolved_annotations(eng, "a")
        pipeline = _pipeline(eng)
        assert await eng.unresolved_annotations("a")
        with monkeypatch.context() as patch:
            patch.setattr("memspine.trust.pipeline.erasure_state.visible_id", lambda *_: False)
            assert await eng.unresolved_annotations("a") == []
        malformed = {**valid, "asserted": True}
        stale = {**valid, "source": {**valid["source"], "source_version": 999}}
        wrong_scope = {**valid, "source": {**valid["source"], "namespace": "b"}}
        wrong_hash = {**valid, "source": {**valid["source"], "content_hash": "stale"}}
        wrong_sender = {**valid, "source": {**valid["source"], "sender": "Other"}}
        monkeypatch.setattr(
            pipeline,
            "unresolved_annotations",
            lambda _namespace: [
                malformed,
                stale,
                wrong_scope,
                wrong_hash,
                wrong_sender,
                {"bad": "shape"},
            ],
        )
        assert await eng.unresolved_annotations("a") == []
    finally:
        await eng.stop()


async def test_public_inspection_checks_lifecycle_and_pipeline_absence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    empty = Engine(
        template="core",
        dotenv_path=None,
        storage={"records": {"provider": "sqlite", "settings": {"path": str(tmp_path / "off.db")}}},
        model={"embedding": {"provider": "hash", "settings": {}}},
    )
    with pytest.raises(MemspineError, match="Engine not started"):
        await empty.unresolved_annotations("a")
    await empty.start()
    try:
        assert await empty.unresolved_annotations("a") == []
    finally:
        await empty.stop()
    with pytest.raises(MemspineError, match="Engine not started"):
        await empty.unresolved_annotations("a")


async def test_latest_empty_effect_replaces_previous_annotation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _engine(monkeypatch, tmp_path / "replacement.db")
    try:
        await _ingest(eng)
        [valid] = ingest_windows.unresolved_annotations(eng, "a")
        pipeline = _pipeline(eng)
        effects = [
            {
                "layer": "unresolved",
                "state": "complete",
                "offset": 1,
                "identity": {"sources": [{"record_id": valid["source"]["record_id"]}]},
                "artifact": {"unresolved_annotations": [valid]},
            },
            {
                "layer": "unresolved",
                "state": "complete",
                "offset": 2,
                "identity": {"sources": [{"record_id": valid["source"]["record_id"]}]},
                "artifact": {"unresolved_annotations": []},
            },
        ]
        monkeypatch.setattr(pipeline, "derivation_effect_status", lambda _namespace: effects)
        assert await eng.unresolved_annotations("a") == []
        effects[1]["artifact"] = {"unresolved_annotations": "malformed"}
        assert await eng.unresolved_annotations("a") == []
    finally:
        await eng.stop()


async def test_public_inspection_respects_required_viewer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _engine(
        monkeypatch, tmp_path / "viewer.db", participants=True, viewer_required=True
    )
    try:
        await _ingest(eng, private=True)
        with pytest.raises(ScopeRefused, match="viewer_required"):
            await eng.unresolved_annotations("a")
        assert len(await eng.unresolved_annotations("a", viewer="Mira")) == 1
        assert await eng.unresolved_annotations("a", viewer="Other") == []
    finally:
        await eng.stop()


@pytest.mark.parametrize("change", ["excluded", "quarantined", "instruction", "inactive"])
async def test_activation_omits_source_that_became_ineligible(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, change: str
) -> None:
    eng = await _engine(monkeypatch, tmp_path / f"{change}.db")
    try:
        records = await _ingest(eng)
        assert len(await eng.unresolved_annotations("a")) == 1
        source = await eng._require_started().get_record(records[1].record_id)
        assert source is not None
        if change == "excluded":
            changed = source.model_copy(update={"tags": [*source.tags, "derive_use:excluded"]})
        elif change == "quarantined":
            changed = source.model_copy(update={"quarantined": True})
        elif change == "instruction":
            changed = source.model_copy(update={"instruction_flag": True})
        else:
            changed = source.model_copy(update={"status": RecordStatus.DELETED})
        await eng._append_and_project(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace="a",
                actor="system",
                payload={"record": changed.model_dump(mode="json")},
            )
        )
        assert await eng.unresolved_annotations("a") == []
        assert (
            await eng.activate_derivation_policy(
                "a", policy_revision="rules:2", registry_digest="digest:2", runner_quiesced=True
            )
            == 0
        )
        with sqlite3.connect(tmp_path / f"{change}.db") as conn:
            assert not any(
                "activation" in json.loads(row[0])
                for row in conn.execute("select payload from outbox_item where stage = 'window'")
            )
    finally:
        await eng.stop()
