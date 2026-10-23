"""Run one synthetic message through persisted ingest, derivation, offline and retrieval.

From the repository root, with ``PYTHONPATH=src``::

    python scripts/trace_memory_lifecycle.py

The extraction call uses the deterministic offline hook from ``docs/v2/flows/f01_ingest.py``.
No model download, external endpoint, or paid call is used. The generated JSON intentionally
contains only synthetic source text and identifiers, stage summaries, and provider names.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

NAMESPACE = "pa084-lifecycle"
SESSION_ID = "pa084-session-001"
CONVERSATION_ID = "pa084-conversation-001"
MESSAGE_ID = "pa084-message-001"
SOURCE_TEXT = "Mira: I collect vintage maps in Utrecht."
EVENT_TIME = "2024-03-09T14:45:00+05:30"


def _offline_window_reply(messages: Any) -> str | None:
    from memspine_evals.inspect.step import _offline_window_reply as reply_for_messages

    return reply_for_messages(messages)


@contextmanager
def _offline_transport() -> Iterator[None]:
    """Install the F1.8 deterministic response within a fully restoring stub scope."""
    scoped_paths = [str(ROOT / "docs" / "v2"), str(ROOT / "evals")]
    original_sys_path = list(sys.path)
    added_paths = [path for path in scoped_paths if path not in sys.path]
    sys.path[:0] = added_paths
    try:
        import litellm
        from memspine_evals.stub_llm import install_stub_litellm

        with install_stub_litellm():
            original = litellm.acompletion

            async def window_aware(**kwargs: Any) -> Any:
                reply = _offline_window_reply(kwargs.get("messages") or [])
                if reply is None:
                    return await original(**kwargs)
                from types import SimpleNamespace

                result = SimpleNamespace(content=reply, role="assistant")
                usage = SimpleNamespace(
                    prompt_tokens=len(json.dumps(kwargs.get("messages"))) // 4,
                    completion_tokens=len(reply) // 4,
                    total_tokens=0,
                )
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=result, finish_reason="stop", index=0)],
                    usage=usage,
                    model=kwargs.get("model"),
                )

            litellm.acompletion = window_aware
            try:
                yield
            finally:
                # Restore the scoped stub first; install_stub_litellm then restores the
                # exact process-wide transport functions that existed on entry.
                litellm.acompletion = original
    finally:
        # Imports made by the offline flow can also reorder the repo path. Restore
        # the exact entry state so the caller's paths and their order are preserved.
        sys.path[:] = original_sys_path


def _safe_provider_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep provider identity and selected status while excluding settings and errors."""
    allowed = (
        "logical_service_id",
        "category",
        "configured_provider",
        "resolved_provider",
        "lifecycle_state",
        "readiness_state",
    )
    return [
        {key: row[key] for key in allowed if key in row}
        for row in rows
        if row.get("logical_service_id")
    ]


def _source_message(record: Any) -> dict[str, Any]:
    source = record.source
    message = getattr(source, "message", None)
    if message is None:
        return {}
    if hasattr(message, "model_dump"):
        raw = message.model_dump(mode="json", exclude_none=True)
    elif isinstance(message, dict):
        raw = dict(message)
    else:
        return {}
    # Only source identity/time fields are useful to this trace; omit fingerprints
    # and any future payload fields from the report by construction.
    return {
        key: raw[key]
        for key in (
            "message_id",
            "session_id",
            "conversation_id",
            "sender",
            "created_at",
            "reply_to",
            "created_at_generated",
        )
        if key in raw
    }


def _record_evidence(record: Any) -> dict[str, Any]:
    source = record.source
    return {
        "record_id": record.record_id,
        "namespace": record.namespace,
        "memory_type": record.memory_type,
        "layer": record.layer,
        "status": record.status.value,
        "content": record.content,
        "valid_from": record.valid_from.isoformat(),
        "source_precision": record.source_precision,
        "source_channel": source.channel,
        "parent_ids": list(source.parents),
        "tags": list(record.tags),
        "message": _source_message(record),
    }


def _offline_stage_outcome(
    stages: dict[str, dict[str, Any]],
) -> tuple[str, dict[str, dict[str, Any]]]:
    failures = {
        name: {"status": row.get("status", "unreported")}
        for name, row in stages.items()
        if row.get("status") not in {"ok", "noop", "skipped"}
    }
    if not stages:
        failures["sleep"] = {"status": "unreported"}
    return ("failed" if failures else "ok", failures)


async def run_trace(database: Path) -> dict[str, Any]:
    if database.exists():
        raise FileExistsError(f"refusing to use existing SQLite path: {database}")
    with _offline_transport():
        return await _run_trace_in_scope(database)


async def _run_trace_in_scope(database: Path) -> dict[str, Any]:
    from flows.f01_ingest import CONFIG

    from memspine import Engine

    database.parent.mkdir(parents=True, exist_ok=True)
    config = {
        **CONFIG,
        "storage": {
            "records": {
                "provider": "sqlite",
                "settings": {"path": str(database), "busy_timeout_s": 5.0},
            },
            "vector": {"provider": "lancedb", "settings": {"isolation": "shared"}},
            "lexical": {"provider": "tantivy", "settings": {}},
        },
        "model": {"embedding": {"provider": "hash", "settings": {"dim": 64}}},
        "read": {**CONFIG["read"], "hybrid": True},
        "deployment": CONFIG["deployment"],
        "observability": {"tap": {"enabled": True, "sink": "memory", "content": False}},
    }

    engine = None
    engine_started = False
    try:
        engine = Engine(template="core", dotenv_path=None, **config)
        await engine.start()
        engine_started = True
        catalog = _safe_provider_rows(await engine.provider_catalog())

        def selected_provider(category: str) -> str | None:
            return next(
                (
                    str(row["resolved_provider"])
                    for row in catalog
                    if row.get("category") == category
                ),
                None,
            )

        raw_rows = await engine.ingest_messages(
            [
                {
                    "message_id": MESSAGE_ID,
                    "session_id": SESSION_ID,
                    "conversation_id": CONVERSATION_ID,
                    "role": "user",
                    "sender": {"id": "mira", "kind": "human", "name": "Mira"},
                    "content": SOURCE_TEXT,
                    "created_at": EVENT_TIME,
                }
            ],
            namespace=NAMESPACE,
            session_id=SESSION_ID,
            conversation_id=CONVERSATION_ID,
        )
        if len(raw_rows) != 1:
            raise RuntimeError(f"expected one raw ingested turn, got {len(raw_rows)}")
        raw = raw_rows[0]

        derive = await engine.flush_windows(NAMESPACE, final=True)
        records = await engine.list_records(namespace=NAMESPACE)
        raw_ids = {raw.record_id}
        derived = [
            rec
            for rec in records
            if rec.record_id != raw.record_id
            and rec.source.channel == "mining"
            and raw_ids.intersection(rec.source.parents)
        ]

        offline = await engine.sleep()
        if hasattr(offline, "wait"):
            offline = await offline.wait()
        offline_rows = {
            name: {
                key: value
                for key, value in stats.items()
                if key
                in {
                    "status",
                    "reason",
                    "records",
                    "summaries",
                    "written",
                    "built",
                    "updated",
                    "deleted",
                    "examined",
                }
                and (key != "reason" or stats.get("status") in {"skipped", "noop"})
                and isinstance(value, (str, int, float, bool, type(None)))
            }
            for name, stats in offline.items()
        }
        offline_outcome, offline_failures = _offline_stage_outcome(offline_rows)

        query = "Where do I collect vintage maps?"
        hits = await engine.search(query, namespace=NAMESPACE, top_k=10)
        hit_ids = [rec.record_id for rec, _ in hits]
        lexical_hits = await engine._lexical_leg(NAMESPACE, "vintage maps Utrecht", 10)
        vector = (await engine._embedder.embed([query]))[0]
        vector_hits = await engine._vector_leg(NAMESPACE, list(vector), 10)
        all_after = await engine.list_records(namespace=NAMESPACE)

        source_message = _source_message(raw)
        derived_ids = {rec.record_id for rec in derived}
        compile_edges = offline_rows.get("compile_edges", {})
        graph_outcome = (
            "ok"
            if compile_edges.get("status") == "ok"
            else f"{compile_edges.get('status', 'unreported')}"
        )
        report = {
            "schema_version": 1,
            "trace": "PA-084",
            "safety": {
                "synthetic_content_only": True,
                "external_model_or_network_calls": False,
                "model_downloads": False,
                "paid_calls": False,
                "content_capture": False,
            },
            "configuration": {
                "storage": "sqlite",
                "vector": "lancedb",
                "lexical": "tantivy",
                "graph": selected_provider("graph"),
                "embedding": "hash",
                "database_file_backed": True,
                "sqlite_file_exists_during_trace": database.is_file(),
                "same_engine_for_all_four_stages": True,
            },
            "provider_selection": catalog,
            "source": {
                "namespace": NAMESPACE,
                "session_id": SESSION_ID,
                "conversation_id": CONVERSATION_ID,
                "message_id": MESSAGE_ID,
                "event_time": source_message.get("created_at"),
                "timestamp_matches_input": source_message.get("created_at") == EVENT_TIME,
                "sender": source_message.get("sender"),
                "content": SOURCE_TEXT,
            },
            "stages": {
                "ingest": {
                    "outcome": "ok",
                    "raw_turn": _record_evidence(raw),
                    "source_identity_matches": (
                        source_message.get("message_id") == MESSAGE_ID
                        and source_message.get("session_id") == SESSION_ID
                        and source_message.get("conversation_id") == CONVERSATION_ID
                    ),
                    "timestamp_matches_input": source_message.get("created_at") == EVENT_TIME,
                    "sender_identity": source_message.get("sender"),
                },
                "derive": {
                    "outcome": "ok"
                    if derive.get("facts_deposited", 0) and derived
                    else "no_derived_fact",
                    "method": "flush_windows with deterministic existing offline extraction stub",
                    "stats": {
                        key: derive.get(key)
                        for key in (
                            "windows_processed",
                            "calls",
                            "facts_returned",
                            "facts_deposited",
                            "items_valid",
                        )
                    },
                    "derived_records": [_record_evidence(rec) for rec in derived],
                    "lineage_valid": bool(derived)
                    and all(set(rec.source.parents) == raw_ids for rec in derived),
                },
                "offline": {
                    "outcome": offline_outcome,
                    "failed_stages": offline_failures,
                    "stages": offline_rows,
                },
                "retrieval": {
                    "outcome": "ok" if derived_ids.intersection(hit_ids) else "no_derived_match",
                    "query": query,
                    "engine_search_record_ids": hit_ids,
                    "vector_leg_record_ids": [hit.record_id for hit in vector_hits],
                    "lexical_leg_record_ids": [hit.record_id for hit in lexical_hits],
                    "raw_or_derived_found": any(
                        rec_id in hit_ids
                        for rec_id in [raw.record_id, *(r.record_id for r in derived)]
                    ),
                    "raw_record_found": raw.record_id in hit_ids,
                    "derived_record_found": bool(derived_ids.intersection(hit_ids)),
                    "namespace": NAMESPACE,
                    "record_count_after_offline": len(all_after),
                },
            },
            "provider_and_coverage_boundaries": {
                "graph": {
                    "provider": selected_provider("graph"),
                    "outcome": graph_outcome,
                    "written_edges": offline.get("compile_edges", {}).get("written", 0),
                },
                "reranker": {"provider": "off", "outcome": "disabled for this trace"},
                "cache": {
                    "provider": "memory",
                    "outcome": "not a persistence/retrieval acceptance target",
                },
                "other_memory_types": "not exercised; this trace does not cover all nine type/provider combinations",
            },
        }
        return report
    finally:
        if engine is not None and engine_started:
            await engine.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        help="new SQLite path only; existing paths are refused (default: temporary file)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "docs" / "provider_verification" / "memory-lifecycle-trace.json",
    )
    args = parser.parse_args()
    if args.database:
        report = asyncio.run(run_trace(args.database.resolve()))
    else:
        with tempfile.TemporaryDirectory(prefix="memspine-pa084-") as temp:
            report = asyncio.run(run_trace(Path(temp) / "memory.sqlite"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Wrote {args.output}")
    for name, stage in report["stages"].items():
        print(f"{name}: {stage['outcome']}")
    return 0 if all(stage["outcome"] == "ok" for stage in report["stages"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
