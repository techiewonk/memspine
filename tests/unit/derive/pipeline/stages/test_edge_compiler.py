"""``memspine.derive.pipeline.stages.edge_compiler``: the pure public helpers.

The compile/project/retract wiring lives in ``tests/unit/wiring/test_f_02c_edge_compiler.py`` and
``tests/unit/wiring/test_rm89_rm100_edge_qualifiers.py``."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from memspine.derive.edges import GraphDelta, GraphEdge
from memspine.derive.pipeline.stages.edge_compiler import (
    CompiledEdges,
    ProjectionStats,
    attach_lineage,
    edge_qualifiers_for,
    source_origin_for,
)
from memspine.services.graph.qualifiers import ClaimState, ErasureState, Polarity

T1 = datetime(2026, 1, 1, tzinfo=UTC)
T2 = datetime(2026, 6, 1, tzinfo=UTC)


def _edge(**over: Any) -> GraphEdge:
    base: dict[str, Any] = {
        "key": "e1",
        "kind": "projection",
        "label": "lives_in",
        "src": "ent:alice",
        "dst": "ent:leiden",
        "assertion_id": "a1",
        "record_id": "r1",
        "occurrence_id": "o1",
        "tenant": "t",
        "owner": "t",
    }
    base.update(over)
    return GraphEdge(**base)


@pytest.mark.parametrize(
    ("cls", "channel", "origin"),
    [
        ("user_stated", "message", "raw_message"),
        ("document", "ingest", "document"),
        ("document", "rest", "document"),  # the class alone names a document
        ("derived", "reflection", "reflection"),  # the channel wins over the class
        ("anything", "agent_tool", "tool_output"),
        ("derived", "extraction", None),  # unknown stays unknown
        ("user_stated", "rest", None),
    ],
)
def test_source_origin_for(cls: str, channel: str, origin: str | None) -> None:
    assert source_origin_for(cls, channel) == origin


def test_attach_lineage_adds_only_resolved_roots() -> None:
    compiled = CompiledEdges(
        namespace="n",
        delta=GraphDelta(),
        meta={
            "r1": {"evidence_ids": ["e2", "e1", "e3"]},
            "r2": {"evidence_ids": ["e9"]},
        },
    )
    out = attach_lineage(compiled, {"e1": ["x", "a"], "e2": ["b"], "e3": []})
    assert out.meta["r1"]["evidence_roots"] == {"e1": ["a", "x"], "e2": ["b"]}
    assert "evidence_roots" not in out.meta["r2"]  # nothing resolved: unknown support
    assert "evidence_roots" not in compiled.meta["r1"]  # the input is not mutated


def test_projection_stats_report_only_nonzero_counts() -> None:
    assert ProjectionStats(written=2, contested=1).as_dict() == {"written": 2, "contested": 1}
    assert ProjectionStats().as_dict() == {}


def test_qualifiers_of_a_live_edge() -> None:
    meta = {
        "trust": 1.7,
        "evidence_ids": ["e1"],
        "source_message_id": "m1",
        "origin": "raw_message",
        "binding": "bound",
        "namespace": "t/alice",
        "known_at": T1,
    }
    q = edge_qualifiers_for(_edge(valid_from=T1), meta)
    assert q.claim_state is ClaimState.CURRENT
    assert q.erasure_state is ErasureState.ACTIVE
    assert q.polarity is Polarity.POSITIVE
    assert q.valid_from == T1 and q.valid_to is None and q.valid_open_ended is True
    assert q.trust == 1.0  # clamped
    assert q.claim_id == "r1" and q.evidence_ids == ("e1",)
    assert q.provenance is not None and q.provenance.source_id == "m1"
    assert q.binding_status == "bound" and q.scope == "t/alice" and q.known_at == T1
    assert q.event_role is None


def test_unknown_values_stay_unknown() -> None:
    naive = datetime(2026, 1, 1)
    q = edge_qualifiers_for(
        _edge(valid_from=naive, status="draft"),
        {"trust": True, "binding": "maybe", "known_at": naive, "event_precision": "fortnight"},
    )
    assert q.valid_from is None and q.known_at is None  # a naive time is never guessed
    assert q.trust is None and q.binding_status is None and q.valid_precision is None
    assert q.claim_state is None and q.provenance is None and q.evidence_ids is None


def test_incoherent_bounds_keep_only_the_start() -> None:
    q = edge_qualifiers_for(_edge(valid_from=T2, valid_to=T1), {})
    assert q.valid_from == T2 and q.valid_to is None


@pytest.mark.parametrize(
    ("edge", "meta", "state"),
    [
        ({"closed": True, "valid_from": T1, "valid_to": T2}, {}, ClaimState.SUPERSEDED),
        ({"contested": True}, {}, ClaimState.CONTESTED),
        ({}, {"evidence_erased": True}, ClaimState.CONTESTED),
    ],
)
def test_claim_state(edge: dict[str, Any], meta: dict[str, Any], state: ClaimState) -> None:
    q = edge_qualifiers_for(_edge(**edge), meta)
    assert q.claim_state is state
    if edge.get("closed"):
        assert q.valid_to == T2 and q.valid_open_ended is None


def test_negated_role_edge() -> None:
    q = edge_qualifiers_for(_edge(kind="role", label="subject", negated=True), {})
    assert q.polarity is Polarity.NEGATIVE and q.event_role == "subject"
