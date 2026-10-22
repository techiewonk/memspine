"""RM-99 (+RM-86/RM-87): compiled edges carry sensitivity labels from the labelling layer.

Model-free: deterministic labels only. The opt-in GLiNER binding is checked at config level.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema.root import MemspineConfig
from memspine.derive.pipeline.stages.edge_compiler_v2 import EdgeCompiler, compile_graph_edges
from memspine.derive.sensitivity import LABELLER_ID, LabelHit, SensitivityLabeller
from memspine.exceptions import ConfigError
from memspine.services.graph.qualifiers import QualifierFilter, edge_qualifiers
from memspine.store.records import MemoryRecord, SourceInfo

NS = "s"
T0 = datetime(2024, 3, 1, 9, 0, tzinfo=UTC)


async def _engine() -> Engine:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"provider": "sqlite", "settings": {"path": ":memory:", "busy_timeout_s": 5.0}},
        embedding={"provider": "hash", "settings": {}},
        memories={
            "semantic": {"enabled": True},
            "episodic": {"enabled": True},
            "associative": {"enabled": True, "policies": {"entity_nodes": True}},
        },
        graph={"edge_compiler_v2": True},
    )
    await eng.start()
    return eng


FACTS = {
    # key: (subject, rel, dst, content)
    "home": ("Ana", "resides_in", "Porto", "Ana resides in Porto"),
    "jazz": ("Ana", "likes", "Jazz", "Ana likes Jazz"),
    "phone": ("Ana", "shared_number", "Bruno", "Ana gave Bruno her number +44 20 7946 0958"),
    "doctor": ("Ana", "visited", "Clinic", "Ana visited the clinic; she was diagnosed with asthma"),
}


async def _build(eng: Engine) -> dict[str, str]:
    turn = await eng.write("Ana talked about her week", namespace=NS, memory_type="episodic")
    ids: dict[str, str] = {}
    for key, (src, rel, dst, content) in FACTS.items():
        rec = await eng.write(
            content,
            namespace=NS,
            memory_type="semantic",
            entity=src,
            tags=["kind:event", f"rel:{rel}", f"dst:{dst}"],
            source=SourceInfo(role="derived", channel="extract_graph", parents=[turn.record_id]),
            valid_from=T0,
        )
        ids[key] = rec.record_id
    stats = await compile_graph_edges(eng._pipeline_ctx())
    assert stats["status"] == "ok"
    return ids


async def _labels_by_record(eng: Engine) -> dict[str, list[Any]]:
    assert eng._graph is not None
    out: dict[str, list[Any]] = {}
    for edge in await eng._graph.edge_list(NS):
        if edge.properties.get("compiled") != "edge_compiler_v2" or edge.weight <= 0:
            continue
        q = edge_qualifiers(edge.properties)
        assert q is not None
        out.setdefault(str(edge.properties["record_id"]), []).append(q)
    return out


async def test_compiled_edges_carry_sensitivity_labels() -> None:
    eng = await _engine()
    try:
        ids = await _build(eng)
        by_record = await _labels_by_record(eng)
        again = await compile_graph_edges(eng._pipeline_ctx())
    finally:
        await eng.stop()
    expected = {
        "home": {"location"},
        "jazz": set(),  # checked_none
        "phone": {"contact"},
        "doctor": {"health"},
    }
    for key, labels in expected.items():
        quals = by_record[ids[key]]
        assert quals, key
        for q in quals:
            assert q.sensitivity == labels, key
            assert q.sensitivity_checked is True
            assert q.sensitivity_labeller == LABELLER_ID
            assert q.sensitivity_labeller_version == SensitivityLabeller().version
    assert by_record[ids["home"]][0].sensitivity_confidence == {"location": 1.0}
    assert by_record[ids["jazz"]][0].sensitivity_confidence is None
    # re-projection (the backfill path) is idempotent: same labels, no new LINK events
    assert "written" not in again and again["unchanged"] > 0


async def test_exclusion_filter_on_compiled_edges() -> None:
    eng = await _engine()
    try:
        ids = await _build(eng)
        assert eng._graph is not None
        edges = [
            e
            for e in await eng._graph.edge_list(NS)
            if e.properties.get("compiled") == "edge_compiler_v2" and e.weight > 0
        ]
    finally:
        await eng.stop()
    flt = QualifierFilter(exclude_sensitivities=frozenset({"health", "contact"}))
    kept = {str(e.properties["record_id"]) for e in edges if flt.admits(e)}
    assert kept == {ids["home"], ids["jazz"]}  # labelled matches dropped, checked_none kept
    allow = QualifierFilter(sensitivities=frozenset({"location", "health"}))
    assert {str(e.properties["record_id"]) for e in edges if allow.admits(e)} == {
        ids["home"],
        ids["doctor"],
    }


def _record(rel: str, content: str, **kw: Any) -> MemoryRecord:
    return MemoryRecord(
        namespace=NS,
        memory_type="semantic",
        content=content,
        entity="Ana",
        tags=["kind:event", f"rel:{rel}", "dst:Porto"],
        source=SourceInfo(role="derived", channel="extract_graph", parents=["p1"]),
        valid_from=T0,
        **kw,
    )


def test_claim_of_uses_predicate_key_and_values() -> None:
    claim = EdgeCompiler.claim_of(
        _record("has_attribute", "Ana: email", attribute="contact_channel")
    )
    assert claim.predicate == "has_attribute" and claim.attribute_key == "contact_channel"
    assert claim.values == ("Ana: email", "Porto")


def test_precomputed_labels_override_the_deterministic_pass() -> None:
    compiler = EdgeCompiler()
    rec = _record("likes", "Ana likes Porto")
    custom = SensitivityLabeller().label(compiler.claim_of(_record("resides_in", "x")))
    compiled = compiler.compile([rec], NS, sensitivity={rec.record_id: custom})
    assert compiled.meta[rec.record_id]["sensitivity"] is custom
    plain = compiler.compile([rec], NS)
    assert plain.meta[rec.record_id]["sensitivity"].state == "checked_none"


# ------------------------------------------------------------------ opt-in GLiNER binding


def _gliner_config(local_files_only: bool) -> dict[str, Any]:
    return {
        "runtime": {
            "provider_services": {
                "health_timeout_s": 5.0,
                "close_timeout_s": 10.0,
                "startup_timeout_s": None,
            }
        },
        "models": {
            "profiles": {
                "pii": {
                    "kind": "gliner",
                    "provider": "gliner2",
                    "settings": {"tasks": ["entities"], "local_files_only": local_files_only},
                }
            },
            "bindings": {"gliner_sensitivity": "pii"},
        },
    }


def test_gliner_sensitivity_binding_is_offline_only() -> None:
    MemspineConfig.model_validate(_gliner_config(True))
    with pytest.raises((ConfigError, ValueError), match="offline only"):
        MemspineConfig.model_validate(_gliner_config(False))


class _FakeModel:
    detector_id = "fake-pii@1"

    async def detect(self, text: str) -> list[LabelHit]:
        return [LabelHit("identity", 0.9, "model:fake")] if "Jazz" in text else []


async def test_bound_model_runs_in_compile_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = await _engine()
    monkeypatch.setattr(eng, "_sensitivity_model", lambda: _FakeModel())
    try:
        ids = await _build(eng)
        by_record = await _labels_by_record(eng)
    finally:
        await eng.stop()
    jazz = by_record[ids["jazz"]][0]
    assert jazz.sensitivity == {"identity"} and jazz.sensitivity_checked is True
    assert jazz.sensitivity_labeller_version is not None
    assert jazz.sensitivity_labeller_version.endswith("+fake-pii@1")
    assert by_record[ids["home"]][0].sensitivity == {"location"}


async def test_unbound_engine_has_no_sensitivity_model() -> None:
    eng = await _engine()
    try:
        assert eng._pipeline_ctx().sensitivity_model is None
    finally:
        await eng.stop()
