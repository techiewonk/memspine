"""R14-M-1 (``read.list_mode_trigger`` / ``read.coverage_select``): list depth and coverage.

Two pure, model-free helpers for list-mode reads:

* :func:`list_signal` decides whether a question needs list depth from its SHAPE (the
  ``intent`` trigger plus weaker English cues) and the SCORE PROFILE of the first vector
  leg (flat top scores, a small gap between the first two hits). The score is a fixed
  logistic form (weights below), so the decision is deterministic and costs no model call.
  The weights were fitted offline on a reference full run's retrieval forensics (question
  shape + vector-leg scores against the number of gold turns); ``evals/analysis/
  r14_listcov_trigger_check.py`` reproduces the recall / false-fire numbers from those
  forensics (ids and counts only) and cross-validates the fit by conversation.
* :func:`coverage_order` reorders a ranked hit list greedily so near-duplicates and
  same-session hits stop crowding the head: each pick is the best remaining hit after a
  penalty per already-picked hit of the same session (``group_id``). The list keeps its
  length, so the downstream budget is unchanged.

The English cues are guarded like the other shape rules (``@english_only``).
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from memspine.core.language import english_only
from memspine.models.encoders.query.shape import is_intent_list, is_set_question

__all__ = [
    "COVERAGE_DEFAULT_PENALTY",
    "TRIGGER_THRESHOLD",
    "ListSignal",
    "coverage_order",
    "list_signal",
    "profile_features",
    "shape_features",
]

_WH = re.compile(r"^\s*(?:what|which|who|where|how)\b", re.I)
#: Perfect aspect ("What has X cooked?", "Where has X camped?"): repeated events.
_PERFECT = re.compile(r"\b(?:has|have|had)\s+(?:\w+\s+){0,3}?\w+(?:ed|en|ne|wn|ht|nt|t)\b", re.I)
#: A plural noun before an auxiliary / preposition / the end ("What events has ...").
_PLURAL = re.compile(
    r"\b(?:\w{3,}(?<!s)s|children|people|men|women)\b"
    r"(?=\s+(?:does|do|did|has|have|is|are|was|were|would|could|might|that|which|and|or"
    r"|in|on|at|for|to|of)\b|\s*\?)",
    re.I,
)
#: Open-ended inference wording ("What might X do ...", "What traits ...").
_MODAL = re.compile(
    r"\b(?:might|could|would|potentially|likely|traits|attributes|describe)\b", re.I
)

#: Fixed logistic weights (raw feature scale) and bias; see the module docstring.
_WEIGHTS: dict[str, float] = {
    "intent": 1.54,
    "wh": 0.89,
    "perfect": 1.36,
    "plural": 0.91,
    "modal": 2.11,
    "set_question": -0.22,
    "log_n90": 0.04,
    "r10": 3.42,
    "log_qlen": -1.87,
    "gap": -2.73,
}
_BIAS = -0.59
#: Score at or above which list depth fires (about a 13% false-fire rate on the fit data).
TRIGGER_THRESHOLD = 0.28
#: Default session penalty of :func:`coverage_order` (a fraction of the normalised relevance).
COVERAGE_DEFAULT_PENALTY = 0.15


@dataclass(frozen=True)
class ListSignal:
    """The decision and its parts (for the wiretap and the forensics)."""

    fire: bool
    score: float
    features: dict[str, float]


@english_only()
def _cue(text: str, pattern: re.Pattern[str]) -> bool:
    return bool(pattern.search(text))


def shape_features(query: str) -> dict[str, float]:
    """The question-shape features (0/1) and the word count."""
    return {
        "intent": float(is_intent_list(query)),
        "wh": float(_cue(query, _WH)),
        "perfect": float(_cue(query, _PERFECT)),
        "plural": float(_cue(query, _PLURAL)),
        "modal": float(_cue(query, _MODAL)),
        "set_question": float(is_set_question(query)),
        "log_qlen": math.log(max(1, len(query.split()))),
    }


def profile_features(scores: Sequence[float]) -> dict[str, float]:
    """Score-profile features of a descending vector leg: ``r10`` (10th score over the best:
    near 1 = flat, many comparable hits), ``gap`` (relative gap between the first two) and
    ``log_n90`` (log of 1 + the hits within 10% of the best). Missing depth uses the last."""
    s = [float(x) for x in scores if x is not None]
    if not s or s[0] <= 0.0:
        return {"r10": 0.0, "gap": 0.0, "log_n90": 0.0}
    top = s[0]
    tenth = s[9] if len(s) > 9 else s[-1]
    gap = (s[0] - s[1]) / top if len(s) > 1 else 0.0
    n90 = sum(1 for x in s if x >= 0.9 * top)
    return {"r10": tenth / top, "gap": gap, "log_n90": math.log1p(n90)}


def list_signal(
    query: str, scores: Sequence[float], threshold: float = TRIGGER_THRESHOLD
) -> ListSignal:
    """Decide list depth from the question shape and the vector-leg score profile.

    ``scores`` are the first vector leg's scores, best first (about 30 deep is enough).
    No model call; an empty profile contributes zero weight to the profile features."""
    feats = {**shape_features(query), **profile_features(scores)}
    z = _BIAS + sum(_WEIGHTS[k] * feats.get(k, 0.0) for k in _WEIGHTS)
    return ListSignal(fire=z >= threshold, score=z, features=feats)


def coverage_order[T](
    items: Sequence[T],
    scores: Sequence[float],
    groups: Sequence[str | None],
    *,
    penalty: float = COVERAGE_DEFAULT_PENALTY,
    keys: Sequence[str | None] | None = None,
) -> list[int]:
    """Greedy MMR-like order over ``items`` (given best first): the index of the next pick
    maximises ``relevance - penalty * (picked hits already in its group)``, relevance being
    the min-max normalised score. A hit whose ``key`` (e.g. normalised content) equals a
    picked one is deferred to the tail. Returns a permutation of ``range(len(items))``;
    ties break by the original rank. ``penalty == 0`` returns the identity order."""
    n = len(items)
    if n < 3 or penalty <= 0.0:
        return list(range(n))
    lo, hi = min(scores), max(scores)
    span = (hi - lo) or 1.0
    rel = [(s - lo) / span for s in scores]
    remaining = list(range(n))
    taken: dict[str, int] = {}
    seen_keys: set[str] = set()
    order: list[int] = []
    dupes: list[int] = []
    while remaining:
        best = max(
            remaining,
            key=lambda i: (rel[i] - penalty * taken.get(groups[i] or "", 0), -i),
        )
        remaining.remove(best)
        key = keys[best] if keys is not None else None
        if key and key in seen_keys:
            dupes.append(best)
            continue
        if key:
            seen_keys.add(key)
        order.append(best)
        if groups[best]:
            taken[groups[best] or ""] = taken.get(groups[best] or "", 0) + 1
    return order + dupes
