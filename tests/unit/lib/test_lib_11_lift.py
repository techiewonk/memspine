from __future__ import annotations

from memspine.read.lift import (
    STAGES,
    Candidate,
    LiftPolicy,
    cap,
    lift,
    pipeline,
    rerank_pool,
)
from memspine.schemas.durable.lineage import LineageEdge

TEXT = {
    "turn-a": "alpha lantern",
    "turn-b": "beta kettle",
    "turn-c": "gamma ferry",
    "turn-d": "delta moss",
}


def edges(*triples: tuple[str, str, str]) -> dict[str, list[LineageEdge]]:
    out: dict[str, list[LineageEdge]] = {}
    for child, parent, role in triples:
        out.setdefault(child, []).append(LineageEdge(child=child, parent=parent, role=role))
    return out


def fake_rerank(query: str, texts: list[str]) -> list[float | None]:
    return [float(len(t)) for t in texts]


def run(cands, lineage, visible=lambda r: True, rerank=fake_rerank, **kw):
    args = {"max_turns_per_hit": 3, "pool_cap": 10, "max_rerank": 10, "text_of": TEXT.get}
    args.update(kw)
    return pipeline("q", cands, lineage, visible, rerank, **args)


def test_lifted_turn_is_scored_by_rerank():
    res = run([Candidate("fact-1", "fact", rank=0)], edges(("fact-1", "turn-a", "support")))
    (item,) = res.items
    assert item.record_id == "turn-a" and item.lifted and item.scored
    assert item.score == float(len(TEXT["turn-a"]))
    assert item.provenance[0].via == "fact-1" and item.provenance[0].role == "support"


def test_unscored_tail_keeps_none_never_zero():
    cands = [Candidate(f"turn-{c}", rank=i) for i, c in enumerate("abcd")]
    res = run(cands, {}, max_rerank=2)
    assert [c.scored for c in res.items] == [True, True, False, False]
    assert all(c.score is None for c in res.items[2:])
    assert [i.score for i in res.scored_items()[2:]] == [None, None]
    assert [c.record_id for c in res.items[2:]] == ["turn-c", "turn-d"]


def test_function_none_stays_unscored_and_real_zero_stays_zero():
    pool = [Candidate("turn-a", rank=0, text="x"), Candidate("turn-b", rank=1, text="yy")]
    out = rerank_pool("q", pool, lambda q, t: [None, 0.0], max_rerank=5)
    assert [c.record_id for c in out] == ["turn-b", "turn-a"]
    assert out[0].scored and out[0].score == 0.0
    assert not out[1].scored and out[1].score is None


def test_candidate_without_text_unscored():
    pool = [Candidate("turn-a"), Candidate("turn-b", text="t", rank=1)]
    out = rerank_pool("q", pool, fake_rerank, max_rerank=5)
    assert [c.record_id for c in out] == ["turn-b", "turn-a"]
    assert [c.scored for c in out] == [True, False]


def test_invisible_lifted_turn_dropped():
    lin = edges(("fact-1", "turn-a", "support"), ("fact-1", "turn-b", "support"))
    res = run(
        [Candidate("fact-1", "fact")],
        lin,
        visible=lambda r: r != "turn-b",
        policy=LiftPolicy(partial_lineage="keep_visible"),
    )
    assert [c.record_id for c in res.items] == ["turn-a"]
    assert any(
        d.record_id == "turn-b" and d.reason == "lifted_turn_not_visible" for d in res.trace.dropped
    )


def test_strict_policy_drops_hit_with_hidden_support():
    lin = edges(("fact-1", "turn-a", "support"), ("fact-1", "turn-b", "support"))
    res = run([Candidate("fact-1", "fact")], lin, visible=lambda r: r != "turn-b")
    assert res.items == ()
    assert any(
        d.record_id == "fact-1" and d.reason == "partial_lineage_hidden" for d in res.trace.dropped
    )


def test_all_support_hidden_drops_hit():
    res = run(
        [Candidate("fact-1", "fact")],
        edges(("fact-1", "turn-a", "support")),
        visible=lambda r: r != "turn-a",
    )
    assert res.items == ()
    assert any(d.reason == "no_visible_support" for d in res.trace.dropped)


def test_derived_without_live_lineage_loses_slot():
    res = run([Candidate("fact-1", "fact"), Candidate("turn-c", rank=1)], {})
    assert [c.record_id for c in res.items] == ["turn-c"]
    assert any(d.record_id == "fact-1" and d.reason == "no_live_lineage" for d in res.trace.dropped)


def test_context_only_lineage_is_not_live_support():
    res = run([Candidate("fact-1", "fact")], edges(("fact-1", "turn-a", "context")))
    assert res.items == ()


def test_non_turn_root_is_dead_end():
    pol = LiftPolicy(is_turn=lambda r: r.startswith("turn-"))
    res = run([Candidate("fact-1", "fact")], edges(("fact-1", "note-9", "support")), policy=pol)
    assert res.items == ()
    assert res.trace.dropped[0].reason == "lineage_unresolvable"


def test_invisible_derived_hit_dropped():
    res = run(
        [Candidate("fact-1", "fact")],
        edges(("fact-1", "turn-a", "support")),
        visible=lambda r: r != "fact-1",
    )
    assert res.items == ()
    assert res.trace.dropped[0].reason == "derived_hit_not_visible"


def test_transitive_walk_and_fanout_limit_binding_first():
    lin = edges(
        ("sum-1", "fact-2", "member"),  # member is not a lift role
        ("sum-1", "turn-a", "support"),
        ("sum-1", "turn-b", "binding"),
        ("sum-1", "turn-c", "support"),
        ("sum-1", "turn-d", "support"),
    )
    out = lift([Candidate("sum-1", "summary")], lin, lambda r: True, max_turns_per_hit=2)
    assert [c.record_id for c in out] == ["turn-b", "turn-a"]
    lin2 = edges(("sum-1", "fact-2", "support"), ("fact-2", "turn-a", "support"))
    out2 = lift([Candidate("sum-1", "summary")], lin2, lambda r: True, max_turns_per_hit=3)
    assert out2[0].record_id == "turn-a" and out2[0].provenance[0].hops == 2


def test_dedupe_merges_provenance_and_keeps_direct():
    lin = edges(("fact-1", "turn-a", "support"), ("fact-2", "turn-a", "binding"))
    res = run(
        [
            Candidate("fact-1", "fact", rank=0),
            Candidate("turn-a", rank=1),
            Candidate("fact-2", "fact", rank=2),
        ],
        lin,
    )
    (item,) = res.items
    assert item.rank == 0 and not item.lifted
    assert {p.via for p in item.provenance} == {"fact-1", "fact-2"}
    assert res.trace.count("dedupe").n_out == 1


def test_cap_keeps_protected():
    pool = [Candidate(f"turn-{i}", rank=i, protected=(i == 9)) for i in range(10)]
    assert [c.record_id for c in cap(pool, 3)] == ["turn-0", "turn-1", "turn-9"]
    many = [Candidate(f"turn-{i}", rank=i, protected=True) for i in range(5)]
    assert [c.record_id for c in cap(many, 2)] == ["turn-0", "turn-1"]
    assert cap(pool, 0) == []


def test_post_lift_pool_never_exceeds_cap_and_fanout_cannot_crowd_out_protected():
    lin = edges(*[("fact-1", f"turn-{i}", "support") for i in range(8)])
    cands = [Candidate("fact-1", "fact", rank=0), Candidate("turn-z", rank=1, protected=True)]
    res = run(cands, lin, pool_cap=3, max_turns_per_hit=8)
    assert len(res.items) == 3 and "turn-z" in {c.record_id for c in res.items}
    assert res.trace.count("lift").n_out == 9 and res.trace.count("cap").n_out == 3
    assert sum(d.reason == "over_cap" for d in res.trace.dropped) == 6


def test_stage_order_in_trace():
    res = run([Candidate("turn-a")], {})
    assert res.trace.order == STAGES == ("lift", "readmit", "dedupe", "cap", "rerank")


def test_rerank_called_once_on_deduped_lifted_pool():
    calls: list[list[str]] = []

    def spy(q, texts):
        calls.append(list(texts))
        return [1.0] * len(texts)

    lin = edges(("fact-1", "turn-a", "support"), ("fact-1", "turn-b", "support"))
    run([Candidate("fact-1", "fact"), Candidate("turn-a", rank=1)], lin, rerank=spy)
    assert calls == [[TEXT["turn-a"], TEXT["turn-b"]]]


def test_readmit_rechecks_direct_hits():
    res = run(
        [Candidate("turn-a"), Candidate("turn-b", rank=1)], {}, visible=lambda r: r != "turn-a"
    )
    assert [c.record_id for c in res.items] == ["turn-b"]
    assert res.trace.dropped[0].stage == "readmit"


def test_deterministic():
    lin = edges(("fact-1", "turn-a", "support"), ("fact-2", "turn-b", "binding"))
    cands = [
        Candidate("fact-2", "fact", rank=1),
        Candidate("fact-1", "fact", rank=1),
        Candidate("turn-c", rank=2),
    ]
    assert run(cands, lin) == run(list(cands), lin)
