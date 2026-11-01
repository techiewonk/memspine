"""RM-42 (ADR-090 item 5): unresolved fact candidates persist as source-bound, unasserted
annotations: visible to inspection, never facts or search hits, re-resolved after an
activation, erased with their source."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.derive import non_generative_facts
from memspine.derive.non_generative_facts import UnresolvedAnnotation
from memspine.ingest import windows as ingest_windows
from memspine.observability import bindings, tap
from memspine.observability import inspect as insp
from memspine.services.llm.base import LLMRouter
from memspine.workers.windows import WindowPipeline

UNMAPPED = "I grew up in Porto."  # the rule miner reads ``origin``: no predicate maps it


async def _engine(monkeypatch: pytest.MonkeyPatch, path: Path, **observability: Any) -> Engine:
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
        },
        workers={"outbox": True},
        **({"observability": observability} if observability else {}),
    )
    await eng.start()
    return eng


async def _ingest(eng: Engine) -> list[Any]:
    records = await eng.ingest_messages(
        [
            {
                "message_id": "m-1",
                "role": "user",
                "sender": {"id": "Mira"},
                "content": "I work for Acme.",
                "created_at": "2026-10-24T10:00:00+00:00",
            },
            {
                "message_id": "m-2",
                "role": "user",
                "sender": {"id": "Mira"},
                "content": UNMAPPED,
                "created_at": "2026-10-24T10:01:00+00:00",
            },
        ],
        namespace="a",
        session_id="s1",
        conversation_id="s1",
    )
    await eng.flush_windows("a")
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
        assert await ingest_windows.reresolve_unresolved(eng, "a") == 1
        assert ingest_windows.unresolved_annotations(eng, "a") == []
        contents = sorted(r.content for r in await _semantic(eng))
        assert contents == ["Mira employed_by Acme", "Mira resides_in Porto"]
        assert await ingest_windows.reresolve_unresolved(eng, "a") == 0  # nothing left
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
