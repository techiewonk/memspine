from __future__ import annotations

from datetime import UTC, datetime

import pytest

from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.erasure import CONTENT_KEYS, RetentionBoundary
from memspine.schemas.durable.lineage import LineageEdge
from memspine.schemas.public.plugins import CacheKey
from memspine.trust.erasure_plan import (
    ErasureState,
    InMemoryLineageGraph,
    apply_plan,
    assert_manifest_has_no_content,
    cache_key_current,
    manifest,
    plan_erasure,
    purge_steps,
    reapply_on_restore,
    retention_report,
    visible,
)

SCOPE = OwnerScope(tenant="t-zed", owner="u-quill")
NOW = datetime(2026, 10, 13, tzinfo=UTC)


def edge(child: str, parent: str, role: str = "support") -> LineageEdge:
    return LineageEdge(child=child, parent=parent, role=role)


def graph(*triples: tuple[str, str, str]) -> InMemoryLineageGraph:
    return InMemoryLineageGraph(edge(c, p, r) for c, p, r in triples)


def acts(plan) -> dict[str, str]:
    return {a.record_id: a.action for a in plan.actions}


def test_diamond_one_path_lost_keeps_join():
    g = graph(
        ("a", "x", "support"), ("b", "x", "support"), ("d", "a", "support"), ("d", "b", "support")
    )
    # erase a only: d still has b
    p = plan_erasure({"a"}, g, generation=1)
    assert acts(p) == {"d": "keep"}
    assert "d" not in p.hidden
    # erase x: a and b hidden, so d loses both
    p = plan_erasure({"x"}, g, generation=1)
    assert acts(p) == {"a": "hide_and_rederive", "b": "hide_and_rederive", "d": "hide_and_rederive"}
    d = next(a for a in p.actions if a.record_id == "d")
    assert d.lost_parents == ("a", "b")
    assert p.rederive_queue == ("a", "b", "d")


def test_multiple_paths_one_strongest_action():
    g = graph(
        ("s", "x", "support"), ("s", "y", "binding"), ("s", "z", "member"), ("s", "w", "support")
    )
    p = plan_erasure({"x", "y", "z"}, g, generation=2)
    assert len([a for a in p.actions if a.record_id == "s"]) == 1
    # live support w remains, member z lost with a live support: rerun beats unbind and keep
    assert acts(p) == {"s": "rerun_summary"}


def test_chain_of_summaries_cascades():
    g = graph(
        ("s1", "r1", "member"),
        ("s1", "r2", "member"),
        ("s2", "s1", "member"),
        ("s2", "r3", "member"),
    )
    p = plan_erasure({"r1"}, g, generation=1)
    assert acts(p) == {"s1": "rerun_summary", "s2": "rerun_summary"}
    s1 = next(a for a in p.actions if a.record_id == "s1")
    assert s1.remaining_sources == ("r2",)
    # losing every member leaves nothing to re-run from
    p = plan_erasure({"r1", "r2"}, g, generation=1)
    assert acts(p)["s1"] == "hide_and_rederive"
    assert acts(p)["s2"] == "rerun_summary"  # r3 remains


def test_binding_only_loss_unbinds_and_hides():
    g = graph(("c", "src", "support"), ("c", "ref", "binding"), ("d", "c", "support"))
    p = plan_erasure({"ref"}, g, generation=1)
    assert acts(p) == {"c": "unbind_and_hide", "d": "hide_and_rederive"}
    assert "c" not in p.rederive_queue
    assert "d" in p.rederive_queue


def test_partial_support_loss_keeps_and_drops_edge():
    g = graph(("c", "p1", "support"), ("c", "p2", "support"))
    p = plan_erasure({"p1"}, g, generation=1)
    assert acts(p) == {"c": "keep"}
    assert [(e.child, e.parent) for e in p.edges_to_remove] == [("c", "p1")]
    assert p.hidden == frozenset({"p1"})
    assert p.rederive_queue == ()


def test_non_dependency_roles_never_hide():
    g = graph(
        ("n", "o", "supersedes"),
        ("m", "o", "version_of"),
        ("k", "o", "cites"),
        ("q", "o", "context"),
    )
    p = plan_erasure({"o"}, g, generation=1)
    assert set(acts(p).values()) == {"keep"}
    assert len(p.edges_to_remove) == 4


def test_cycle_terminates_and_is_conservative():
    g = graph(("a", "x", "support"), ("a", "b", "support"), ("b", "a", "support"))
    p = plan_erasure({"x"}, g, generation=1)
    assert acts(p) == {"a": "hide_and_rederive", "b": "hide_and_rederive"}


def test_planner_is_pure_and_deterministic():
    g = graph(("a", "x", "support"), ("b", "a", "support"))
    before = g.edges()
    assert plan_erasure({"x"}, g, generation=1) == plan_erasure(["x"], g, generation=1)
    assert g.edges() == before


def test_repeated_erasure_is_idempotent():
    g = graph(
        ("a", "x", "support"), ("b", "a", "support"), ("c", "p", "support"), ("c", "x", "support")
    )
    s0 = ErasureState()
    p1 = plan_erasure({"x"}, g, generation=1)
    s1 = apply_plan(s0, p1)
    g.remove_edges(p1.edges_to_remove)
    # replay against the pruned graph, carrying prior lost ids
    p2 = plan_erasure({"x"}, g, generation=1, already_lost=s1.lost)
    assert p2.actions == ()
    assert apply_plan(s1, p2) == s1
    assert apply_plan(s1, p1) == s1
    # a later erasure sees earlier hidden ids as lost parents
    p3 = plan_erasure({"p"}, g, generation=2, already_lost=s1.lost)
    s3 = apply_plan(s1, p3)
    assert s3.generation == 2
    assert acts(p1)["c"] == "keep" and visible("c", s1)
    assert acts(p3) == {"c": "hide_and_rederive"}  # x edge was removed; p was its last support
    assert not any(visible(i, s3) for i in ("a", "b", "c", "x", "p"))


def test_erased_records_invisible_even_if_cache_returns_them():
    g = graph(("a", "x", "support"))
    s = apply_plan(ErasureState(), plan_erasure({"x"}, g, generation=3))
    assert [i for i in ["x", "a", "ok"] if visible(i, s)] == ["ok"]
    assert visible("a", s.release(["a"]))
    assert not visible("x", s.release(["x"]))
    key = CacheKey(
        scope=SCOPE, query="hello", config_hash="h", store_generation=1, erasure_generation=2
    )
    assert not cache_key_current(key, s)
    assert cache_key_current(key.model_copy(update={"erasure_generation": 3}), s)


def test_manifest_has_ids_and_generation_only():
    g = graph(("a", "x", "support"))
    p = plan_erasure({"x", "e1"}, g, generation=4)
    m = manifest(p, manifest_id="m-1", scope=SCOPE, erased_at=NOW, id_kinds={"e1": "event"})
    assert m.erasure_generation == 4
    assert m.ids.record_ids == ["x"] and m.ids.event_ids == ["e1"]
    dumped = m.model_dump()
    assert dumped["ids"]["record_ids"] == ["x"]
    assert "a" not in str(dumped["ids"])  # hidden dependants are recomputed, not listed

    def keys(n):
        if isinstance(n, dict):
            for k, v in n.items():
                yield str(k).lower()
                yield from keys(v)
        elif isinstance(n, list):
            for v in n:
                yield from keys(v)

    assert not (set(keys(dumped)) & CONTENT_KEYS)
    assert_manifest_has_no_content(m)
    with pytest.raises(ValueError):
        manifest(
            plan_erasure(set(), g, generation=1), manifest_id="m-2", scope=SCOPE, erased_at=NOW
        )


def test_purge_steps_track_progress_and_bump_cache_generation():
    g = graph(("a", "x", "support"), ("b", "y", "binding"))
    p = plan_erasure({"x", "y"}, g, generation=5)
    prog = purge_steps(p, ["projections", "vectors", "graph", "caches"])
    assert [s.store for s in prog.steps] == ["projections", "vectors", "graph", "caches"]
    cache = prog.steps[-1]
    assert cache.kind == "bump_generation" and cache.generation == 5 and cache.ids == ()
    assert prog.steps[0].ids == ("a", "b", "x", "y")
    assert (prog.done_count, prog.pending_count, prog.complete) == (0, 4, False)
    prog = prog.mark_done(prog.steps[0].step_id).mark_done(prog.steps[0].step_id)
    assert (prog.done_count, prog.pending_count, prog.fraction) == (1, 3, 0.25)
    for s in prog.pending:
        prog = prog.mark_done(s.step_id)
    assert prog.complete and prog.pending == ()
    with pytest.raises(KeyError):
        prog.mark_done("nope")
    batched = purge_steps(p, ["vectors"], batch_size=3)
    assert [len(s.ids) for s in batched.steps] == [3, 1]


def test_restore_reapplies_manifest():
    g = graph(("a", "x", "support"), ("b", "a", "support"), ("k", "p", "support"))
    p = plan_erasure({"x"}, g, generation=1)
    m = manifest(p, manifest_id="m-1", scope=SCOPE, erased_at=NOW)
    restored = {"x", "a", "b", "k", "p"}
    assert reapply_on_restore([m], restored) == {"x"}
    assert reapply_on_restore([m], restored, g) == {"x", "a", "b"}
    assert reapply_on_restore([m], {"k"}, g) == set()
    assert reapply_on_restore([], restored, g) == set()


def test_rolling_retention_flags_limited_rebuild():
    g = graph(("s", "old", "member"), ("s", "new", "member"), ("t", "new", "support"))
    rolling = RetentionBoundary(scope=SCOPE, mode="rolling", boundary_seq=10, rebuild="limited")
    rep = retention_report(rolling, g, ["s", "t", "old", "new"], origin_seq={"old": 5, "new": 20})
    assert rep.advertises_limited_rebuild
    assert rep.rebuildable == frozenset({"t", "new"})
    assert rep.limited == frozenset({"s", "old"})
    assert rep.lineage_kept and rep.tombstones_kept
    # unknown origin is treated as pruned
    assert "new" in retention_report(rolling, g, ["new"], origin_seq={}).limited
    full = RetentionBoundary(scope=SCOPE, mode="full", rebuild="full")
    assert retention_report(full, g, ["s"]).rebuildable == frozenset({"s"})
    eph = RetentionBoundary(scope=SCOPE, mode="ephemeral", rebuild="unavailable")
    assert retention_report(eph, g, ["s"]).unavailable == frozenset({"s"})
