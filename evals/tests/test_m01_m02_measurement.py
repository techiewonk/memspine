"""M01 (finalised OP-Bench scores, completion states, reconcile) and M02 (evidence normaliser).

No dataset text: synthetic ids and answers only.
"""

from __future__ import annotations

import json

import pytest
from memspine_evals import errata as ER
from memspine_evals import evidence as EV
from memspine_evals import opbench as OB

# ------------------------------------------------------------------------------------- M01


def _row(qid, item, label, score, *, status="completed", answer="an answer", **kw):
    return {
        "kind": "result",
        "query_id": qid,
        "item_id": item,
        "type_label": label,
        "score": score,
        "status": status,
        "answer": answer,
        **kw,
    }


def _rows():
    return [
        _row("p:irr:0", "c:p", "irrelevance_easy/fully_irrelevant", 1.0),
        _row("p:irr:1", "c:p", "irrelevance_easy/fully_irrelevant", 0.0),
        _row("p:syc:0", "c:p", "sycophancy/fact", 1.0),
        _row("p:div:0", "c:p", "diversity", 1.0),  # provisional: first answer, empty pool
        _row("p:div:1", "c:p", "diversity", 0.4),
        _row("p:div:2", "c:p", "diversity", 0.9, status="truncated", answer_truncated=True),
    ]


def _summary(rows=None):
    per_probe = {
        "p:irr:0": 1.0,
        "p:irr:1": 0.0,
        "p:syc:0": 1.0,
        "p:div:0": 0.2,
        "p:div:1": 0.3,
        "p:div:2": 0.45,
    }
    return {
        "per_probe": per_probe,
        "n_probes": 6,
        "probe_mean": {
            "irrelevance_easy": 0.5,
            "sycophancy/fact": 1.0,
            "diversity": (0.2 + 0.3 + 0.45) / 3,
        },
        "official": {
            "by_task_type": {
                "irrelevance_easy": {"average": 0.5},
                "sycophancy": {"average": 1.0},
                "diversity": {"average": 0.31},
            }
        },
    }


def test_final_score_replaces_provisional_and_keeps_it_visible():
    final = _summary()["per_probe"]
    d = OB.diagnose_row(_rows()[3], final)
    assert (d["score"], d["basis"], d["provisional"], d["differs"]) == (0.2, "final", 1.0, True)
    assert d["ok"] == 0  # the provisional 1.0 would have been a pass


def test_truncated_positive_score_is_not_an_answer_pass():
    final = {"q": 0.9}
    row = _row("q", "c:p", "diversity", 0.9, status="truncated", answer_truncated=True)
    d = OB.diagnose_row(row, final)
    assert d["score"] == 0.9 and d["state"] == "truncated" and d["ok"] == 0


@pytest.mark.parametrize(
    ("row", "state"),
    [
        (_row("q", "i", "x", 1.0), "complete"),
        (_row("q", "i", "x", 1.0, answer="  "), "empty"),
        (_row("q", "i", "x", 1.0, status="truncated"), "truncated"),
        (_row("q", "i", "x", 1.0, answer_truncated=True), "truncated"),
        (_row("q", "i", "x", 0.0, error="boom"), "failed"),
        (_row("q", "i", "x", 0.0, meta={"judge_failed": True}), "failed"),
        (_row("q", "i", "x", None, status="retrieval_only", answer=""), "retrieval_only"),
        (_row("q", "i", "x", None, meta={"retrieval_only": True}), "retrieval_only"),
        (_row("q", "i", "x", None, status="pending", answer=""), "pending"),
    ],
)
def test_states_are_separate(row, state):
    assert OB.answer_state(row) == state
    assert OB.diagnose_row(row, None)["ok"] == int(state == "complete" and (row["score"] or 0) >= 0.5)


def test_repetition_without_final_is_pending_not_scored():
    d = OB.diagnose_row(_row("q", "i", "diversity", 0.8), None)
    assert d["basis"] == "pending" and d["score"] is None and d["ok"] == 0
    assert OB.diagnose_row(_row("q", "i", "irrelevance_hard", 1.0), None)["basis"] == "judge"


def test_reconcile_exact_and_counts_provisional_differences():
    t = OB.reconcile(_rows(), _summary())
    assert t["n_final"] == t["n_probes"] == 6
    assert t["repetition"]["n_provisional_differs_from_final"] == 3
    assert t["states"] == {"complete": 5, "truncated": 1}
    assert t["task_macro_checked"] == ["irrelevance_easy", "sycophancy"]


def test_reconcile_rejects_any_disagreement():
    s = _summary()
    s["probe_mean"]["irrelevance_easy"] = 0.6
    with pytest.raises(OB.ReconcileError):
        OB.reconcile(_rows(), s)
    s = _summary()
    s["n_probes"] = 7
    with pytest.raises(OB.ReconcileError):
        OB.reconcile(_rows(), s)
    s = _summary()
    s["per_probe"]["ghost"] = 0.1
    with pytest.raises(OB.ReconcileError):
        OB.reconcile(_rows(), s)
    s = _summary()
    del s["per_probe"]["p:div:1"]  # a complete repetition answer with no final score
    with pytest.raises(OB.ReconcileError):
        OB.reconcile(_rows(), s)


def test_aggregate_formula_is_unchanged():
    """The official aggregation still reads row scores for judged probes and ignores the
    provisional repetition value (per_probe is leave-one-out cosine)."""
    vec = {"a": [1.0, 0.0], "b": [0.0, 1.0]}

    def embed(texts):
        return [vec[t] for t in texts]

    rows = [
        _row("d0", "c:p", "diversity", 1.0, answer="a"),
        _row("d1", "c:p", "diversity", 0.123, answer="b"),
        _row("i0", "c:p", "irrelevance_easy/fully_irrelevant", 1.0),
    ]
    agg = OB.aggregate(rows, embed=embed)
    assert agg["per_probe"]["d0"] == pytest.approx(1.0)
    assert agg["official"]["by_task_type"]["diversity"]["average"] == pytest.approx(1.0)
    assert agg["official"]["by_task_type"]["irrelevance_easy"]["average"] == 1.0
    assert OB.reconcile(rows, agg)["n_probes"] == 3


# ------------------------------------------------------------------------------------- M02

KNOWN = {f"D{s}:{t}" for s in range(1, 32) for t in range(1, 40)}


def test_ok_list_and_separators():
    v = EV.normalise_evidence(["D8:6; D9:17", "D1:2"], KNOWN)
    assert v.status == "ok" and v.resolved_ids == ("D8:6", "D9:17", "D1:2")
    assert v.restore() == ["D8:6; D9:17", "D1:2"]


@pytest.mark.parametrize(
    ("raw", "resolved", "how"),
    [
        ("D:11:26", ("D11:26",), "stray_colon"),
        ("D30:05", ("D30:5",), "leading_zero"),
        ("d4:4", ("D4:4",), "case"),
        ("D9:1 D4:4 D4:6", ("D9:1", "D4:4", "D4:6"), "exact"),
        ("D22:1 D22:2 D9:10 D9:11", ("D22:1", "D22:2", "D9:10", "D9:11"), "exact"),
    ],
)
def test_repairs_resolve_within_the_conversation(raw, resolved, how):
    r = EV.normalise_ref(raw, KNOWN)
    assert r.status == "repaired" and r.resolved_ids == resolved and r.raw == raw
    assert r.tokens[0].how == how


@pytest.mark.parametrize("raw", ["D", "D40:99", "", "D31:40", "zz"])
def test_nothing_is_guessed(raw):
    r = EV.normalise_ref(raw, KNOWN)
    assert r.status == "unresolved" and r.resolved_ids == ()


def test_repair_is_scoped_to_the_given_conversation():
    other = {"D11:26"}
    assert EV.normalise_ref("D:11:26", other).status == "repaired"
    assert EV.normalise_ref("D:11:26", {"D11:27"}).status == "unresolved"  # other conversation


def test_partial_resolution_keeps_the_resolved_part_and_flags_the_rest():
    v = EV.normalise_evidence(["D1:2", "D1:3 D99:1"], KNOWN)
    assert v.status == "unresolved"
    assert v.resolved_ids == ("D1:2", "D1:3") and v.unresolved_tokens == ("D99:1",)


def test_official_recall_is_untouched_and_diagnostic_is_separate():
    v = EV.normalise_evidence(["D1:2", "D:11:26"], KNOWN)
    out = EV.recall_pair(["D1:2", "D11:26"], v)
    assert out["official"] == 0.5  # the malformed token can never be hit
    assert out["adjudicated"] == 1.0 and out["differs"] and out["status"] == "repaired"
    clean = EV.recall_pair(["D1:2"], EV.normalise_evidence(["D1:2"], KNOWN))
    assert clean["official"] == clean["adjudicated"] == 1.0 and not clean["differs"]
    none = EV.recall_pair(["D1:2"], EV.normalise_evidence(["D"], KNOWN))
    assert none["official"] == 0.0 and none["adjudicated"] is None


def test_status_counts():
    views = [EV.normalise_evidence(x, KNOWN) for x in (["D1:1"], ["D:1:2"], ["D"], ["D1:1", "D"])]
    c = EV.status_counts(views)
    assert c["questions"] == 4 and c["question_ok"] == 1 and c["question_repaired"] == 1
    assert c["question_unresolved"] == 2 and c["ref_unresolved"] == 2


def test_errata_candidates_and_adjudication_rules(tmp_path):
    known = {"c1": KNOWN}
    cands = ER.evidence_candidates(
        [("c1", "1-1", ["D1:1"]), ("c1", "1-2", ["D"]), ("c1", "1-3", ["D:2:3"])], known
    )
    assert [(c["qid"], c["status"]) for c in cands] == [("1-2", "unresolved"), ("1-3", "repaired")]
    path = tmp_path / "e.json"
    base = {"item": "c1", "qid": "1-2", "tag": "bad_evidence_id"}
    good = {**base, "evidence_adjudicated": ["D1:18"], "source_span": {"turn": "D1:18", "quote": "x"},
            "reviewed_by": "second reader"}
    path.write_text(json.dumps({"schema": "errata/v1", "entries": [good]}), encoding="utf-8")
    adj = ER.adjudications(path)
    ER.check_adjudications(adj, known)  # passes
    assert ER.load_errata(path) == {}  # bad_evidence_id never drops a question
    for bad in (
        {**good, "evidence_adjudicated": ["D99:1"]},  # not a turn of the conversation
        {**good, "source_span": {"turn": "D1:18", "quote": " "}},  # no exact span
        {**good, "reviewed_by": None},  # no independent review
        {**good, "evidence_adjudicated": []},
    ):
        path.write_text(json.dumps({"schema": "errata/v1", "entries": [bad]}), encoding="utf-8")
        with pytest.raises(ER.ErrataError):
            ER.check_adjudications(ER.adjudications(path), known)
