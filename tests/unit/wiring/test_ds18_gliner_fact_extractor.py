"""DS-18 (ADR-090 item 6): the configured non-generative GLiNER fact extractor behind the
rules-only window path's extractor hook. Model-free: the GLiNER pool loads a fake GLiNER2
model with the SDK surface the clients call (no download, no weights); no LLM role is bound.

Positive and abstention cases end to end: exact spans, zero generative calls, canonical
envelopes, unresolved annotations, source annotation batches, restart and erasure."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "derive"))
from gliner_fake import FakeGliner

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.exceptions import ConfigError
from memspine.memories.semantic.entities import GlinerFactEntityExtractor
from memspine.schemas.durable import AnnotationBatch, SemanticEnvelope
from memspine.services.llm.base import LLMRouter

GLINER_SLOT = {
    "provider": "gliner2",
    "settings": {"model": "fake/gliner2", "tasks": ["entities", "relations"]},
}
MESSAGES = [
    {
        "message_id": "g-1",
        "role": "user",
        "sender": {"id": "Mira"},
        "content": "I live in Lisbon.",
        "created_at": "2026-10-24T10:00:00+00:00",
    },
    {
        "message_id": "g-2",
        "role": "user",
        "sender": {"id": "Mira"},
        "content": "If I live in Porto, I will cycle to work.",
        "created_at": "2026-10-24T10:01:00+00:00",
    },
    {
        "message_id": "g-3",
        "role": "user",
        "sender": {"id": "Mira"},
        "content": "Bram works for Acme.",
        "created_at": "2026-10-24T10:02:00+00:00",
    },
]


def _config(path: Path, **semantic_policies: Any) -> dict[str, Any]:
    return {
        "template": "core",
        "dotenv_path": None,
        "storage": {
            "records": {
                "provider": "sqlite",
                "settings": {"path": str(path), "busy_timeout_s": 5.0},
            }
        },
        "model": {
            "embedding": {"provider": "hash", "settings": {}},
            "extraction": {"semantic": GLINER_SLOT},
        },
        "memories": {
            "episodic": {"enabled": True},
            "semantic": {"enabled": True, "policies": semantic_policies},
        },
        "write": {
            "online_extractor": True,
            "window_extract": "facts",
            "window_async": False,
            "window_max_turns": 3,
            "window_idle_s": 0.0,
        },
        "workers": {"outbox": True},
    }


async def _engine(monkeypatch: pytest.MonkeyPatch, path: Path, model: FakeGliner) -> Engine:
    async def router(
        config: MemspineConfig, hooks: object = None, wiring: object = None
    ) -> LLMRouter:
        return LLMRouter({})  # no generative role: the rules-only path

    monkeypatch.setattr("memspine.runtime.container.build_llm_router", router)
    monkeypatch.setattr(
        "memspine.clients.weights.gliner2.load_local_gliner", lambda settings: model
    )
    eng = Engine(**_config(path, entity_extraction="gliner", grounded_facts="gliner"))
    await eng.start()
    return eng


async def _facts(eng: Engine, ns: str = "a") -> list[Any]:
    from memspine.trust.pipeline.erasure_state import visible_id

    storage = eng._require_started()
    return [
        r
        for r in await storage.list_records(ns, "semantic")
        if "non_generative" in r.tags and visible_id(eng, r.record_id)
    ]


def _batches(eng: Engine) -> dict[str, AnnotationBatch]:
    assert eng._windows is not None
    out: dict[str, AnnotationBatch] = {}
    for row in eng._windows.derivation_effect_status("a"):
        if row["layer"] == "annotations" and row.get("state") == "complete":
            for raw in row["artifact"]["annotation_batches"]:
                batch = AnnotationBatch.model_validate(raw)
                out[batch.source.record_id] = batch
    return out


async def test_configured_gliner_extractor_grounds_and_abstains_without_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model = FakeGliner()
    db = tmp_path / "ds18.db"
    eng = await _engine(monkeypatch, db, model)
    try:
        assert isinstance(eng._semantic._extractor, GlinerFactEntityExtractor)  # type: ignore[union-attr]
        records = await eng.ingest_messages(
            MESSAGES, namespace="a", session_id="s1", conversation_id="s1"
        )
        ids = [r.record_id for r in records]
        stats = await eng.flush_windows("a")
        assert stats["calls"] == 0  # zero generative calls
        assert model.calls >= len(MESSAGES)  # the structured GLiNER pass ran per source
        [fact] = await _facts(eng)
        assert fact.content == "Mira resides_in Lisbon"
        envelope = SemanticEnvelope.model_validate(fact.fingerprint.envelope)
        assert envelope.registry is not None and envelope.registry.predicate == "resides_in"
        assert envelope.producer is not None
        assert envelope.producer.revision == "gliner-structured-facts:1"
        [evidence] = envelope.evidence
        assert evidence.turn is not None and evidence.turn.source_id == ids[0]
        assert evidence.span is not None and evidence.span.quote == "I live in Lisbon."

        assert eng._windows is not None
        unresolved = {
            item["source"]["record_id"]: item["reason"]
            for item in eng._windows.unresolved_annotations("a")
        }
        assert unresolved == {ids[1]: "grounding_unsupported", ids[2]: "subject_not_speaker"}

        batches = _batches(eng)
        assert set(batches) == set(ids)
        assert {(m.text, m.label) for m in batches[ids[2]].mentions} == {
            ("Bram", "person"),
            ("Acme", "organization"),
        }
        assert all(m.asserted is False for b in batches.values() for m in b.mentions)
    finally:
        await eng.stop()

    # Restart: the claim, the unresolved annotations and the batches are durable.
    eng = await _engine(monkeypatch, db, FakeGliner())
    try:
        assert [f.content for f in await _facts(eng)] == ["Mira resides_in Lisbon"]
        assert eng._windows is not None
        assert {i["source"]["record_id"] for i in eng._windows.unresolved_annotations("a")} == {
            ids[1],
            ids[2],
        }
        assert set(_batches(eng)) == set(ids)

        # Erasure: the source takes its claim, its annotations and its unresolved items along
        # (an effect bound to an erased source is deleted whole, RM-42).
        await eng.forget(ids[0], namespace="a", hard=True)
        await eng.forget(ids[2], namespace="a", hard=True)
        assert await _facts(eng) == []
        left = {i["source"]["record_id"] for i in eng._windows.unresolved_annotations("a")}
        assert not left & {ids[0], ids[2]}
        remaining = _batches(eng)
        assert ids[0] not in remaining and ids[2] not in remaining
        assert not any(
            m.source_record_id in (ids[0], ids[2]) for b in remaining.values() for m in b.mentions
        )
    finally:
        await eng.stop()


def _load(cfg: dict[str, Any]) -> MemspineConfig:
    from memspine.config.loader import load_config

    template = cfg.pop("template")
    cfg.pop("dotenv_path")
    return load_config(template=template, overrides=cfg).config


def test_grounded_facts_is_opt_in_and_validated(tmp_path: Path) -> None:
    from memspine.config.loader import load_config

    for template in ("base", "assistant", "core"):
        semantic = load_config(template=template).config.memories.get("semantic")
        assert semantic is None or "grounded_facts" not in semantic.policies

    with pytest.raises((ConfigError, ValueError), match="entity_extraction: gliner"):
        _load(_config(tmp_path / "x.db", grounded_facts="gliner"))
    with pytest.raises((ConfigError, ValueError), match="'off' or 'gliner'"):
        _load(_config(tmp_path / "x.db", entity_extraction="gliner", grounded_facts="llm"))
    cfg = _config(tmp_path / "y.db", entity_extraction="gliner", grounded_facts="gliner")
    cfg["model"]["extraction"]["semantic"] = {
        "provider": "gliner2",
        "settings": {"model": "fake/gliner2", "tasks": ["entities"]},
    }
    with pytest.raises((ConfigError, ValueError), match="relations"):
        _load(cfg)
    # Without the policy the GLiNER semantic extractor is the plain, mention-only one.
    plain = _load(_config(tmp_path / "z.db", entity_extraction="gliner"))
    semantic = plain.memories["semantic"]
    assert semantic.policies.get("grounded_facts", "off") == "off"
