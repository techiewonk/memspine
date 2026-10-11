"""Reader-side open gaps: B3/C4/R2-6 inference clause, C6 list clause, C9 detail clause, C10 retry
limits, I18 preference clause and adherence scaffold, I13 judge-calibration set. No model call."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from memspine_evals import refusal
from memspine_evals.contracts import ReaderAnswer
from memspine_evals.readers import (
    GROUNDED_GENERIC_QA_PROMPT,
    QA_PROMPTS,
    ROUTED_QA_PROMPTS,
    generic_qa_shape,
)
from memspine_evals.refusal import (
    MAX_RETRIES,
    RefusalRetryReader,
    named_entities,
    names_absent_entity,
)

EVALS = Path(__file__).resolve().parents[1]
BENCH_NAMES = ("locomo", "longmemeval", "op-bench", "opbench", "beam", "prefeval", "lamp", "convomem")
NEW = ("grounded_generic_infer", "grounded_generic_list", "grounded_generic_prefs")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def test_existing_prompt_ids_are_byte_identical() -> None:
    assert _sha(QA_PROMPTS["grounded_generic"]) == "147e6cd54d5fae44"
    # 2026-10-11: ``grounded`` / ``grounded_detail`` are the clean prompts (placeholders for the
    # example dates); the contaminated texts survive byte-identical as ``*_legacy``.
    assert _sha(QA_PROMPTS["grounded_legacy"]) == "314a230e5871ff32"
    assert _sha(QA_PROMPTS["grounded_detail_legacy"]) == "c9c7a18b1abd70fe"
    assert _sha(QA_PROMPTS["grounded"]) == "71c12de625d763d4"
    assert _sha(QA_PROMPTS["grounded_detail"]) == "2041cb1d42c3014a"
    assert _sha(QA_PROMPTS["grounded_v2"]) == "8d07d3f6294e9cce"
    assert _sha(QA_PROMPTS["grounded_v3"]) == "b3e8ee1a7aa139d6"
    assert _sha(QA_PROMPTS["grounded_ordered"]) == "abcd8ba8ec0fe324"
    assert _sha(QA_PROMPTS["default"]) == "83fc53c604167050"


@pytest.mark.parametrize("name", NEW)
def test_new_prompts_render_and_are_generic(name: str) -> None:
    text = QA_PROMPTS[name].format(context="[2024-01-02] I moved to Oslo.", question="Q?", question_date="2024-03-01")
    assert "Oslo" in text and "2024-03-01" in text and "Q?" in text
    assert text != GROUNDED_GENERIC_QA_PROMPT.format(context="[2024-01-02] I moved to Oslo.", question="Q?", question_date="2024-03-01")
    low = QA_PROMPTS[name].lower()
    assert not any(n in low for n in BENCH_NAMES)


def test_infer_prompt_commits_but_keeps_factual_refusal() -> None:
    p = QA_PROMPTS["grounded_generic_infer"]
    assert "do not refuse" in p and "most likely answer" in p and "one-line reason" in p
    assert "only for a factual question" in p
    assert "reply exactly: Not mentioned in the conversation." in p  # the factual refusal stays
    assert "specific detail" in p  # C9 detail clause rides in the same variant
    assert p.replace(p[p.index("If the message asks whether") : p.index("Keep the answer short.")], "") == GROUNDED_GENERIC_QA_PROMPT


def test_list_prompt_enumerates_every_line_dedupes_and_counts() -> None:
    p = QA_PROMPTS["grounded_generic_list"]
    for phrase in ("every memory line", "distinct item", "merge repeated mentions", "count of that merged list"):
        assert phrase in p
    assert "list every matching item" not in p  # not grounded_detail's wording
    assert "list every matching item" in QA_PROMPTS["grounded_detail"]


def test_prefs_prompt_follows_silently_and_not_on_unrelated() -> None:
    p = QA_PROMPTS["grounded_generic_prefs"]
    assert "without announcing or quoting them" in p and "never apply a preference to a request it is unrelated to" in p
    assert p.count("Not mentioned") == 1 and "question whose answer the memories do not contain" in p


@pytest.mark.parametrize(
    ("question", "variant"),
    [
        ("Would Caroline likely have Dr. Seuss books on her bookshelf?", "infer"),
        ("What might John's financial status be?", "infer"),
        ("Is it likely that Nate has friends besides Joanna?", "infer"),
        ("When would Ana likely visit Paris?", "plain"),  # a date question stays plain
        ("How many hikes has Joanna been on?", "list"),
        ("What activities does Melanie partake in?", "list"),
        ("What pet does Ana have?", "plain"),
    ],
)
def test_generic_router(question: str, variant: str) -> None:
    assert generic_qa_shape(question) == variant
    routed = ROUTED_QA_PROMPTS["routed_generic"]
    rendered = routed.format(context="c", question=question, question_date="d")
    assert rendered == routed.variants[variant].format(context="c", question=question, question_date="d")
    assert routed.describe()["qa_prompt"] == "routed_generic"
    assert routed.describe()["qa_router"] == "generic-shape-v1"


def test_routed_describe_unchanged() -> None:
    d = ROUTED_QA_PROMPTS["routed"].describe()
    assert d["qa_prompt"] == "routed" and d["qa_router"] == "shape-v1"


def test_cli_accepts_new_prompts_and_retry_guard() -> None:
    from memspine_evals.cli import build_parser

    parser = build_parser()
    for name in (*NEW, "routed_generic"):
        assert parser.parse_args(["c0-1", "--dataset", "locomo", "--qa-prompt", name]).qa_prompt == name
    assert parser.parse_args(["c0-1", "--dataset", "locomo", "--retry-guard"]).retry_guard is True
    assert parser.parse_args(["c0-1", "--dataset", "locomo"]).retry_guard is False


# ---- C10 retry limits ---------------------------------------------------------------------------------
class _Reader:
    reader_id = "stub"
    model = "stub-model"
    makes_model_calls = True

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls = 0

    def describe(self) -> dict[str, Any]:
        return {"reader_id": self.reader_id}

    async def answer(self, question: str, context: str, question_date: str | None = None) -> ReaderAnswer:
        self.calls += 1
        return ReaderAnswer(text=self.replies.pop(0) if self.replies else "Not mentioned.", model_calls=1)


CTX = "[2023-05-25] Caroline: I went hiking in Denver last Friday"


def _ask(reader: RefusalRetryReader, question: str, context: str = CTX) -> ReaderAnswer:
    return asyncio.run(reader.answer(question, context))


def test_retry_is_capped_at_one_and_discards_a_second_refusal() -> None:
    assert MAX_RETRIES == 1
    inner = _Reader("Not mentioned.", "Still not mentioned.", "Denver")
    out = _ask(RefusalRetryReader(inner), "Where did Caroline hike?")
    assert inner.calls == 2 and out.text == "Not mentioned."  # never a third ask
    assert out.extra_meta["retry_accepted"] is False


def test_no_retry_on_empty_context() -> None:
    inner = _Reader("Not mentioned.")
    out = _ask(RefusalRetryReader(inner), "Where did Caroline hike?", context="  ")
    assert inner.calls == 1 and out.text == "Not mentioned."


def test_default_reader_id_and_behaviour_unchanged_by_the_guard_option() -> None:
    off = RefusalRetryReader(_Reader())
    assert off.reader_id == "stub+retry-neutral" and off.guard_absent_entity is False
    assert "retry_guard_absent_entity" not in off.describe()
    inner = _Reader("Not mentioned.", "Rome")
    # an absent entity is still retried when the guard is off
    assert _ask(RefusalRetryReader(inner), "What did Melanie say about Rome?").text == "Rome"
    assert inner.calls == 2


def test_guard_skips_a_question_about_an_entity_absent_from_the_context() -> None:
    inner = _Reader("Not mentioned.", "Rome")
    guarded = RefusalRetryReader(inner, guard_absent_entity=True)
    out = _ask(guarded, "What did Melanie say about hiking?")
    assert inner.calls == 1 and out.text == "Not mentioned." and guarded.skipped == 1 and guarded.retried == 0
    assert "retry_skipped" in out.extra_meta
    assert guarded.reader_id.endswith("-entityguard")
    assert guarded.describe()["retry_guard_absent_entity"] is True
    # a present entity is retried as before
    inner2 = _Reader("Not mentioned.", "Denver")
    assert _ask(RefusalRetryReader(inner2, guard_absent_entity=True), "Where did Caroline hike?").text == "Denver"
    assert inner2.calls == 2


@pytest.mark.parametrize(
    ("question", "names"),
    [
        ("What did Caroline's friend say?", ["Caroline"]),
        ("When did Jon and Gina go to Paris in May?", ["Jon", "Gina", "Paris"]),
        ("What is the weather like?", []),
        ("Which Friday was it?", []),
    ],
)
def test_named_entities(question: str, names: list[str]) -> None:
    assert named_entities(question) == names


def test_names_absent_entity_uses_only_question_and_context() -> None:
    assert names_absent_entity("What did Melanie say?", CTX)
    assert not names_absent_entity("Where did Caroline go?", CTX)
    # one known person is enough: a world-knowledge question about a stored person stays retryable
    assert not names_absent_entity("Would Caroline enjoy Vivaldi?", CTX)
    assert names_absent_entity("What did Melanie and Vivaldi say?", CTX)
    assert not names_absent_entity("what is the plan?", CTX)
    assert "gold" not in refusal.names_absent_entity.__code__.co_varnames


# ---- I18 preference adherence scaffold ----------------------------------------------------------------
def test_preference_adherence_block() -> None:
    from forensics_report import avoided_terms, preference_adherence_block, preference_label

    assert preference_label({"type_label": "explicit/travel", "gold": "I dislike beaches"}) == "I dislike beaches"
    assert preference_label({"meta": {"preference": "no spicy food"}, "type_label": "x"}) == "no spicy food"
    assert preference_label({"type_label": "temporal", "gold": "7 May 2023"}) is None
    assert avoided_terms("I don't like spicy food, and I hate crowds.") == ["spicy", "food", "crowds"]
    assert avoided_terms("I love hiking") == []
    rows = [
        {"mode": "qa", "preference": "I don't like spicy food", "form": "explicit", "correct": True,
         "answer": "Try a mild lentil soup.", "gold_turns": [{"in_context": True}]},
        {"mode": "qa", "preference": "I don't like spicy food", "form": "explicit", "correct": False,
         "answer": "As you mentioned, you dislike it, but this spicy curry is great.", "gold_turns": [{"in_context": False}]},
        {"mode": "qa", "form": "x", "correct": True, "answer": "no preference label", "gold_turns": []},
    ]
    b = preference_adherence_block(rows)
    assert b["n"] == 2 and b["judged_adherence"] == 0.5
    assert b["preference_turn_in_context"] == 0.5 and b["announced_rate"] == 0.5
    assert b["n_constrained"] == 2 and b["violation_proxy"] == 0.5
    assert set(b["by_form"]) == {"explicit"}
    assert preference_adherence_block([rows[2]]) is None


# ---- I13 judge calibration set ------------------------------------------------------------------------
CAL = EVALS / "analysis" / "judge_calibration_dev.jsonl"


def _cal() -> list[dict[str, Any]]:
    return [json.loads(x) for x in CAL.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_calibration_set_shape_and_dev_only() -> None:
    rows = _cal()
    assert 40 <= len(rows) <= 50 and len({r["id"] for r in rows}) == len(rows)
    split = json.loads((EVALS / "analysis" / "locomo_split.json").read_text(encoding="utf-8"))
    assert {r["item"] for r in rows} <= set(split["dev_items"])
    assert not {r["item"] for r in rows} & set(split["heldout_items"])
    kinds = {r["kind"] for r in rows}
    assert {"paraphrase", "wrong_plausible", "refusal", "partial_list", "date_equivalent"} <= kinds
    assert all(r["reason"] and r["human_verdict"] in {"CORRECT", "PARTIAL", "WRONG"} for r in rows)
    assert all(r["human_verdict"] == "WRONG" for r in rows if r["kind"] == "refusal")
    assert all(r["human_verdict"] == "PARTIAL" for r in rows if r["kind"] == "partial_list")


def test_calibration_set_matches_the_label_table() -> None:
    from build_judge_calibration import LABELS

    rows = {r["id"]: r for r in _cal()}
    assert set(rows) == set(LABELS)
    for key, (kind, verdict, reason, borderline) in LABELS.items():
        assert (rows[key]["kind"], rows[key]["human_verdict"], rows[key]["reason"], rows[key]["borderline"]) == (
            kind, verdict, reason, borderline)


def test_judge_agreement_perfect_and_degraded(tmp_path: Path) -> None:
    from judge_agreement import cohen_kappa, main, score

    rows = _cal()
    perfect = {r["id"]: ("CORRECT" if r["human_verdict"] == "CORRECT" else "WRONG") for r in rows}
    res = score(rows, perfect)
    assert res["all"]["agreement"] == 1.0 and res["all"]["kappa"] == 1.0
    assert res["all"]["false_negatives"] == 0 and res["all"]["false_positives"] == 0
    assert res["all"]["partial_credit_rate"] == 0.0 and res["all"]["refusal_credit_rate"] == 0.0
    # a judge that credits every refusal and partial list
    lenient = {r["id"]: ("WRONG" if r["human_verdict"] == "WRONG" and r["kind"] == "wrong_plausible" else "CORRECT") for r in rows}
    res = score(rows, lenient)
    assert res["all"]["refusal_credit_rate"] == 1.0 and res["all"]["partial_credit_rate"] == 1.0
    assert res["all"]["false_positives"] >= 8
    labels = tmp_path / "labels.jsonl"
    labels.write_text("\n".join(json.dumps({"id": i, "label": v}) for i, v in perfect.items()), encoding="utf-8")
    assert main(["--labels", str(labels), "--json", "--fail-below", "0.9"]) == 0
    labels.write_text("\n".join(json.dumps({"id": i, "label": "CORRECT"}) for i in perfect), encoding="utf-8")
    assert main(["--labels", str(labels), "--fail-below", "0.9"]) == 1
    assert cohen_kappa([]) is None and cohen_kappa([(True, True), (True, True)]) is None


def test_judge_agreement_run_matches_only_identical_answers(tmp_path: Path) -> None:
    from judge_agreement import main

    rows = _cal()
    run = tmp_path / "r--memspine" / "report"
    run.mkdir(parents=True)
    lines = []
    for i, r in enumerate(rows):
        answer = r["answer"] if i % 2 == 0 else r["answer"] + " (different)"
        lines.append(json.dumps({"item": r["item"], "qid": r["qid"], "answer": answer,
                                 "correct": r["human_verdict"] == "CORRECT"}))
    (run / "per_question.jsonl").write_text("\n".join(lines), encoding="utf-8")
    assert main(["--run", "r", "--runs-dir", str(tmp_path), "--json"]) == 0


# ---- C9 paired comparison -----------------------------------------------------------------------------
def test_paired_regressions_compare() -> None:
    from paired_regressions import compare

    def row(item: str, qid: str, correct: bool, answer: str, cat: str = "single-hop") -> dict[str, Any]:
        return {"item": item, "qid": qid, "category": cat, "question": "What did Ana buy?", "gold_answer": "tea",
                "answer": answer, "correct": correct, "context_text": ["a", "b"], "gold_turns": [{"in_context": True}]}

    base = {("conv-26", "0-1"): row("conv-26", "0-1", True, "She bought some loose leaf tea at the market."),
            ("conv-26", "0-2"): row("conv-26", "0-2", False, "x")}
    fixed = {("conv-26", "0-1"): row("conv-26", "0-1", False, "Not mentioned."),
             ("conv-26", "0-2"): row("conv-26", "0-2", True, "tea")}
    res = compare(base, fixed)
    assert res["n"] == 2 and len(res["lost"]) == 1 and len(res["gained"]) == 1
    assert res["lost"][0]["fixed_refusal"] is True and res["lost"][0]["len_ratio"] < 0.5
