"""Official LoCoMo QA F1 (G-C7): a second score beside the judge accuracy, computed offline from the saved answers.

This reimplements, from its behaviour, the scoring that snap-research/locomo ships in
``task_eval/evaluation.py`` (``normalize_answer``, ``f1_score``, ``f1`` and ``eval_question_answering``), the
function the combined-engine bundle delegates to. LICENCE NOTE: that repository's ``LICENSE.txt`` is Creative
Commons Attribution-NonCommercial 4.0 (GitHub reports "other"; it is NOT MIT), so the upstream file is neither
copied nor downloaded at run time: this module is an independent port of the documented algorithm, with
attribution (Maharana et al., "Evaluating Very Long-Term Conversational Memory of LLM Agents", ACL 2024). A number
from it is a research comparison figure; keep the NC terms in mind before any commercial use of the upstream code.

Rules, as upstream (checked against the upstream source on 2026-10-11):
  * normalise: drop every comma, lowercase, strip ASCII punctuation, drop the tokens ``a an the and`` (the upstream
    regex includes "and"), collapse whitespace;
  * tokens are Porter-stemmed (``nltk.stem.PorterStemmer``, default mode); F1 is the bag-of-words F1 (precision and
    recall over the multiset overlap), 0 when there is no overlap;
  * category 1 (multi-hop): split prediction AND gold on ',' and strip; the score is the mean over the gold
    sub-answers of the best token-F1 against any prediction sub-answer;
  * categories 2, 3, 4: plain token F1; category 3 first cuts the gold at the first ';' (``answer.split(';')[0]``);
  * category 5 (adversarial): 1 if the prediction contains "no information available" or "not mentioned"
    (case-insensitive), else 0;
  * the gold is ``str()``-ed.
Headline F1 is over categories 1-4 (the upstream convention; category 5 is an abstention check).

``nltk`` is needed only for the stemmer; it is imported lazily so importing this module never fails.
"""

from __future__ import annotations

import re
import string
from collections import Counter
from functools import lru_cache

_PUNCT = frozenset(string.punctuation)
_ARTICLES = re.compile(r"\b(a|an|the|and)\b")

ABSTAIN_MARKERS = ("no information available", "not mentioned")
CAT_NAMES = {1: "multi-hop", 2: "temporal", 3: "open-domain", 4: "single-hop", 5: "adversarial"}


@lru_cache(maxsize=1)
def _stemmer():
    try:
        from nltk.stem import PorterStemmer
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "official LoCoMo F1 needs nltk (PorterStemmer): pip install nltk"
        ) from exc
    return PorterStemmer()


@lru_cache(maxsize=200_000)
def _stem(word: str) -> str:
    return _stemmer().stem(word)


def normalize_answer(s: str) -> str:
    s = s.replace(",", "")
    s = s.lower()
    s = "".join(ch for ch in s if ch not in _PUNCT)
    s = _ARTICLES.sub(" ", s)
    return " ".join(s.split())


def f1_score(prediction: str, ground_truth: str) -> float:
    p = [_stem(w) for w in normalize_answer(prediction).split()]
    g = [_stem(w) for w in normalize_answer(ground_truth).split()]
    same = sum((Counter(p) & Counter(g)).values())
    if same == 0:
        return 0.0
    precision, recall = same / len(p), same / len(g)
    return 2 * precision * recall / (precision + recall)


def f1_multi(prediction: str, ground_truth: str) -> float:
    """Category-1 rule: comma-split both sides; mean over gold parts of the best-matching prediction part."""
    preds = [x.strip() for x in prediction.split(",")]
    golds = [x.strip() for x in ground_truth.split(",")]
    return sum(max(f1_score(p, g) for p in preds) for g in golds) / len(golds)


def locomo_f1(prediction: str | None, gold, category: int) -> float:
    """Official LoCoMo score for one answer (0..1). ``category`` is the LoCoMo category 1-5."""
    pred = prediction or ""
    answer = str(gold)
    if category == 3:
        answer = answer.split(";")[0].strip()
    if category in (2, 3, 4):
        return f1_score(pred, answer)
    if category == 1:
        return f1_multi(pred, answer)
    if category == 5:
        low = pred.lower()
        return 1.0 if any(m in low for m in ABSTAIN_MARKERS) else 0.0
    raise ValueError(f"unknown LoCoMo category {category!r}")


def category_of(type_label: str | None) -> int | None:
    """Result ``type_label`` ('cat1'..'cat5') to the LoCoMo category number."""
    m = re.fullmatch(r"cat([1-5])", type_label or "")
    return int(m.group(1)) if m else None


def row_f1(row: dict) -> float | None:
    """F1 for one results.jsonl ``result`` row, or None when the row is not a scorable LoCoMo QA row.

    Errors and non-completed rows score 0 (they stay in the denominator, as the bundle does)."""
    cat = category_of(row.get("type_label"))
    if cat is None or row.get("gold") is None:
        return None
    if row.get("status") != "completed":
        return 0.0
    return locomo_f1(row.get("answer"), row["gold"], cat)


def summarise(f1_by_category: dict[int, list[float]]) -> dict:
    """Mean F1 (x100) per category and the categories 1-4 headline."""
    out: dict = {"by_category": {}, "n_1_4": 0, "f1_1_4": None}
    pool: list[float] = []
    for c in sorted(f1_by_category):
        v = f1_by_category[c]
        out["by_category"][c] = {"n": len(v), "f1": 100 * sum(v) / len(v) if v else None}
        if c != 5:
            pool += v
    out["n_1_4"] = len(pool)
    out["f1_1_4"] = 100 * sum(pool) / len(pool) if pool else None
    return out
