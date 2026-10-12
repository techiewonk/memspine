from __future__ import annotations

from typing import Any

from memspine.read.router_rules import (
    LayerState,
    QueryDesc,
    ServiceState,
    route,
)


def _state(enabled: tuple[str, ...] = ("L0",), counts: Any = None, **svc: bool) -> LayerState:
    return LayerState(
        enabled=frozenset(enabled),  # type: ignore[arg-type]
        counts=counts if counts is not None else {"L0": 100},
        services=ServiceState(**svc),
    )


def test_raw_only_store_routes_vector_lexical_temporal() -> None:
    p = route(QueryDesc(), _state(), "standard")
    assert p.leg_names() == ("vector", "lexical", "temporal")
    assert p.families() == {"vector": "dense", "lexical": "lexical", "temporal": "structured"}
    for n in ("graph", "episode", "profile", "entity"):
        assert p.skipped_reason(n) == "not_routed"


def test_join_without_l3_skips_graph_with_reason() -> None:
    q = QueryDesc(shape="join", named_subjects=("Bram",))
    p = route(q, _state(graph_store=True), "standard")
    assert "entity" in p.leg_names() and "graph" not in p.leg_names()
    assert p.skipped_reason("graph") == "layer_disabled"


def test_l3_populated_adds_graph_for_join() -> None:
    st = _state(("L0", "L3"), {"L0": 50, "L3": 10}, graph_store=True)
    p = route(QueryDesc(shape="join"), st, "standard")
    assert {"entity", "graph"} <= set(p.leg_names())
    assert p.families()["graph"] == "derived"


def test_empty_layer_and_missing_service_skipped() -> None:
    q = QueryDesc(shape="join")
    p = route(q, _state(("L0", "L3"), {"L0": 5, "L3": 0}, graph_store=True), "standard")
    assert p.skipped_reason("graph") == "layer_empty"
    p2 = route(q, _state(("L0", "L3"), {"L0": 5, "L3": 3}, graph_store=False), "standard")
    assert p2.skipped_reason("graph") == "service_missing"
    assert "graph_store" in p2.missing_services
    p3 = route(QueryDesc(), _state(embedder=False), "fast")
    assert p3.skipped_reason("vector") == "service_missing"
    assert "vector" not in p3.leg_names()


def test_l0_empty_skips_core_legs() -> None:
    p = route(QueryDesc(), _state(counts={}), "standard")
    assert p.legs == ()
    assert p.skipped_reason("vector") == "layer_empty"
    assert any(t.rule == "no_core_leg" for t in p.trace)


def test_temporal_and_as_of_views() -> None:
    p = route(QueryDesc(shape="temporal", has_time_scope=True), _state(), "standard")
    plain = route(QueryDesc(), _state(), "standard")
    tl = {leg.name: leg for leg in p.legs}["temporal"]
    tp = {leg.name: leg for leg in plain.legs}["temporal"]
    assert tl.fetch_k > tp.fetch_k
    assert p.time_view == "current"
    assert route(QueryDesc(shape="as_of"), _state(), "fast").time_view == "as_of"


def test_list_count_widen_and_exhaustive() -> None:
    base = route(QueryDesc(), _state(), "standard")
    p = route(QueryDesc(shape="count", many_cardinality=True), _state(), "standard")
    assert p.exhaustive and p.list_mode
    assert p.legs[0].fetch_k > base.legs[0].fetch_k
    assert p.pool_size <= 64
    assert not base.exhaustive


def test_profile_leg_rules() -> None:
    st = _state(("L0", "L5"), {"L0": 9, "L5": 4})
    assert "profile" in route(QueryDesc(personal=True), st).leg_names()
    g = route(QueryDesc(shape="generic", personal=True), st)
    assert "profile" not in g.leg_names()
    assert g.skipped_reason("profile") == "generic_query"
    assert route(QueryDesc(personal=True), _state()).skipped_reason("profile") == "layer_disabled"


def test_episode_needs_l2() -> None:
    q = QueryDesc(episodic_cue=True)
    assert route(q, _state()).skipped_reason("episode") == "layer_disabled"
    st = _state(("L0", "L2"), {"L0": 3, "L2": 2})
    assert "episode" in route(q, st).leg_names()


def test_rerank_always_when_installed_and_missing_recorded() -> None:
    for tier in ("fast", "standard", "deep"):
        assert route(QueryDesc(), _state(), tier).rerank.enabled  # type: ignore[arg-type]
    p = route(QueryDesc(), _state(reranker=False), "standard")
    assert not p.rerank.enabled and "reranker" in p.missing_services
    assert any(t.rule == "rerank.missing" for t in p.trace)


def test_deep_only_allows_rewrite() -> None:
    st = _state(worker_model=True)
    assert not route(QueryDesc(), st, "fast").rewrite.rewrite_allowed
    assert not route(QueryDesc(), st, "standard").rewrite.rewrite_allowed
    d = route(QueryDesc(), st, "deep")
    assert d.rewrite.rewrite_allowed and d.rewrite.sufficiency_check_allowed
    nd = route(QueryDesc(), _state(), "deep")
    assert not nd.rewrite.rewrite_allowed and "worker_model" in nd.missing_services


def test_lexical_language_carried() -> None:
    p = route(QueryDesc(language="ja"), _state())
    assert {leg.name: leg for leg in p.legs}["lexical"].language == "ja"


def test_deterministic_and_traced() -> None:
    q = QueryDesc(shape="join", named_subjects=("a", "b"), personal=True, language="de")
    st = _state(
        ("L0", "L3", "L5"), {"L0": 9, "L3": 2, "L5": 1}, graph_store=True, worker_model=True
    )
    a, b = route(q, st, "deep"), route(q, st, "deep")
    assert a == b and a.model_dump_json() == b.model_dump_json()
    assert a.trace and a.trace[0].rule == "tier"
    assert a.leg_names() == ("vector", "lexical", "temporal", "entity", "graph", "profile")
