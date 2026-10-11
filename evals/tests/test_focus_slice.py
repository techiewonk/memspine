"""Question-level filter (--query-ids), focus-slice builder and focus scorer."""

from __future__ import annotations

import json
import random

import eval_focus
import make_focus_slice
from memspine_evals.cli import parse_query_ids
from memspine_evals.contracts import EvalItem, Query
from memspine_evals.experiments import _QueryFilter


class _DS:
    def __init__(self, items):
        self._items = items

    def info(self):
        return "info"

    def items(self):
        return iter(self._items)


def _ds():
    return _DS(
        [
            EvalItem("conv-1", (), tuple(Query(f"0-{i}", "q") for i in range(4))),
            EvalItem("conv-2", (), tuple(Query(f"0-{i}", "q") for i in range(3))),
        ]
    )


def test_query_filter_pairs_and_skips_empty_items():
    f = _QueryFilter(_ds(), ("conv-1/0-1", "conv-1/0-3"))
    got = list(f.items())
    assert [i.item_id for i in got] == ["conv-1"]  # conv-2 has nothing selected -> never ingested
    assert [q.query_id for q in got[0].queries] == ["0-1", "0-3"]


def test_query_filter_bare_id_matches_every_item():
    got = list(_QueryFilter(_ds(), ("0-0",)).items())
    assert [(i.item_id, len(i.queries)) for i in got] == [("conv-1", 1), ("conv-2", 1)]


def test_parse_query_ids_formats(tmp_path):
    assert parse_query_ids(None) is None
    assert parse_query_ids("a/1,b/2") == ("a/1", "b/2")
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"query_ids": ["a/1"]}))
    assert parse_query_ids(f"@{p}") == ("a/1",)
    p.write_text(json.dumps(["a/1", "b/2"]))
    assert parse_query_ids(f"@{p}") == ("a/1", "b/2")
    p.write_text("a/1\n\nb/2\n")
    assert parse_query_ids(f"@{p}") == ("a/1", "b/2")


def test_stratified_sample_is_seeded_proportional():
    strata = {("c1", "x"): [f"a{i}" for i in range(30)], ("c2", "x"): [f"b{i}" for i in range(10)]}
    s1 = make_focus_slice.stratified_sample(strata, 8, random.Random(1))
    s2 = make_focus_slice.stratified_sample(strata, 8, random.Random(1))
    assert s1 == s2 and len(s1) == 8
    assert sum(q.startswith("a") for q in s1) == 6


def test_focus_scoring_verdicts():
    sl = {"fail": ["c/f1", "c/f2"], "control": ["c/k1", "c/k2"], "n_correct_total": 100, "n_total": 1540,
          "category": {"c/f1": "cat1", "c/f2": "cat1", "c/k1": "cat1", "c/k2": "cat2"}}
    row = lambda ok: {"score": 1.0 if ok else 0.0, "status": "completed", "answer": "x"}  # noqa: E731
    arm = {"c/f1": row(True), "c/f2": row(False), "c/k1": row(True), "c/k2": row(False)}
    r = eval_focus.score_locomo(arm, sl)
    assert (r["fixed"], r["broken"]) == (1, 1)
    assert r["net"] == 1 - 1 * 50 and r["verdict"] == "REJECT"
    arm["c/k2"] = row(True)
    assert eval_focus.score_locomo(arm, sl)["net"] == 1
