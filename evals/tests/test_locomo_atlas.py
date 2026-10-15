"""LOCOMO-ATLAS: classifier rules, solution-map schema and the text-free / ignored-path guards. Synthetic rows only."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import build_locomo_problem_atlas as ATLAS
import build_locomo_solution_map as SM
import locomo_problem_classes as PC
import pytest


def gt(
    turn="D1:3",
    text="Caroline: I went to a support group",
    *,
    lost=None,
    vector=None,
    lexical=None,
    extra=None,
    in_ctx=False,
):
    return {
        "turn": turn,
        "text": text,
        "source_timestamp": "1:56 pm on 8 May, 2023",
        "in_context": in_ctx,
        "lost_at": lost,
        "ranks": {
            "vector": vector,
            "lexical": lexical,
            "extra": extra or {},
            "fused": None,
            "pool": None,
            "rerank_order": None,
            "final": None,
        },
    }


def row(
    category="single-hop",
    question="Where did Caroline go?",
    gold="support group",
    answer="the park",
    gaps=None,
    bucket=None,
    golds=None,
    correct=False,
    detail=None,
):
    return {
        "item": "conv-26",
        "qid": "0-1",
        "category": category,
        "question": question,
        "gold_answer": gold,
        "answer": answer,
        "correct": correct,
        "primary_gap": gaps,
        "reader_bucket": {"bucket": bucket, "detail": detail} if bucket else None,
        "gold_turns": golds if golds is not None else [gt(in_ctx=gaps == "reader")],
    }


def cls(r, **kw):
    return PC.classify_row(r, speakers=("Caroline", "Melanie"), **kw)


def test_correct_is_ok():
    assert cls(row(correct=True))["rclass"] == "ok"


def test_errata_drops_to_gold_error():
    out = cls(row(gaps="reader"), errata_tag="gold_error")
    assert (out["stage"], out["rclass"]) == ("f", "gold_error")
    # an evidence-label errata tag does not hide a real failure
    assert (
        cls(row(gaps="reader", bucket="wrong_fact"), errata_tag="evidence_label_error")["stage"]
        != "f"
    )


def test_fusion_loss_vector_only_and_lexical_only():
    r = row(gaps="fusion", golds=[gt(lost="fusion", vector=40)])
    assert cls(r)["rclass"] == "fusion_loss_vector_only" and cls(r)["stage"] == "cf"
    r = row(gaps="fusion", golds=[gt(lost="fusion", lexical=12)])
    assert cls(r)["rclass"] == "fusion_loss_lexical_only"
    r = row(gaps="fusion", golds=[gt(lost="fusion", vector=15, lexical=9)])
    assert cls(r)["rclass"] == "fusion_loss_two_leg_crowded"
    assert "leg_top10_cut" in cls(r)["tags"]


def test_rerank_cut_and_temporal_and_open_domain_take_the_category():
    r = row(gaps="rerank", golds=[gt(lost="rerank", vector=3)])
    assert (cls(r)["stage"], cls(r)["rclass"]) == ("cr", "rerank_keep_cut")
    r = row(
        category="temporal",
        question="When did Caroline go?",
        gaps="recall",
        golds=[gt(lost="recall")],
    )
    assert cls(r)["rclass"] == "temporal_evidence_missing"
    r = row(category="open-domain", gaps="recall", golds=[gt(lost="recall")])
    assert cls(r)["rclass"] == "open_domain_inference_retrieval"


def test_list_aggregation_needs_two_gold_turns_and_a_list_shape():
    golds = [gt("D1:1", lost="recall"), gt("D2:1", lost="recall"), gt("D3:1", lost="recall")]
    r = row(
        category="multi-hop",
        question="What activities does Melanie do?",
        gold="pottery, camping",
        gaps="recall",
        golds=golds,
    )
    out = cls(r)
    assert (out["stage"], out["rclass"]) == ("b", "list_aggregation_generic") and out[
        "heuristic"
    ] is True
    one = row(
        category="multi-hop",
        question="What activities does Melanie do?",
        gold="pottery",
        gaps="recall",
        golds=golds[:1],
    )
    assert cls(one)["rclass"] != "list_aggregation_generic"


def test_implicit_gold_when_answer_words_are_not_in_the_turns():
    r = row(
        question="Where did Caroline move from?",
        gold="Sweden",
        gaps="recall",
        golds=[gt(text="Caroline: from my home country", lost="recall")],
    )
    assert cls(r)["rclass"] == "implicit_or_paraphrased_gold"


def test_reader_classes():
    base = {"gaps": "reader"}
    assert cls(row(category="open-domain", **base))["rclass"] == "inference_open_domain"
    assert cls(row(bucket="refusal", **base))["rclass"] == "refusal_premise_denial"
    assert (
        cls(row(question="How long has Caroline had her dog?", bucket="wrong_fact", **base))[
            "rclass"
        ]
        == "date_duration_arithmetic"
    )
    assert (
        cls(
            row(
                question="When did Caroline go?",
                bucket="wrong_absolute_date",
                detail="off by 1 d",
                **base,
            )
        )["rclass"]
        == "date_offset_small"
    )
    assert (
        cls(
            row(
                question="When did Caroline go?",
                bucket="wrong_absolute_date",
                detail="off by 90 d",
                **base,
            )
        )["rclass"]
        == "date_wrong_event"
    )
    assert cls(row(bucket="list_missing_item", **base))["rclass"] == "list_incomplete"
    assert (
        cls(row(question="How many times did Caroline go?", bucket="wrong_fact", **base))["rclass"]
        == "count_wrong"
    )
    assert cls(row(bucket="wrong_fact", **base))["rclass"] == "wrong_detail"


def test_speaker_confusion_and_distractor_are_heuristic():
    r = row(
        question="What does Caroline like?",
        gold="pottery",
        answer="Melanie likes camping",
        bucket="wrong_fact",
        gaps="reader",
    )
    out = cls(r)
    assert out["rclass"] == "speaker_confusion" and out["heuristic"] is True
    r = row(
        question="What does she like?",
        gold="pottery",
        answer="She enjoys kayaking trips every summer",
        bucket="wrong_fact",
        gaps="reader",
    )
    out = cls(r, non_gold_context=["Melanie: we enjoy kayaking trips every summer together"])
    assert out["rclass"] == "distractor_line"


def test_judge_side_classes():
    r = row(
        gaps="reader",
        gold="Poetry reading, conference",
        answer="poetry reading and conference",
        bucket="list_extra_item",
    )
    assert cls(r, convention_credit="list_superset")["rclass"] == "judge_false_negative"
    assert (
        cls(row(gaps="reader", gold="Flood", answer="A flood hit the area.", bucket="wrong_fact"))[
            "rclass"
        ]
        == "judge_false_negative"
    )
    # a negated answer containing the gold words is not credited; an arithmetic answer is not either
    assert (
        cls(row(gaps="reader", gold="Flood", answer="There was no flood.", bucket="wrong_fact"))[
            "rclass"
        ]
        != "judge_false_negative"
    )
    assert (
        cls(
            row(
                gaps="reader",
                gold="two weeks",
                answer="One week, not two weeks",
                bucket="date_arithmetic",
            )
        )["rclass"]
        != "judge_false_negative"
    )
    r = row(
        question="When did Jolene buy it?",
        gaps="reader",
        gold="A year ago",
        answer="She bought it in 2022.",
        bucket="wrong_fact_dated_question",
    )
    assert cls(r)["rclass"] == "relative_date_gold_vs_judge"
    assert (
        cls(row(gaps="reader", bucket="wrong_granularity"))["rclass"]
        == "relative_date_gold_vs_judge"
    )
    assert cls(row(gaps="reader", bucket="wrong_fact"), date_guard_equivalent=True)["stage"] == "e"


def test_every_class_in_the_registry_has_a_rule_and_group():
    for cid, meta in PC.CLASSES.items():
        for k in ("label", "group", "doc", "rule", "heuristic"):
            assert k in meta, (cid, k)
        assert isinstance(meta["heuristic"], bool)
    assert set(PC.STAGE_OF_CLASS) <= set(PC.CLASSES)


def test_text_helpers():
    assert PC.asks_duration("How long has she had it?") and not PC.asks_count(
        "How long has she had it?"
    )
    assert PC.asks_count("How many times did he go?")
    assert PC.gold_is_list("pottery, camping and swimming")
    assert PC.relative_gold("The week before 9 June 2023") and not PC.relative_gold("7 May 2023")
    assert PC.sessions_of(
        {"gold_turns": [{"turn": "D3:1"}, {"turn": "D1:2"}, {"turn": "D3:4"}]}
    ) == [1, 3]


# ----------------------------------------------------------------------------------------------------------------------
# Solution map schema
# ----------------------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def smap():
    return SM.build_map()


def test_solution_map_is_valid(smap):
    assert SM.validate(smap) == []


def test_every_class_is_mapped_or_reported_as_gap(smap):
    gap_classes = {g["class"] for g in smap["structural_gaps"]}
    for cid, meta in smap["classes"].items():
        assert meta["solutions"] or meta["no_solution_needed"] or cid in gap_classes, cid


def test_solution_fields_and_references(smap):
    for s in smap["solutions"]:
        assert s["status"] in SM.STATUSES
        assert s["doc_refs"] and all(r["file"] and r["section"] for r in s["doc_refs"])
        assert all(sid in smap["screens"] for sid in s["screens"])
        assert all(rel in SM.RELATIONS for rel in s["classes"].values())
        if s["status"] == "not_built":
            assert not s["screens"]


def test_catalogue_examples_from_the_brief(smap):
    by_id = {s["id"]: s for s in smap["solutions"]}
    assert "S14" in by_id["SOL-FAMFUS"]["screens"] and "S51" in by_id["SOL-FAMFUS"]["screens"]
    assert "S67" in by_id["SOL-RRPOOL"]["screens"]
    assert {"S34", "S35", "S40"} <= set(by_id["SOL-STANDING"]["screens"])
    assert "S55" in by_id["SOL-DEEPREAD"]["screens"]
    assert {"S30", "S61"} <= set(by_id["SOL-GRAPH"]["screens"])
    assert {"S31", "S33", "S01"} <= set(by_id["SOL-EPISODE"]["screens"])
    assert by_id["SOL-JUDGE-I77"]["status"] == "not_built"


def test_validate_catches_bad_maps(smap):
    bad = copy.deepcopy(smap)
    bad["solutions"][0]["status"] = "maybe"
    assert any("bad status" in e for e in SM.validate(bad))
    bad = copy.deepcopy(smap)
    bad["solutions"][0]["classes"]["no_such_class"] = "primary"
    assert any("unknown class" in e for e in SM.validate(bad))
    bad = copy.deepcopy(smap)
    bad["solutions"][0]["doc_refs"] = []
    assert any("no doc_ref" in e for e in SM.validate(bad))
    bad = copy.deepcopy(smap)
    bad["solutions"][0]["screens"] = ["S999"]
    assert any("unknown screen" in e for e in SM.validate(bad))


def test_committed_map_matches_the_generator(smap):
    p = Path(SM.__file__).parent / "analysis" / "locomo_solution_map.json"
    if p.exists():
        committed = json.loads(p.read_text(encoding="utf-8"))
        assert [s["id"] for s in committed["solutions"]] == [s["id"] for s in smap["solutions"]]
        assert committed["schema"] == SM.SCHEMA


# ----------------------------------------------------------------------------------------------------------------------
# Estimates and guards
# ----------------------------------------------------------------------------------------------------------------------
def test_screen_estimates_use_the_primary_classes(smap):
    counts = {
        "by_class": {
            "fusion_loss_vector_only": 10,
            "fusion_loss_lexical_only": 4,
            "fusion_loss_two_leg_crowded": 1,
        }
    }
    est = {r["id"]: r for r in ATLAS.screen_estimates(smap, counts)}
    assert est["S14"]["ub_primary"] == 15
    assert est["S14"]["exp30"] == 4.5 and est["S14"]["exp60"] == 9.0
    # a bundle is the union of its parts, never the sum
    assert est["S60"]["ub_primary"] >= est["S14"]["ub_primary"]


def test_writer_refuses_paths_outside_the_ignored_analysis_folder(tmp_path):
    with pytest.raises(SystemExit):
        ATLAS.assert_ignored(tmp_path / "atlas.html")
    with pytest.raises(SystemExit):
        ATLAS.assert_ignored(Path(ATLAS.HERE) / "analysis" / "leak.html")
    ATLAS.assert_ignored(ATLAS.OUT_DIR / "locomo_problem_atlas.html")  # ignored: allowed


def test_text_free_check_refuses_dataset_text():
    rows = [
        {
            "question": "Where did Caroline go on her summer holiday this year?",
            "gold": "x",
            "answer": "y",
            "gold_turns": [],
        }
    ]
    with pytest.raises(SystemExit):
        ATLAS.verify_text_free(
            "see: where did caroline go on her summer holiday this year? done", rows
        )
    ATLAS.verify_text_free("ids and counts only: conv-26 0-1 b 12", rows)
