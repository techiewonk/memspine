"""T5-06: the ops ledger, ``Engine.ops_report``, the OTel exporter and the no-content span guard.

Model-free: hash embedder, no LLM. Invented names only (Ilse, Bram, Noor, Tomasz).

OTel: the exporter is driven through a fake tracer (always runs, covers the span shape and the
guard); the SDK in-memory exporter test skips only when the optional SDK is absent. Broken
installed SDKs, transitive dependencies and exporter imports fail normally.
"""

from __future__ import annotations

import importlib
import sys
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.config.schema.observability import OtelConfig
from memspine.economy import ledger as costs
from memspine.exceptions import MissingServiceError
from memspine.observability import bindings, tap
from memspine.observability.stage_ledger import StageEntry
from memspine.ops import ledger as ops
from memspine.ops.otel import (
    OtelExporter,
    SpanContentError,
    scrub_span_attributes,
    validate_span_attributes,
)

NAMES = ("Ilse", "Bram", "Noor", "Tomasz")


@pytest.fixture(autouse=True)
def _reset() -> Any:
    tap.shutdown()
    yield
    tap.shutdown()
    assert ops.installed() is None  # every engine uninstalls its ledger on stop


class FakeSpan:
    def __init__(self, name: str) -> None:
        self.name = name
        self.attributes: dict[str, Any] = {}
        self.ended = False

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def end(self, end_time: int | None = None) -> None:
        self.ended = True


class FakeTracer:
    def __init__(self) -> None:
        self.spans: list[FakeSpan] = []

    def start_span(self, name: str, context: Any = None, start_time: Any = None) -> FakeSpan:
        span = FakeSpan(name)
        self.spans.append(span)
        return span


def _engine(**extra: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        **extra,
    )


async def _workload(eng: Engine) -> None:
    await eng.write("Ilse lives in Tomasz Street with Bram", namespace="noor", entity="Ilse")
    await eng.ingest_messages(
        [
            {"role": "user", "content": "Bram: Noor moved to Tomasz Street last spring"},
            {"role": "assistant", "content": "Ilse noted that Noor likes green tea"},
        ],
        namespace="noor",
        session_id="s1",
    )
    await eng.sleep()
    await eng.search("Where does Ilse live?", namespace="noor")
    await eng.retrieve(namespace="noor")
    await eng.read("What does Noor drink?", namespace="noor")


# ----------------------------------------------------------------------------- flag off
def test_defaults_are_off() -> None:
    cfg = MemspineConfig(
        runtime={
            "provider_services": {
                "health_timeout_s": 1.0,
                "close_timeout_s": 1.0,
                "startup_timeout_s": None,
            }
        }
    ).observability
    assert cfg.ops_ledger is False
    assert cfg.otel == OtelConfig(enabled=False)


async def test_flag_off_installs_nothing_and_results_are_unchanged() -> None:
    results: list[list[str]] = []
    for flags in ({}, {"observability": {"ops_ledger": True}}):
        eng = _engine(**flags)
        await eng.start()
        try:
            with bindings.bound(eng._binding):  # what this Engine's calls see
                assert (ops.installed() is not None) == bool(flags)
                assert not tap.enabled()
            await eng.write("Ilse lives in Brussels", namespace="noor", entity="Ilse")
            hits = await eng.search("Ilse", namespace="noor")
            results.append([h[0].content for h in hits])
            report = eng.ops_report()
            assert report["enabled"] is bool(flags)
            if not flags:
                assert all(not rows for rows in report["stages"].values())
                assert report["otel"] is None and report["slo"] is None and report["cost"] is None
        finally:
            await eng.stop()
    assert results[0] == results[1] and results[0]


# ----------------------------------------------------------------------------- ops_report
async def test_ops_report_covers_read_write_and_sleep_stages() -> None:
    eng = _engine(observability={"ops_ledger": True})
    await eng.start()
    try:
        await _workload(eng)
        report = eng.ops_report()
    finally:
        await eng.stop()
    assert report["enabled"] is True
    stages = report["stages"]
    assert {"write", "ingest_messages"} <= set(stages["write"])
    assert {"search", "retrieve", "read"} <= set(stages["read"])
    assert {"consolidate", "extract_graph"} <= set(stages["sleep"])
    for kind in ("read", "write", "sleep"):
        for row in stages[kind].values():
            assert row["runs"] >= 1 and row["errors"] == 0
            assert row["p50_ms"] is not None and row["p95_ms"] >= row["p50_ms"]
            assert {"items", "dropped", "deferred", "model_calls", "tokens_in"} <= set(row)
    assert report["totals"]["runs"] == sum(
        r["runs"] for rows in stages.values() for r in rows.values()
    )
    # a read verb that calls another read verb is one run, not two
    assert stages["read"]["read"]["runs"] == 1


async def test_a_failing_verb_counts_an_error() -> None:
    eng = _engine(observability={"ops_ledger": True})
    await eng.start()
    try:
        with pytest.raises(Exception):  # noqa: B017 - any engine error
            await eng.write("", namespace="")
        row = eng.ops_report()["stages"]["write"]["write"]
    finally:
        await eng.stop()
    assert row["runs"] == 1 and row["errors"] == 1


def test_ops_report_aggregates_all_three_ledgers() -> None:
    from memspine.economy.slo import SloLedger

    ledger = ops.OpsLedger()
    with ledger.stage("sleep", "consolidate") as handle:
        handle.add(dropped=2, deferred=1, items=5)
    with ledger.stage("read", "search"):
        pass
    cost = costs.WriteCostLedger()
    cost.record(
        "generative",
        model="stub",
        calls=2,
        tokens_in=30,
        tokens_out=10,
        latency_ms=4.0,
        scope=costs.CostScope(namespace=costs.SLEEP_POOL, layer="consolidate", stage="consolidate"),
    )
    cost.record(
        "embedding",
        calls=3,
        scope=costs.CostScope(namespace="noor", layer="L2", stage="window_extract"),
    )
    cost.record(
        "generative",
        outcome="error",
        scope=costs.CostScope(namespace="noor", layer="L2", stage="window_extract"),
    )
    slo = SloLedger()
    slo.record_read(0.010)
    slo.record_read(0.030)
    slo.record_lag(1.5)
    slo.set_backlog(4, 12.0)
    report = ops.ops_report(ledger, slo=slo.report(), cost=cost)

    consolidate = report["stages"]["sleep"]["consolidate"]  # ops + cost merged into one row
    assert (consolidate["runs"], consolidate["dropped"], consolidate["deferred"]) == (1, 2, 1)
    assert consolidate["model_calls"] == 2 and consolidate["tokens_in"] == 30
    window = report["stages"]["write"]["window_extract"]  # seen by the cost ledger only
    assert window["embed_calls"] == 3 and window["errors"] == 1 and window["runs"] == 0
    assert report["stages"]["read"]["search"]["runs"] == 1
    assert report["slo"]["reads"] == 2 and report["slo"]["backlog_items"] == 4
    assert report["cost"]["model_calls"] == 3
    assert report["totals"]["model_calls"] == 3 and report["totals"]["deferred"] == 1
    only_noor = ops.ops_report(ledger, cost=cost, namespace="noor")
    assert "window_extract" in only_noor["stages"]["write"]
    # engine-wide sleep stages stay in a namespace view
    assert only_noor["stages"]["sleep"]["consolidate"]["model_calls"] == 2


async def test_ops_report_merges_the_engine_cost_ledger() -> None:
    eng = _engine(observability={"ops_ledger": True}, economy={"ledger_v2": True})
    await eng.start()
    try:
        await eng.write("Ilse lives in Brussels", namespace="noor", entity="Ilse")
        await eng.sleep()
        report = eng.ops_report()
        assert report["cost"] is not None and report["cost"] == eng.cost_report()
    finally:
        await eng.stop()


# ----------------------------------------------------------------------------- the guard
def test_validator_accepts_ids_hashes_counts_codes_and_durations() -> None:
    ok = {
        "memspine.stage": "read.gate.dropped",
        "memspine.scope_hash": "0123456789abcdef",
        "memspine.request_id": "a1b2c3d4e5f60718",
        "memspine.events": 3,
        "memspine.duration_ms": 1.25,
        "memspine.error": "ValueError",
        "memspine.model_id": "Qwen3-8B",
        "memspine.n.dropped": 2,
        "memspine.failed": True,
    }
    assert validate_span_attributes(ok) == ok


@pytest.mark.parametrize(
    "attrs",
    [
        {"memspine.stage": "Ilse lives in Tomasz Street"},  # a sentence
        {"memspine.stage": "Ilse"},  # a bare name is not a lowercase code
        {"memspine.scope_hash": "Noor"},
        {"memspine.stage": "x" * 200},
        {"memspine.stage": "has\nnewline"},
        {"memspine.error": "Bram went home"},
        {"stage": "read"},  # not a memspine.* key
        {"memspine.stage": ["read"]},  # not a scalar
        {"memspine.duration_ms": float("nan")},
        {"memspine.Bad Key": 1},
    ],
)
def test_validator_rejects_free_text(attrs: dict[str, Any]) -> None:
    with pytest.raises(SpanContentError):
        validate_span_attributes(attrs)
    clean, rejected = scrub_span_attributes(attrs)
    assert not clean and rejected == list(attrs)


def test_exporter_scrubs_a_bad_attribute_and_counts_it() -> None:
    tracer = FakeTracer()
    exporter = OtelExporter(tracer=tracer)
    trace = {
        "id": "a1b2c3d4e5f60718",
        "kind": "read",
        "scope_hash": "0123456789abcdef",
        "started": "2026-10-13T10:00:00.000+00:00",
        "duration_ms": 2.0,
        "fields": {"op": "Ilse likes tea", "stage": "gate"},
        "events": [
            {"seq": 0, "t_ms": 0.1, "stage": "read.gate.start", "fields": {"n": 4}},
            {"seq": 1, "t_ms": 0.9, "stage": "read.gate.end", "fields": {"duration_ms": 0.8}},
        ],
        "children": [],
    }
    exporter.write(trace)
    root, stage = tracer.spans
    assert root.name == "memspine.read" and "memspine.op" not in root.attributes
    assert root.attributes["memspine.stage"] == "gate"
    assert stage.attributes == {
        "memspine.stage": "read.gate",
        "memspine.events": 2,
        "memspine.n.n": 4,
        "memspine.duration_ms": 0.8,
    }
    assert exporter.stats() == {"spans": 2, "rejected_attributes": 1}
    assert all(s.ended for s in tracer.spans)


def test_exporter_exports_ledger_entries() -> None:
    tracer = FakeTracer()
    exporter = OtelExporter(tracer=tracer)
    entry = StageEntry(
        stage="sleep.consolidate",
        scope_hash="0123456789abcdef",
        request_or_job_id="call-1",
        cost_class="generative",
        model_id="stub-1",
        calls=2,
        duration_ms=3.5,
    )
    exporter.export_entries([entry])
    attrs = tracer.spans[0].attributes
    assert attrs["memspine.stage"] == "sleep.consolidate" and attrs["memspine.calls"] == 2
    assert exporter.stats()["rejected_attributes"] == 0


# ----------------------------------------------------------------------------- otel wiring
def _blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    monkeypatch.delitem(sys.modules, "opentelemetry.trace", raising=False)


def test_missing_opentelemetry_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    _blocked(monkeypatch)
    with pytest.raises(MissingServiceError) as err:
        OtelExporter()
    assert err.value.extra == "otel" and "memspine[otel]" in str(err.value)


async def test_engine_start_with_otel_enabled_but_missing_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _blocked(monkeypatch)
    eng = _engine(observability={"otel": {"enabled": True}})
    with pytest.raises(MissingServiceError, match=r"memspine\[otel\]"):
        await eng.start()
    assert ops.installed() is None and not tap.enabled()


def _exporter_with(tracer: FakeTracer, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("memspine.api.engine.OtelExporter", lambda: OtelExporter(tracer=tracer))


async def test_no_content_in_any_span_over_a_full_workload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracer = FakeTracer()
    _exporter_with(tracer, monkeypatch)
    # the worst case for the guard: the tap keeps content too (its own sink is irrelevant here)
    eng = _engine(
        observability={
            "ops_ledger": True,
            "otel": {"enabled": True},
            "tap": {"enabled": True, "sink": "memory", "content": True},
        }
    )
    await eng.start()
    try:
        await _workload(eng)
        report = eng.ops_report()
        with bindings.bound(eng._binding):  # this Engine's tap
            held = str(tap.memory_traces())
    finally:
        await eng.stop()
    assert tracer.spans, "the workload emitted no spans"
    names = {s.name for s in tracer.spans}
    assert {"memspine.read", "memspine.write", "memspine.job", "memspine.stage"} <= names
    seen_kinds = {s.attributes.get("memspine.kind") for s in tracer.spans}
    assert {"read", "write", "job"} <= seen_kinds
    for span in tracer.spans:
        # the guard accepts what the exporter set, and nothing carries a name or a sentence
        validate_span_attributes(span.attributes)
        assert span.name.startswith("memspine.") and " " not in span.name
        for key, value in span.attributes.items():
            assert key.startswith("memspine.")
            if isinstance(value, str):
                assert not any(ch.isspace() for ch in value), (key, value)
                for name in NAMES:
                    assert name.lower() not in value.lower(), (key, value)
        assert not any(n.lower() in span.name.lower() for n in NAMES)
    assert report["otel"]["spans"] == len(tracer.spans)
    assert report["otel"]["rejected_attributes"] == 0
    # the tap itself held the invented names in this run, so a leak into spans would be caught
    assert any(n in held for n in NAMES)


def _require_otel_sdk() -> None:
    """Only an absent optional package is a skip; broken installations fail."""
    try:
        importlib.import_module("opentelemetry.sdk")
    except ModuleNotFoundError as exc:
        if exc.name not in {"opentelemetry", "opentelemetry.sdk"}:
            raise
        pytest.skip("opentelemetry-sdk is not installed")


@pytest.mark.parametrize("missing", ["opentelemetry", "opentelemetry.sdk"])
def test_sdk_guard_skips_only_absent_optional_package(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    def missing_sdk(name: str) -> None:
        assert name == "opentelemetry.sdk"
        raise ModuleNotFoundError(name=missing)

    monkeypatch.setattr(importlib, "import_module", missing_sdk)
    with pytest.raises(pytest.skip.Exception, match="opentelemetry-sdk is not installed"):
        _require_otel_sdk()


@pytest.mark.parametrize(
    "error",
    [
        ModuleNotFoundError(name="sdk_transitive_dependency"),
        ImportError("broken SDK"),
        RuntimeError("SDK initialization failed"),
    ],
)
def test_sdk_guard_does_not_hide_broken_installation(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def broken_sdk(name: str) -> None:
        assert name == "opentelemetry.sdk"
        raise error

    monkeypatch.setattr(importlib, "import_module", broken_sdk)
    with pytest.raises(type(error)) as caught:
        _require_otel_sdk()
    assert caught.value is error


async def test_sdk_in_memory_exporter_if_the_sdk_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    _require_otel_sdk()
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    monkeypatch.setattr(
        "memspine.api.engine.OtelExporter", lambda: OtelExporter(tracer_provider=provider)
    )
    eng = _engine(observability={"otel": {"enabled": True}})
    await eng.start()
    try:
        await _workload(eng)
    finally:
        await eng.stop()
    spans = memory.get_finished_spans()
    assert spans
    for span in spans:
        validate_span_attributes(dict(span.attributes or {}))
        for value in (span.attributes or {}).values():
            if isinstance(value, str):
                assert not any(n.lower() in value.lower() for n in NAMES)


# ----------------------------------------------------------------------------- REST route
async def test_rest_ops_report_route() -> None:
    pytest.importorskip("fastapi")
    import httpx

    from memspine.api.rest import create_app

    eng = _engine(observability={"ops_ledger": True})
    await eng.start()
    try:
        await eng.write("Ilse lives in Brussels", namespace="noor", entity="Ilse")
        transport = httpx.ASGITransport(app=create_app(eng))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
            resp = await http.get("/ops/report")
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True and body["stages"]["write"]["write"]["runs"] == 1
        assert "Ilse" not in resp.text and "Brussels" not in resp.text
    finally:
        await eng.stop()
