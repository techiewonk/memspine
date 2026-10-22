"""Exercise real offline GLiNER through Engine message ingestion and window flush.

The GLiNER model is loaded from a complete local Hugging Face snapshot. The window
compiler uses a deterministic synthetic supported-claim LiteLLM fixture; no generative
model endpoint is contacted. All database and projection files live in a temporary directory.

From the repository root, with ``PYTHONPATH=src``::

    python scripts/check_gliner_ingestion.py --report docs/provider_verification/gliner-ingestion-real.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import importlib.metadata
import json
import math
import os
import platform
import re
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT = Path(
    "C:/Users/hembad/.cache/huggingface/hub/models--fastino--gliner2.5-base-v1/"
    "snapshots/ca906247640776a07753514055be9726f9080ead"
)
DEFAULT_REVISION = "ca906247640776a07753514055be9726f9080ead"
NAMESPACE = "pa121-gliner-source"
OTHER_NAMESPACE = "pa121-gliner-isolation-check"
SESSION_ID = "pa121-session-001"
CONVERSATION_ID = "pa121-conversation-001"
EVENT_TIME = "2024-03-09T14:45:00+05:30"
MESSAGES: tuple[dict[str, Any], ...] = (
    {
        "message_id": "pa121-message-001",
        "session_id": SESSION_ID,
        "conversation_id": CONVERSATION_ID,
        "role": "user",
        "sender": {"id": "mira-user-17", "kind": "human", "name": "Mira"},
        "content": "Mira works for Acme in Utrecht.",
        "created_at": EVENT_TIME,
    },
    {
        "message_id": "pa121-message-002",
        "session_id": SESSION_ID,
        "conversation_id": CONVERSATION_ID,
        "reply_to": "pa121-message-001",
        "role": "assistant",
        "sender": {"id": "assistant-local-1", "kind": "agent", "name": "Assistant"},
        "content": "Thanks, I will remember that.",
        "created_at": "2024-03-09T14:46:00+05:30",
    },
)
MAX_GLINER_CALLS = 12
MAX_WINDOW_LLM_CALLS = 12
TIMEOUT_SECONDS = 180.0


class _CountingExtractor:
    """Count calls while delegating every load and inference to the real SDK/model."""

    def __init__(self, inner: Any, counter: dict[str, int]) -> None:
        self._inner = inner
        self._counter = counter

    def extract_entities(self, *args: Any, **kwargs: Any) -> Any:
        self._counter["used"] += 1
        if self._counter["used"] > self._counter["limit"]:
            raise RuntimeError("real GLiNER call cap exceeded")
        return self._inner.extract_entities(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        if name == "_inner" or (name.startswith("__") and name.endswith("__")):
            raise AttributeError(name)
        return getattr(self._inner, name)


def _deterministic_window_reply(messages: Any) -> str | None:
    """Make one fully source-quoted synthetic fact for the PA-121 prompt only."""
    rows = list(messages) if isinstance(messages, (list, tuple)) else []
    system = str(rows[0].get("content", "")) if rows and isinstance(rows[0], Mapping) else ""
    user = str(rows[-1].get("content", "")) if rows and isinstance(rows[-1], Mapping) else ""
    if not system.startswith("You turn numbered conversation lines into memory items"):
        return None
    for match in re.finditer(r"^\[(\d+)\] \[[^\]]+\] ([^:\n]+): ([^\n]+)$", user, re.M):
        line_number = int(match.group(1))
        speaker = match.group(2).strip()
        text = match.group(3).strip()
        if text != "Mira works for Acme in Utrecht.":
            continue
        item = {
            "s": "Mira",
            "r": "works_for",
            "o": "Acme",
            "text": "Mira works for Acme.",
            "kind": "state",
            "mode": "asserted",
            "neg": False,
            "when": None,
            "until": None,
            "lines": [line_number],
            "span": text,
            "source": "user_stated",
            "speaker": speaker,
            "reported_speaker": None,
        }
        return json.dumps({"facts": [item], "edges": [], "episode": None})
    return None


@contextmanager
def _offline_gliner(counter: dict[str, int]) -> Iterator[None]:
    """Count real model calls at the engine pool loader boundary; leave loader behavior intact."""
    from memspine.clients.weights import gliner2 as gliner_runtime

    original = gliner_runtime.load_local_gliner

    def counted_loader(settings: Any) -> Any:
        return _CountingExtractor(original(settings), counter)

    gliner_runtime.load_local_gliner = counted_loader
    try:
        yield
    finally:
        gliner_runtime.load_local_gliner = original


@contextmanager
def _offline_window_transport(counter: dict[str, int]) -> Iterator[None]:
    """Use a deterministic supported-claim fixture and fail closed on other prompts."""
    scoped_evals = str(ROOT / "evals")
    original_path = list(sys.path)
    if scoped_evals not in sys.path:
        sys.path.insert(0, scoped_evals)
    try:
        import litellm

        stub_module = importlib.import_module("memspine_evals.stub_llm")
        install_stub_litellm = stub_module.install_stub_litellm
    finally:
        sys.path[:] = original_path

    with install_stub_litellm():
        import litellm

        original_completion = litellm.acompletion

        async def fixture_completion(**kwargs: Any) -> Any:
            counter["used"] += 1
            if counter["used"] > counter["limit"]:
                raise RuntimeError("deterministic window LLM call cap exceeded")
            messages = kwargs.get("messages") or []
            reply = _deterministic_window_reply(messages)
            if reply is None:
                counter["unexpected_prompt"] = counter.get("unexpected_prompt", 0) + 1
                raise RuntimeError("window request did not match the deterministic PA-121 fixture")
            from types import SimpleNamespace

            message = SimpleNamespace(content=reply, role="assistant")
            usage = SimpleNamespace(
                prompt_tokens=len(json.dumps(kwargs.get("messages"), default=str)) // 4,
                completion_tokens=len(reply) // 4,
                total_tokens=0,
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message, finish_reason="stop", index=0)],
                usage=usage,
                model=kwargs.get("model"),
            )

        litellm.acompletion = fixture_completion
        try:
            yield
        finally:
            litellm.acompletion = original_completion


def _model_dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        raw = value.model_dump(mode="json", exclude_none=False)
    elif isinstance(value, Mapping):
        raw = dict(value)
    else:
        return {}
    return raw if isinstance(raw, dict) else {}


def _message_metadata(record: Any) -> dict[str, Any]:
    message = getattr(record.source, "message", None)
    return _model_dump(message)


def _mention_rows(envelope: Any) -> list[dict[str, Any]]:
    raw = getattr(envelope, "extras", {}).get("gliner_mentions", [])
    return [_model_dump(item) for item in raw]


def _verify_mentions(mentions: list[dict[str, Any]], source_by_id: dict[str, Any]) -> list[str]:
    from memspine.store.events import fingerprint_payload

    errors: list[str] = []
    for index, mention in enumerate(mentions):
        prefix = f"mention[{index}]"
        source_id = mention.get("source_record_id")
        source = source_by_id.get(str(source_id))
        if source is None:
            errors.append(f"{prefix}: no source record for source_record_id")
            continue
        start, end, quote = mention.get("start"), mention.get("end"), mention.get("text")
        if (
            type(start) is not int
            or type(end) is not int
            or not isinstance(quote, str)
            or not (0 <= start < end <= len(source.content))
            or source.content[start:end] != quote
        ):
            errors.append(f"{prefix}: span does not exactly match the source turn")
        if mention.get("asserted") is not False:
            errors.append(f"{prefix}: candidate mention was not marked asserted=false")
        confidence = mention.get("confidence")
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not 0.0 <= float(confidence) <= 1.0
        ):
            errors.append(f"{prefix}: confidence is neither finite [0,1] nor None")
        metadata = _message_metadata(source)
        sender = metadata.get("sender") or {}
        expected = {
            "source_namespace": NAMESPACE,
            "source_message_id": metadata.get("message_id"),
            "sender_id": sender.get("id"),
            "sender_role": metadata.get("role") or source.source.role,
            "source_role": source.source.role,
            "source_channel": source.source.channel,
            "created_at": metadata.get("created_at"),
            "source_version": source.version,
        }
        for key, value in expected.items():
            if mention.get(key) != value:
                errors.append(f"{prefix}: {key} does not match persisted source metadata")
        actual_content_fingerprint = fingerprint_payload({"content": source.content})
        if mention.get("source_content_fingerprint") != actual_content_fingerprint:
            errors.append(f"{prefix}: source content hash does not match actual source text")
        if mention.get("parents") != source.source.parents:
            errors.append(f"{prefix}: parent IDs do not match the source record")
        source_identity = {
            "namespace": source.namespace,
            "record_id": source.record_id,
            "version": source.version,
            "content_fingerprint": actual_content_fingerprint,
            "source_role": source.source.role,
            "source_channel": source.source.channel,
            "source_message_id": expected["source_message_id"],
            "sender_id": expected["sender_id"],
            "sender_role": expected["sender_role"],
            "created_at": expected["created_at"],
            "parents": list(source.source.parents),
        }
        if mention.get("source_binding_hash") != fingerprint_payload(source_identity):
            errors.append(f"{prefix}: source binding hash does not match recomputed identity")
    return errors


def _grounding_summary(envelope: Any, source_by_id: dict[str, Any]) -> dict[str, Any]:
    if envelope is None:
        return {"status": "missing_envelope"}
    evidence_rows: list[dict[str, Any]] = []
    missing_sources: list[str] = []
    exact_quotes = True
    for evidence in envelope.evidence:
        row = _model_dump(evidence)
        turn = _model_dump(getattr(evidence, "turn", None))
        source_id = turn.get("source_id")
        source = source_by_id.get(str(source_id)) if source_id is not None else None
        span = _model_dump(getattr(evidence, "span", None))
        source_version = turn.get("source_version")
        if source is None or source_version != getattr(source, "version", None):
            missing_sources.append(str(source_id))
        if source is not None and not span and getattr(evidence, "role", None) == "support":
            exact_quotes = False
        elif source is not None and span:
            start, end = span.get("start"), span.get("end")
            quote = span.get("quote") or span.get("text")
            if (
                type(start) is not int
                or type(end) is not int
                or not isinstance(quote, str)
                or not (0 <= start < end <= len(source.content))
                or source.content[start:end] != quote
            ):
                exact_quotes = False
        evidence_rows.append(
            {
                "role": row.get("role"),
                "turn_record_id": source_id,
                "turn_source_version": source_version,
                "span": span,
                "source_found": source is not None,
            }
        )
    grounding = getattr(envelope, "grounding", None)
    grounding = getattr(grounding, "value", grounding)
    return {
        "status": "ok" if evidence_rows and not missing_sources and exact_quotes else "failed",
        "grounding": grounding,
        "evidence": evidence_rows,
        "all_turns_resolve_to_ingested_messages": not missing_sources,
        "all_quotes_match_source_content": exact_quotes,
        "missing_source_ids": missing_sources,
    }


def _snapshot_metadata(snapshot: Path) -> dict[str, Any]:
    required = ("config.json", "model.safetensors")
    missing = [name for name in required if not (snapshot / name).is_file()]
    if missing:
        raise FileNotFoundError(f"local GLiNER snapshot is missing {', '.join(missing)}")
    config = snapshot / "config.json"
    with config.open("rb") as stream:
        config_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    return {
        "path": str(snapshot),
        "revision": snapshot.name,
        "files": {name: {"bytes": (snapshot / name).stat().st_size} for name in required},
        "config_sha256": config_sha256,
    }


def _engine_config(database: Path, snapshot: Path) -> dict[str, Any]:
    return {
        "storage": {
            "records": {
                "provider": "sqlite",
                "settings": {"path": str(database), "busy_timeout_s": 5.0},
            }
        },
        "embedding": {"provider": "hash", "settings": {"dim": 64}},
        "memories": {
            "episodic": {"enabled": True},
            "semantic": {
                "enabled": True,
                "policies": {"entity_extraction": "gliner"},
            },
        },
        "models": {
            "profiles": {
                "local-gliner": {
                    "kind": "gliner",
                    "provider": "gliner2",
                    "settings": {
                        "model": str(snapshot),
                        "revision": snapshot.name,
                        "device": "cpu",
                        "local_files_only": True,
                        "tasks": ["entities"],
                        "inference_concurrency": 1,
                    },
                }
            },
            "bindings": {
                "gliner_prepass": "local-gliner",
                "gliner_semantic": "local-gliner",
            },
            "gliner_max_models": 1,
        },
        "write": {
            "window_extract": "facts",
            "window_async": False,
            "window_max_turns": 6,
            "envelope_v2": True,
            "prompt_v2": True,
            "grounding_v2": True,
            "prepass": "gliner",
            "stamping_v2": True,
            "integration_v2": True,
            "lineage_table": True,
        },
        "llm": {
            "roles": {
                "window_extract": {
                    "provider": "litellm",
                    "settings": {"model": "stub/window", "timeout_seconds": 30.0},
                }
            }
        },
        "deployment": {"id": "pa121-local-acceptance"},
        "observability": {"tap": {"enabled": True, "sink": "memory", "content": False}},
    }


async def _run_engine(database: Path, snapshot: Path, counters: dict[str, Any]) -> dict[str, Any]:
    from memspine import Engine

    config = _engine_config(database, snapshot)
    engine = Engine(template="core", dotenv_path=None, **config)
    await engine.start()
    try:
        raw = await engine.ingest_messages(
            list(MESSAGES),
            namespace=NAMESPACE,
            session_id=SESSION_ID,
            conversation_id=CONVERSATION_ID,
        )
        if len(raw) != len(MESSAGES):
            raise RuntimeError(f"expected {len(MESSAGES)} ingested turns, got {len(raw)}")
        source_by_id = {record.record_id: record for record in raw}
        window_stats = await engine.flush_windows(NAMESPACE, final=True)
        window_stage_events = _safe_stage_events()
        # The current public ingestion path stores turns as episodic source records. Semantic
        # mention annotations are attached by the semantic write door, so run one explicit
        # semantic write using the first ingested turn's exact source envelope and parent ID.
        semantic_probe = await engine.write(
            raw[0].content,
            namespace=NAMESPACE,
            memory_type="semantic",
            source=raw[0].source,
            derived_from=[raw[0].record_id],
            actor=raw[0].source.role,
        )
        source_by_id[semantic_probe.record_id] = semantic_probe
        stored = await engine.list_records(namespace=NAMESPACE)
        raw_ids = set(source_by_id)
        facts = [
            record
            for record in stored
            if record.memory_type == "semantic"
            and record.source.channel == "mining"
            and raw_ids.intersection(record.source.parents)
        ]
        mentions: list[dict[str, Any]] = [
            _model_dump(item) for item in semantic_probe.fingerprint.entity_mentions
        ]
        window_mentions: list[dict[str, Any]] = []
        envelopes: dict[str, Any] = {}
        for record in facts:
            envelope = await engine.get_envelope(record.record_id, NAMESPACE)
            if envelope is not None:
                envelopes[record.record_id] = envelope
                envelope_mentions = _mention_rows(envelope)
                window_mentions.extend(envelope_mentions)
                mentions.extend(envelope_mentions)

        mentions_errors = _verify_mentions(mentions, source_by_id)
        semantic_mention_errors = _verify_mentions(
            [_model_dump(item) for item in semantic_probe.fingerprint.entity_mentions],
            source_by_id,
        )
        window_mention_errors = _verify_mentions(window_mentions, source_by_id)
        fact_grounding = [
            {
                "record_id": fact.record_id,
                "content": fact.content,
                "parent_ids": list(fact.source.parents),
                "namespace": fact.namespace,
                "entity": fact.entity,
                "attribute": fact.attribute,
                "grounding": _grounding_summary(envelopes.get(fact.record_id), source_by_id),
            }
            for fact in facts
        ]
        no_mention_fact = not any(
            record.attribute == "mention" for record in stored if record.memory_type == "semantic"
        )
        semantic_annotation_valid = (
            bool(semantic_probe.fingerprint.entity_mentions)
            and not (semantic_mention_errors)
            and (semantic_probe.entity is None and semantic_probe.attribute is None)
        )
        window_mentions_valid = bool(window_mentions) and not window_mention_errors
        other_namespace = await engine.retrieve(namespace=OTHER_NAMESPACE)
        wrong_scope = (
            await engine.get_record(facts[0].record_id, OTHER_NAMESPACE) if facts else None
        )
        search_hits = await engine.search("Where does Mira work?", namespace=NAMESPACE, top_k=10)
        hit_ids = [record.record_id for record, _score in search_hits]
        raw_provenance = []
        raw_errors: list[str] = []
        for index, record in enumerate(raw):
            from memspine.store.events import fingerprint_payload

            metadata = _message_metadata(record)
            expected = MESSAGES[index]
            sender = metadata.get("sender") or {}
            actual_source_text_fingerprint = fingerprint_payload({"content": record.content})
            persisted_role = metadata.get("role") or record.source.role
            row = {
                "record_id": record.record_id,
                "message_id": metadata.get("message_id"),
                "session_id": metadata.get("session_id"),
                "conversation_id": metadata.get("conversation_id"),
                "reply_to": metadata.get("reply_to"),
                "role": persisted_role,
                "sender": sender,
                "created_at": metadata.get("created_at"),
                "content": record.content,
                "actual_source_text_fingerprint": actual_source_text_fingerprint,
                "stored_content_fingerprint_matches_actual": (
                    record.content_fingerprint == actual_source_text_fingerprint
                ),
                "namespace": record.namespace,
            }
            raw_provenance.append(row)
            for key in (
                "message_id",
                "session_id",
                "conversation_id",
                "reply_to",
                "role",
                "created_at",
            ):
                actual_value = persisted_role if key == "role" else metadata.get(key)
                if expected.get(key) != actual_value:
                    raw_errors.append(f"raw[{index}].{key} changed during canonical intake")
            if sender != expected["sender"]:
                raw_errors.append(f"raw[{index}].sender changed during canonical intake")
            if record.content != expected["content"] or record.namespace != NAMESPACE:
                raw_errors.append(f"raw[{index}] content or namespace changed during intake")

        window_fact_valid = bool(fact_grounding)
        for item in fact_grounding:
            grounding_row = item.get("grounding")
            parent_ids = item.get("parent_ids")
            if not isinstance(grounding_row, dict) or not (
                grounding_row.get("status") == "ok"
                and grounding_row.get("grounding") == "grounded"
                and isinstance(parent_ids, list)
                and all(isinstance(parent, str) for parent in parent_ids)
                and set(parent_ids) <= raw_ids
                and item["namespace"] == NAMESPACE
            ):
                window_fact_valid = False
        report = {
            "schema_version": 1,
            "task": "PA-121",
            "status": "passed"
            if (
                not raw_errors
                and not mentions_errors
                and semantic_annotation_valid
                and window_mentions_valid
                and counters["gliner"]["used"] > 0
                and counters["window_llm"]["used"] > 0
                and counters["window_llm"].get("unexpected_prompt", 0) == 0
                and window_fact_valid
                and no_mention_fact
                and not other_namespace
                and wrong_scope is None
                and bool(set(record.record_id for record in facts) & set(hit_ids))
            )
            else "failed",
            "safety": {
                "synthetic_content_only": True,
                "gliner_is_real_local_sdk_inference": True,
                "gliner_downloads": False,
                "gliner_hub_offline": os.environ.get("HF_HUB_OFFLINE") == "1",
                "transformers_offline": os.environ.get("TRANSFORMERS_OFFLINE") == "1",
                "external_llm_endpoint_calls": False,
                "window_llm": "deterministic PA-121 supported-claim fixture (not a real generative model)",
                "paid_calls": False,
                "tap_content_capture": False,
            },
            "runtime": {
                "python": platform.python_version(),
                "packages": {
                    package: importlib.metadata.version(package)
                    for package in ("gliner2", "torch", "transformers")
                },
                "device": "cpu",
                "timeout_seconds": TIMEOUT_SECONDS,
                "max_gliner_calls": counters["gliner"]["limit"],
                "gliner_calls_used": counters["gliner"]["used"],
                "max_window_fixture_calls": counters["window_llm"]["limit"],
                "window_fixture_calls_used": counters["window_llm"]["used"],
                "unexpected_window_prompts": counters["window_llm"].get("unexpected_prompt", 0),
            },
            "model": {
                "provider": "gliner2.AutoExtractor",
                "profile": "local-gliner",
                "bindings": ["gliner_prepass", "gliner_semantic"],
                "tasks": ["entities"],
                "local_files_only": True,
                "snapshot": _snapshot_metadata(snapshot),
            },
            "engine": {
                "api": "Engine.ingest_messages -> Engine.flush_windows(final=True)",
                "same_engine_for_ingest_and_flush": True,
                "sqlite_database_file_backed": database.is_file(),
                "storage_path_temporary": True,
                "provider_catalog": _safe_provider_rows(await engine.provider_catalog()),
            },
            "source_messages": {
                "namespace": NAMESPACE,
                "session_id": SESSION_ID,
                "conversation_id": CONVERSATION_ID,
                "requested_event_time": EVENT_TIME,
                "rows": raw_provenance,
                "status": "passed" if not raw_errors else "failed",
                "errors": raw_errors,
            },
            "window": {
                "flush_stats": _safe_window_stats(window_stats),
                "stage_events": window_stage_events,
                "fact_count": len(facts),
                "facts": fact_grounding,
                "status": "passed" if window_fact_valid else "failed",
            },
            "mentions": {
                "count": len(mentions),
                "samples": mentions[:24],
                "semantic_write_record_id": semantic_probe.record_id,
                "source_raw_turn_parent_id": raw[0].record_id,
                "semantic_door_probe_is_separately_reported": True,
                "all_exact_source_spans_and_attributions": not mentions_errors and bool(mentions),
                "window_mentions_count": len(window_mentions),
                "window_mentions_valid": window_mentions_valid,
                "semantic_probe_mentions_valid": semantic_annotation_valid,
                "asserted_false": all(item.get("asserted") is False for item in mentions),
                "confidence_valid_or_none": all(
                    item.get("confidence") is None
                    or (
                        isinstance(item.get("confidence"), (int, float))
                        and not isinstance(item.get("confidence"), bool)
                        and math.isfinite(float(item["confidence"]))
                        and 0 <= float(item["confidence"]) <= 1
                    )
                    for item in mentions
                ),
                "errors": mentions_errors,
                "semantic_probe_errors": semantic_mention_errors,
                "window_errors": window_mention_errors,
            },
            "semantic_annotation_probe": {
                "method": "Engine.write(memory_type='semantic') on the first ingested turn's exact content",
                "separate_from_message_ingest_and_window_flush": True,
                "record_id": semantic_probe.record_id,
                "parent_message_record_id": raw[0].record_id,
                "entity_mentions_count": len(semantic_probe.fingerprint.entity_mentions),
                "has_entity_or_attribute_fact_key": (
                    semantic_probe.entity is not None or semantic_probe.attribute is not None
                ),
                "status": "passed" if semantic_annotation_valid else "failed",
            },
            "mention_fact_separation": {
                "no_semantic_fact_with_attribute_mention": no_mention_fact,
                "status": "passed" if no_mention_fact else "failed",
            },
            "namespace_isolation": {
                "other_namespace_record_count": len(other_namespace),
                "cross_namespace_get_record_is_hidden": wrong_scope is None,
                "status": "passed" if not other_namespace and wrong_scope is None else "failed",
            },
            "retrieval": {
                "query": "Where does Mira work?",
                "hit_record_ids": hit_ids,
                "window_fact_retrieved": bool(
                    set(record.record_id for record in facts) & set(hit_ids)
                ),
                "status": "passed"
                if set(record.record_id for record in facts) & set(hit_ids)
                else "failed",
            },
            "limits": [
                "One cached Base snapshot and one synthetic conversation; this is not a quality benchmark.",
                "Window compilation uses a deterministic fixture, so no claim is made about generative model quality or endpoint behavior.",
                "This focused acceptance does not cover all nine memory types or all provider combinations.",
                "No CPU performance claim is made.",
            ],
        }
        return report
    finally:
        await engine.stop()


async def _verify_reopen(
    database: Path, snapshot: Path, original: dict[str, Any]
) -> dict[str, Any]:
    """Reopen the same file-backed event store and verify persisted source/fact projection state."""
    from memspine import Engine

    engine = Engine(template="core", dotenv_path=None, **_engine_config(database, snapshot))
    await engine.start()
    try:
        records = await engine.list_records(namespace=NAMESPACE)
        by_id = {record.record_id: record for record in records}
        source_rows = original.get("source_messages", {}).get("rows", [])
        fact_rows = original.get("window", {}).get("facts", [])
        semantic_probe_id = original.get("mentions", {}).get("semantic_write_record_id")
        expected_source_ids = {row["record_id"] for row in source_rows}
        expected_fact_ids = {row["record_id"] for row in fact_rows}
        present = expected_source_ids | expected_fact_ids
        if isinstance(semantic_probe_id, str):
            present.add(semantic_probe_id)
        mention_count = 0
        for fact_id in expected_fact_ids:
            envelope = await engine.get_envelope(fact_id, NAMESPACE)
            if envelope is not None:
                mention_count += len(_mention_rows(envelope))
        semantic_probe = (
            by_id.get(semantic_probe_id) if isinstance(semantic_probe_id, str) else None
        )
        persisted_semantic_mentions = (
            len(semantic_probe.fingerprint.entity_mentions) if semantic_probe is not None else 0
        )
        ok = bool(present) and present <= set(by_id)
        ok = ok and persisted_semantic_mentions > 0
        return {
            "status": "passed" if ok else "failed",
            "expected_source_ids": sorted(expected_source_ids),
            "expected_window_fact_ids": sorted(expected_fact_ids),
            "all_ids_reopened": present <= set(by_id),
            "reopened_mention_count": mention_count,
            "reopened_semantic_probe_id": semantic_probe_id,
            "reopened_semantic_annotation_count": persisted_semantic_mentions,
        }
    finally:
        await engine.stop()


def _safe_provider_rows(rows: Any) -> list[dict[str, Any]]:
    allowed = (
        "logical_service_id",
        "category",
        "configured_provider",
        "resolved_provider",
        "lifecycle_state",
    )
    if not isinstance(rows, list):
        return []
    return [
        {key: row[key] for key in allowed if key in row} for row in rows if isinstance(row, dict)
    ]


def _safe_window_stats(stats: Any) -> dict[str, Any]:
    if not isinstance(stats, Mapping):
        return {}
    allowed = (
        "windows_processed",
        "calls",
        "facts_returned",
        "facts_deposited",
        "items_valid",
        "items_dropped",
        "grounded",
        "ground_dropped",
        "facts_invalid",
        "facts_ungrounded",
        "facts_uncited",
        "facts_skipped_parent_gone",
        "facts_deposit_errors",
        "items_dead",
        "items_duplicate",
        "dead_letters",
        "items_dead_by_code",
        "envelope_stages",
    )
    return {
        key: stats[key]
        for key in allowed
        if key in stats and isinstance(stats[key], (str, int, float, bool, type(None), Mapping))
    }


def _safe_stage_events() -> list[dict[str, Any]]:
    """Return selected content-free tap diagnostics for window stage outcomes."""
    from memspine.observability import tap

    allowed_stages = {
        "write.item.dead",
        "write.item.valid",
        "write.item.dropped",
        "write.item.accepted",
        "write.envelope.compile",
        "write.extract.facts",
        "write.extract.edges",
        "write.extract.episode",
        "write.extract.rejected",
        "write.extract.deposit",
        "write.envelope.stage",
        "write.ground.summary",
        "write.ground.checked",
        "write.integrate.decision",
        "write.integrate.stage",
    }
    allowed_fields = {
        "stage",
        "reason",
        "layer",
        "before",
        "after",
        "label",
        "rules",
        "op",
        "rung",
        "keyed",
        "error",
        "parents",
        "code",
        "count",
        "status",
        "decision",
        "why",
    }
    rows: list[dict[str, Any]] = []

    def visit(trace: Mapping[str, Any]) -> None:
        for event in trace.get("events", []):
            if not isinstance(event, Mapping) or event.get("stage") not in allowed_stages:
                continue
            fields = event.get("fields", {})
            if isinstance(fields, Mapping):
                safe = {
                    key: value
                    for key, value in fields.items()
                    if key in allowed_fields
                    and isinstance(value, (str, int, float, bool, type(None)))
                }
            else:
                safe = {}
            rows.append({"stage": event["stage"], "fields": safe})
        for child in trace.get("children", []):
            if isinstance(child, Mapping):
                visit(child)

    for trace in tap.memory_traces():
        if isinstance(trace, Mapping):
            visit(trace)
    return rows


def run(snapshot: Path, *, timeout_s: float = TIMEOUT_SECONDS) -> dict[str, Any]:
    snapshot = snapshot.expanduser().resolve(strict=True)
    if snapshot.name != DEFAULT_REVISION:
        raise ValueError(
            f"expected the approved cached snapshot revision {DEFAULT_REVISION}, got {snapshot.name}"
        )
    _snapshot_metadata(snapshot)
    if timeout_s <= 0 or timeout_s > TIMEOUT_SECONDS:
        raise ValueError(f"timeout_s must be in (0, {TIMEOUT_SECONDS}] seconds")

    previous_environment = {
        key: os.environ.get(key)
        for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_DISABLE_TELEMETRY")
    }
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    counters = {
        "gliner": {"used": 0, "limit": MAX_GLINER_CALLS},
        "window_llm": {"used": 0, "limit": MAX_WINDOW_LLM_CALLS},
    }
    deadline = time.monotonic() + timeout_s

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError(f"acceptance exceeded its total {timeout_s:g}s deadline")
        return value

    try:
        with tempfile.TemporaryDirectory(prefix="memspine-pa121-") as temp:
            database = Path(temp) / "acceptance.sqlite3"
            with (
                _offline_gliner(counters["gliner"]),
                _offline_window_transport(counters["window_llm"]),
            ):
                report = asyncio.run(
                    asyncio.wait_for(_run_engine(database, snapshot, counters), remaining())
                )
                replay = asyncio.run(
                    asyncio.wait_for(_verify_reopen(database, snapshot, report), remaining())
                )
                report["reopen_projection"] = replay
                if replay["status"] != "passed":
                    report["status"] = "failed"
                return report
    finally:
        for key, value in previous_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-directory", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--timeout-s", type=float, default=TIMEOUT_SECONDS)
    args = parser.parse_args()
    started = time.perf_counter()
    try:
        report = run(args.model_directory, timeout_s=args.timeout_s)
    except Exception as exc:
        report = {
            "schema_version": 1,
            "task": "PA-121",
            "status": "failed",
            "error_class": type(exc).__name__,
            "diagnostic": str(exc)[:1000],
            "elapsed_seconds": time.perf_counter() - started,
        }
    report["elapsed_seconds"] = time.perf_counter() - started
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8"
    )
    print(f"GLiNER ingestion acceptance: {report.get('status', 'failed')}")
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
