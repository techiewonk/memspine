from __future__ import annotations

from memspine.compose.budget import Budget
from memspine.read.neighbours import (
    ExpandedWindow,
    Hit,
    NeighbourConfig,
    Turn,
    expand,
)
from memspine.schemas.steps.retrieve import NeighbourWindow


class WordCounter:
    name = "words"
    accounting = "exact"

    def count(self, text: str) -> int:
        return len(text.split())


C = WordCounter()


def conv(n: int, session_of=lambda i: "s1", words: int = 2) -> list[Turn]:
    return [Turn(f"t{i}", session_of(i), " ".join(["w"] * words)) for i in range(n)]


def run(hits, index, *, vis=lambda t: True, before=1, after=1, budget=100, config=None):
    return expand(
        hits, index, vis, before=before, after=after, budget=budget, counter=C, config=config
    )


def test_single_window_split_and_trace_types():
    (w,) = run([Hit("t3", "c", 0.9)], {"c": conv(8)}, before=2, after=1)
    assert isinstance(w, ExpandedWindow) and isinstance(w, NeighbourWindow)
    assert w.member_ids == ["t1", "t2", "t3", "t4"]
    assert w.hit_ids == ["t3"] and w.neighbour_ids == ["t1", "t2", "t4"]
    assert w.anchor_id == "t3" and w.anchor_ids == ["t3"]
    assert w.tokens == 8 and w.neighbour_tokens == 6 and w.score == 0.9


def test_overlapping_windows_merge_with_best_anchor_and_no_duplicates():
    hits = [Hit("t5", "c", 0.5), Hit("t3", "c", 0.8)]
    (w,) = run(hits, {"c": conv(10)}, before=2, after=2)
    assert w.member_ids == ["t1", "t2", "t3", "t4", "t5", "t6", "t7"]
    assert len(set(w.member_ids)) == len(w.member_ids)
    assert w.anchor_id == "t5" and w.anchor_ids == ["t5", "t3"]  # best rank first
    assert w.score == 0.8
    assert w.hit_ids == ["t3", "t5"]
    assert "t3" not in w.neighbour_ids and "t5" not in w.neighbour_ids


def test_distant_hits_stay_separate_and_other_conversation_too():
    index = {"a": conv(10), "b": conv(5)}
    ws = run([Hit("t1", "a"), Hit("t8", "a"), Hit("t2", "b")], index)
    assert [w.anchor_id for w in ws] == ["t1", "t8", "t2"]
    assert [w.conversation_id for w in ws] == ["a", "a", "b"]


def test_session_boundary_blocks_and_is_traced():
    index = {"c": conv(6, session_of=lambda i: "s1" if i < 3 else "s2")}
    (w,) = run([Hit("t2", "c")], index, before=2, after=2)
    assert w.member_ids == ["t0", "t1", "t2"]
    assert [(s.turn_id, s.reason) for s in w.skipped] == [("t3", "session_boundary")]
    (x,) = run(
        [Hit("t2", "c")], index, before=2, after=2, config=NeighbourConfig(cross_session=True)
    )
    assert x.member_ids == ["t0", "t1", "t2", "t3", "t4"]


def test_invisible_turn_strict_blocks_bridge():
    vis = lambda t: t.turn_id != "t4"
    (w,) = run([Hit("t3", "c")], {"c": conv(8)}, vis=vis, before=1, after=3)
    assert w.member_ids == ["t2", "t3"]
    assert [(s.turn_id, s.reason) for s in w.skipped] == [("t4", "invisible")]


def test_invisible_turn_loose_bridges_and_never_appears():
    vis = lambda t: t.turn_id != "t4"
    (w,) = run(
        [Hit("t3", "c")],
        {"c": conv(8)},
        vis=vis,
        before=0,
        after=3,
        config=NeighbourConfig(strict=False),
    )
    assert w.member_ids == ["t3", "t5", "t6"]
    assert "t4" not in w.member_ids


def test_invisible_hit_gets_no_window():
    vis = lambda t: t.turn_id != "t3"
    ws = run([Hit("t3", "c")], {"c": conv(8)}, vis=vis)
    assert ws == []


def test_budget_exhaustion_lower_ranked_lose_first():
    # each neighbour costs 2 words; budget 4 = two neighbours
    hits = [Hit("t1", "c", 0.9), Hit("t7", "c", 0.5)]
    ws = run(hits, {"c": conv(10)}, before=1, after=1, budget=4)
    top, low = ws
    assert top.neighbour_ids == ["t0", "t2"]
    assert low.neighbour_ids == [] and low.member_ids == ["t7"]
    assert [s.reason for s in low.skipped] == ["over_budget", "over_budget"]


def test_budget_object_uses_neighbours_share_and_is_not_mutated():
    b = Budget(total=100, counter=C, shares={"neighbours": 0.04, "raw": 0.5})
    assert b.block_cap("neighbours") == 4
    (w,) = run([Hit("t4", "c")], {"c": conv(9)}, before=2, after=2, budget=b)
    assert w.neighbour_tokens == 4 and len(w.neighbour_ids) == 2
    assert b.used == 0


def test_hit_neighbours_are_free_and_deduped():
    hits = [Hit("t2", "c", 0.9), Hit("t3", "c", 0.8)]
    (w,) = run(hits, {"c": conv(6)}, before=1, after=1, budget=4)
    assert w.member_ids == ["t1", "t2", "t3", "t4"]
    assert w.hit_ids == ["t2", "t3"] and w.neighbour_ids == ["t1", "t4"]
    assert w.neighbour_tokens == 4


def test_top_anchors_limit_leaves_single_turns():
    ws = run(
        [Hit("t1", "c"), Hit("t7", "c")],
        {"c": conv(10)},
        config=NeighbourConfig(anchors=1),
    )
    assert ws[1].member_ids == ["t7"]


def test_determinism_and_zero_window():
    hits = [Hit("t5", "c", 0.5), Hit("t3", "c", 0.8), Hit("t9", "c", 0.1)]
    idx = {"c": conv(12)}
    assert run(hits, idx, before=2, after=2, budget=7) == run(
        hits, idx, before=2, after=2, budget=7
    )
    (w,) = run([Hit("t3", "c")], idx, before=0, after=0)
    assert w.member_ids == ["t3"] and w.neighbour_ids == []
