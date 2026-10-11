"""Explicit metrics: local token F1, optional official evaluator, separate judge."""
from __future__ import annotations

import importlib.util
import re
import string
from collections import Counter
from pathlib import Path
from statistics import mean


def normalize(text: str) -> str:
    text = text.lower().translate(str.maketrans("", "", string.punctuation))
    return " ".join(re.sub(r"\b(a|an|the)\b", " ", text).split())


def token_f1(prediction: str, reference: str) -> float:
    """Unstemmed local diagnostic, deliberately not labelled official LoCoMo F1."""
    p, r = normalize(prediction).split(), normalize(reference).split()
    overlap = sum((Counter(p) & Counter(r)).values())
    return 2 * overlap / (len(p) + len(r)) if overlap else 0.0


def is_abstention(prediction: str) -> bool:
    """Accept complete abstentions, never a matching fragment plus claimed facts."""
    return normalize(prediction) in {
        "not mentioned in conversation",
        "no information available",
        "no information found",
        "no relevant information available",
        "no relevant information found",
    }


def load_official_evaluator(path: Path):
    """Load a user-provided upstream evaluation.py, without modifying/vendorizing it."""
    spec = importlib.util.spec_from_file_location("locomo_official_evaluation", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Cannot load evaluator: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.eval_question_answering


def score_rows(rows: list[dict], official=None) -> dict:
    if not rows:
        raise ValueError("No prediction rows to score")
    keys = [(r["sample_id"], r["question_index"], r["profile"], r["memory_type"]) for r in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate prediction rows would distort the denominator")
    for row in rows:
        row["token_f1"] = 0.0 if row.get("error") else token_f1(row["prediction"], row["reference"])
        if row["category"] == 5:
            row["abstention_pass"] = int(not row.get("error") and is_abstention(row["prediction"]))
    if official:
        upstream = [dict(answer=r["reference"], prediction=r["prediction"] if not r.get("error") else "",
                         category=r["category"], evidence=list(r.get("evidence", []))) for r in rows]
        scores, _, _ = official(upstream)
        if len(scores) != len(rows):
            raise ValueError("Official evaluator returned the wrong number of scores")
        for row, score in zip(rows, scores):
            row["official_locomo_score"] = float(score)
    groups = {}
    for profile, mode in sorted({(r["profile"], r["memory_type"]) for r in rows}):
        selected = [r for r in rows if r["profile"] == profile and r["memory_type"] == mode]
        answerable = [r for r in selected if r["category"] != 5]
        adversarial = [r for r in selected if r["category"] == 5]
        group = {
            "count": len(selected), "answerable_count": len(answerable),
            "error_count": sum(bool(r.get("error")) for r in selected),
            "empty_context_count": sum(not any(r.get("contexts", {}).values()) for r in selected),
            "token_f1_categories_1_4_pct": 100 * mean(r["token_f1"] for r in answerable) if answerable else None,
            "adversarial_abstention_pct": 100 * mean(r["abstention_pass"] for r in adversarial) if adversarial else None,
            "mean_retrieval_ms": mean(r.get("retrieval_ms", 0) for r in selected),
            "mean_answer_ms": mean(r.get("answer_ms", 0) for r in selected),
            "by_category": {},
        }
        for category in sorted({r["category"] for r in selected}):
            category_rows = [r for r in selected if r["category"] == category]
            stats = {"count": len(category_rows), "token_f1_pct": 100 * mean(r["token_f1"] for r in category_rows)}
            if official:
                stats["official_locomo_score_pct"] = 100 * mean(r["official_locomo_score"] for r in category_rows)
            group["by_category"][str(category)] = stats
        if official:
            group["official_locomo_categories_1_4_pct"] = 100 * mean(r["official_locomo_score"] for r in answerable) if answerable else None
        judged = [r for r in selected if r.get("judge_score") is not None]
        group["judge_count"] = len(judged)
        group["judge_error_count"] = sum(bool(r.get("judge_error")) for r in selected)
        group["strict_judge_pass_pct"] = 100 * sum(r.get("judge_score", 0) or 0 for r in selected) / len(selected) if judged else None
        groups[f"{profile}/{mode}"] = group
    return groups
