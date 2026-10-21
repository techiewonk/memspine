"""Benchmark JSON implementation alternatives on real MemSpine model payloads.

This is an evidence tool, not a global encoder selector. It compares output
semantics and JSON-mode validation behavior as well as throughput. Run with the
repository's Python 3.13 dependency environment, for example:

    python scripts/benchmark_json_paths.py --output docs/provider_verification/json-path-benchmark.json
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import statistics
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import orjson
import xxhash

from memspine.providers.changes import (
    ChangePlan,
    ChangeRequest,
    ChangeScope,
    ChangeStatus,
    DependencyRevision,
    OperationCheckpoint,
    OperationRecord,
    PlanStep,
    ResourceEstimate,
    SourceCompleteness,
    TargetConfigRef,
)
from memspine.store.envelope import from_envelope, to_envelope
from memspine.store.events import EventKind, MemoryEvent, canonical_payload, fingerprint_payload
from memspine.store.records import MemoryRecord, SourceInfo


def _operation_record() -> OperationRecord:
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    completeness = SourceCompleteness(complete=True, evidence_id="bench-source")
    scope = ChangeScope(
        deployment_id="bench-deployment",
        owner_id="bench-deployment",
        resource_class="model_config",
    )
    config_ref = TargetConfigRef(
        provider="litellm", config_id="bench-config", semantic_fingerprint="target-digest"
    )
    request = ChangeRequest(
        category="llm",
        logical_service_id="llm.chat",
        source_revision="rev-old",
        target_revision="rev-new",
        scope=scope,
        target_config_ref=config_ref,
        requested_strategy="forward_revision",
        actor="benchmark",
        dependency_revisions=tuple(
            DependencyRevision(service_id=f"dependency.{i}", revision=f"r{i}") for i in range(12)
        ),
        source_completeness=completeness,
    )
    plan = ChangePlan(
        plan_id="plan-bench",
        request=request,
        source_semantic_fingerprint="source-digest",
        target_semantic_fingerprint="target-digest",
        dependency_revisions=request.dependency_revisions,
        steps=tuple(
            PlanStep(step_id=f"step-{i}", phase="verify", description=f"verify component {i}")
            for i in range(12)
        ),
        required_capabilities=frozenset({"snapshot", "verify", "activate"}),
        source_completeness=completeness,
        estimate=ResourceEstimate(bytes=1024 * 1024, estimated_cost=0.0, model_calls=0),
        created_at=now,
        expires_at=now + timedelta(minutes=30),
        supported=True,
    )
    detail_rows = [
        {
            "component": f"component-{i}",
            "revision": f"r{i}",
            "bytes": 256 * i,
            "healthy": i % 3 != 0,
            "note": "migrated café — verified",
        }
        for i in range(48)
    ]
    return OperationRecord(
        operation_id="operation-bench",
        plan_id=plan.plan_id,
        deployment_id=scope.deployment_id,
        status=ChangeStatus.RUNNING,
        operation_revision=7,
        fence_epoch=11,
        checkpoint=OperationCheckpoint(
            phase="verify",
            source_checkpoint=2048,
            target_checkpoint=2048,
            details={"items": detail_rows},
        ),
        created_at=now,
        updated_at=now,
    )


def _event_payload() -> dict[str, Any]:
    return {
        "record_id": "rec-bench",
        "text": "Mira moved to Porto — café data",
        "scores": [0.1, 1.0, -0.0, 1.2345678901234567],
        "ordinal": 2**53 + 1,
        "tags": ["semantic", "user-stated"],
        "nested": {"a": [1, True, None], "é": "☕"},
    }


def _build_orjson_guard(value: Any) -> bool:
    """Return true only for std/orjson JSON trees with matching valid value semantics."""
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, int):
        return -(2**63) <= value <= 2**64 - 1
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return False
        return True
    if isinstance(value, (list, tuple)):
        return all(_build_orjson_guard(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _build_orjson_guard(key) and _build_orjson_guard(item)
            for key, item in value.items()
        )
    return False


def _attempt(call: Callable[[], Any]) -> dict[str, Any]:
    try:
        result = call()
    except Exception as exc:  # evidence probes intentionally collect boundary behavior
        return {"status": "error", "error_type": type(exc).__name__, "error": str(exc)[:180]}
    return {"status": "ok", "value_type": type(result).__name__}


def _value_summary(value: Any) -> Any:
    if isinstance(value, float) and math.isnan(value):
        return "NaN"
    if isinstance(value, float) and math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, datetime):
        return {"type": "datetime", "iso": value.isoformat()}
    if isinstance(value, dict):
        return {str(key): _value_summary(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_value_summary(item) for item in value]
    if isinstance(value, int) and not isinstance(value, bool) and value > 2**64 - 1:
        return {"type": "bigint", "decimal": str(value)}
    if isinstance(value, str):
        return value.encode("unicode_escape").decode("ascii")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return {"type": type(value).__name__}


def _validate_probe(payload_bytes: bytes) -> dict[str, Any]:
    try:
        result = OperationRecord.model_validate_json(payload_bytes)
    except Exception as exc:  # collect whether each encoder preserves parser failure
        return {"status": "error", "error_type": type(exc).__name__}
    return {"status": "ok", "value": _value_summary(result.checkpoint.details.get("probe"))}


def _probe_values(operation: OperationRecord) -> dict[str, Any]:
    base = operation.model_dump(mode="json")
    python_base = operation.model_dump(mode="python")
    cases: dict[str, Any] = {
        "nan": float("nan"),
        "positive_infinity": float("inf"),
        "negative_infinity": float("-inf"),
        "integer_above_u64": 2**80,
        "non_string_key": {7: "value"},
        "lone_surrogate": "bad-\ud800-value",
        "datetime_object": datetime(2026, 1, 1, tzinfo=UTC),
        "custom_object": object(),
    }
    outcomes: dict[str, Any] = {}
    for name, value in cases.items():
        payload = copy.deepcopy(base)
        payload["checkpoint"]["details"] = {"probe": value}
        python_payload = copy.deepcopy(python_base)
        python_payload["checkpoint"]["details"] = {"probe": value}
        outcome: dict[str, Any] = {"guard_allows_orjson": _build_orjson_guard(payload)}
        std_bytes: bytes | None = None
        orjson_bytes: bytes | None = None
        try:
            std_bytes = json.dumps(payload).encode("utf-8")
            outcome["stdlib_encode"] = {"status": "ok", "bytes": len(std_bytes)}
        except Exception as exc:
            outcome["stdlib_encode"] = {"status": "error", "error_type": type(exc).__name__}
        try:
            orjson_bytes = orjson.dumps(payload)
            outcome["orjson_encode"] = {"status": "ok", "bytes": len(orjson_bytes)}
        except Exception as exc:
            outcome["orjson_encode"] = {"status": "error", "error_type": type(exc).__name__}
        if std_bytes is not None:
            outcome["stdlib_jsonmode_validate"] = _validate_probe(std_bytes)
            compact_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            outcome["stdlib_compact_jsonmode_validate"] = _validate_probe(compact_bytes)
        if orjson_bytes is not None:
            outcome["orjson_jsonmode_validate"] = _validate_probe(orjson_bytes)
        if outcome["guard_allows_orjson"]:
            outcome["guarded_jsonmode_validate"] = _validate_probe(orjson.dumps(payload))
        elif std_bytes is not None:
            outcome["guarded_jsonmode_validate"] = _validate_probe(std_bytes)
        outcome["pythonmode_validate"] = _attempt(
            lambda payload=python_payload: OperationRecord.model_validate(payload)
        )
        outcome["pythonmode_value"] = _value_summary(
            OperationRecord.model_validate(python_payload).checkpoint.details.get("probe")
        )
        outcomes[name] = outcome
    return outcomes


def _event_byte_probes() -> dict[str, Any]:
    cases = {
        "unicode": {"text": "café ☕"},
        "nan": {"value": float("nan")},
        "positive_infinity": {"value": float("inf")},
        "integer_above_u64": {"value": 2**80},
        "negative_zero_and_float": {"values": [-0.0, 1.2345678901234567]},
    }
    result: dict[str, Any] = {}
    for name, payload in cases.items():
        item: dict[str, Any] = {}
        try:
            raw = canonical_payload(payload)
            item["orjson_sorted"] = raw.decode("utf-8", errors="backslashreplace")
            item["orjson_fingerprint"] = fingerprint_payload(payload)
            item["orjson_sha256"] = hashlib.sha256(raw).hexdigest()
        except Exception as exc:
            item["orjson_sorted"] = {"error_type": type(exc).__name__}
        for allow_nan in (False, True):
            try:
                raw = json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=allow_nan,
                ).encode("utf-8")
                label = "stdlib_sorted_allow_nan" if allow_nan else "stdlib_sorted_strict"
                item[label] = raw.decode("utf-8", errors="backslashreplace")
                if name == "unicode":
                    item[label + "_sha256"] = hashlib.sha256(raw).hexdigest()
            except Exception as exc:
                label = "stdlib_sorted_allow_nan" if allow_nan else "stdlib_sorted_strict"
                item[label] = {"error_type": type(exc).__name__}
        result[name] = item
    return result


def _bench(name: str, call: Callable[[], Any], *, loops: int, repeats: int) -> dict[str, Any]:
    samples: list[float] = []
    call()  # warm runtime caches/import paths outside the timed samples
    for _ in range(repeats):
        start = time.perf_counter()
        for _ in range(loops):
            call()
        samples.append(time.perf_counter() - start)
    median = statistics.median(samples)
    return {
        "name": name,
        "loops_per_repeat": loops,
        "repeats": repeats,
        "median_seconds": round(median, 6),
        "ops_per_second": round(loops / median, 1),
        "samples_seconds": [round(item, 6) for item in samples],
    }


def _package_versions() -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for package in ("orjson", "pydantic", "pydantic-core", "sqlalchemy"):
        try:
            result[package] = version(package)
        except PackageNotFoundError:
            result[package] = None
    return result


def run(*, loops: int, repeats: int) -> dict[str, Any]:
    operation = _operation_record()
    operation_json = operation.model_dump(mode="json")
    std_operation = json.dumps(operation_json).encode("utf-8")
    orjson_operation = orjson.dumps(operation_json)
    payload = _event_payload()
    event = MemoryEvent(
        event_id="event-bench",
        seq=17,
        kind=EventKind.WRITE,
        namespace="bench/user",
        ts=datetime(2026, 6, 1, 12, tzinfo=UTC),
        actor="benchmark",
        payload=payload,
    )
    record = MemoryRecord(
        record_id="record-bench",
        namespace="bench/user",
        memory_type="semantic",
        content="Mira moved to Porto — café data",
        entity="Mira",
        attribute="location",
        source=SourceInfo(role="user", channel="benchmark", message_id="turn-bench"),
    )
    envelope = to_envelope(record)
    envelope_python_json = envelope.model_dump(mode="json")
    envelope_pydantic_json = envelope.model_dump_json().encode("utf-8")
    envelope_orjson = orjson.dumps(envelope_python_json)
    envelope_stdlib = json.dumps(
        envelope_python_json, ensure_ascii=False, separators=(",", ":")
    ).encode()

    canonical_event = canonical_payload(event.payload)
    std_canonical_options = {
        "sort_keys": True,
        "separators": (",", ":"),
        "ensure_ascii": False,
        "allow_nan": False,
    }
    std_event = json.dumps(event.payload, **std_canonical_options).encode("utf-8")

    validated_std = OperationRecord.model_validate_json(std_operation)
    validated_orjson = OperationRecord.model_validate_json(orjson_operation)
    validated_python = OperationRecord.model_validate(operation.model_dump(mode="python"))
    semantically_equal = (
        validated_std.model_dump(mode="json")
        == validated_orjson.model_dump(mode="json")
        == validated_python.model_dump(mode="json")
    )
    envelope_roundtrip = from_envelope(envelope)

    timings = [
        _bench(
            "operation stdlib json.dumps + model_validate_json",
            lambda: OperationRecord.model_validate_json(json.dumps(operation_json)),
            loops=loops,
            repeats=repeats,
        ),
        _bench(
            "operation stdlib compact json.dumps + model_validate_json",
            lambda: OperationRecord.model_validate_json(
                json.dumps(operation_json, separators=(",", ":"))
            ),
            loops=loops,
            repeats=repeats,
        ),
        _bench(
            "operation orjson.dumps + model_validate_json",
            lambda: OperationRecord.model_validate_json(orjson.dumps(operation_json)),
            loops=loops,
            repeats=repeats,
        ),
        _bench(
            "operation guarded orjson + model_validate_json",
            lambda: (
                OperationRecord.model_validate_json(orjson.dumps(operation_json))
                if _build_orjson_guard(operation_json)
                else OperationRecord.model_validate_json(json.dumps(operation_json))
            ),
            loops=loops,
            repeats=repeats,
        ),
        _bench(
            "operation model_validate(python-mode model_dump)",
            lambda: OperationRecord.model_validate(operation.model_dump(mode="python")),
            loops=loops,
            repeats=repeats,
        ),
        _bench(
            "operation model_dump_json", operation.model_dump_json, loops=loops, repeats=repeats
        ),
        _bench(
            "operation model_validate_json(model_dump_json)",
            lambda: OperationRecord.model_validate_json(operation.model_dump_json()),
            loops=loops,
            repeats=repeats,
        ),
        _bench(
            "event canonical_payload orjson sorted",
            lambda: canonical_payload(event.payload),
            loops=loops,
            repeats=repeats,
        ),
        _bench(
            "event stdlib sorted compact UTF8 allow_nan_false",
            lambda: json.dumps(event.payload, **std_canonical_options),
            loops=loops,
            repeats=repeats,
        ),
        _bench(
            "envelope pydantic model_dump_json",
            envelope.model_dump_json,
            loops=loops,
            repeats=repeats,
        ),
        _bench(
            "envelope orjson of model_dump(mode=json)",
            lambda: orjson.dumps(envelope.model_dump(mode="json")),
            loops=loops,
            repeats=repeats,
        ),
        _bench(
            "envelope stdlib compact UTF8 of model_dump(mode=json)",
            lambda: json.dumps(
                envelope.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
            ),
            loops=loops,
            repeats=repeats,
        ),
    ]
    return {
        "runtime": {
            "python": sys.version.split()[0],
            "implementation": sys.implementation.name,
            "packages": _package_versions(),
        },
        "benchmark": {
            "loops_per_repeat": loops,
            "repeats": repeats,
            "warmup": "one call per variant outside timed samples",
            "clock": "time.perf_counter; medians reported",
        },
        "payloads": {
            "operation_record": {
                "dump_bytes_stdlib": len(std_operation),
                "dump_bytes_orjson": len(orjson_operation),
                "dump_bytes_model_dump_json": len(operation.model_dump_json().encode()),
                "nested_items": len(operation_json["checkpoint"]["details"]["items"]),
            },
            "event": {
                "kind": event.kind.value,
                "canonical_payload_bytes": len(canonical_event),
                "payload_keys": len(payload),
            },
            "envelope": {
                "record_id": envelope.record_id,
                "pydantic_json_bytes": len(envelope_pydantic_json),
                "orjson_bytes": len(envelope_orjson),
                "stdlib_json_bytes": len(envelope_stdlib),
                "record_roundtrip_equal": envelope_roundtrip.record_id == record.record_id
                and envelope_roundtrip.content == record.content,
            },
        },
        "correctness": {
            "operation_jsonmode_stdlib_orjson_pythonmode_equal_on_json_tree": semantically_equal,
            "operation_model_validate_on_jsonmode_db_mapping": _attempt(
                lambda: OperationRecord.model_validate(operation_json)
            ),
            "event_orjson_and_stdlib_utf8_canonical_bytes_equal": canonical_event == std_event,
            "event_stdlib_utf8_fingerprint_equals_orjson": xxhash.xxh64_hexdigest(std_event)
            == fingerprint_payload(event.payload),
            "event_orjson_bytes_prefix": canonical_event[:90].decode(
                "utf-8", errors="backslashreplace"
            ),
            "event_stdlib_bytes_prefix": std_event[:90].decode("utf-8", errors="backslashreplace"),
            "envelope_encodings_semantically_equal": json.loads(envelope_pydantic_json)
            == json.loads(envelope_orjson)
            == json.loads(envelope_stdlib),
            "event_stdlib_ascii_sha256_equals_orjson_payload_sha256": hashlib.sha256(
                json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            == hashlib.sha256(canonical_event).hexdigest(),
        },
        "operation_timings": timings,
        "provider_payload_edge_probes": _probe_values(operation),
        "event_canonical_byte_edge_probes": _event_byte_probes(),
        "interpretation": [
            "Timing results apply only to these schemas, payload sizes, Python/runtime versions, and benchmark process.",
            "Canonical event bytes and fingerprints are identity/replay inputs; byte differences prohibit a drop-in encoder change without a versioned contract.",
            "Provider change checkpoints permit arbitrary nested details, so encoder alternatives must preserve invalid and unknown values as well as normal JSON trees.",
            "Direct model_validate is Python-mode validation and is not equivalent to existing JSON-mode validation unless separately proven for the full contract.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--loops", type=int, default=2000)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    result = run(loops=args.loops, repeats=args.repeats)
    text = json.dumps(result, indent=2, ensure_ascii=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
