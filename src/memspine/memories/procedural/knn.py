"""W17f / N29 (plan v3.2, ADR-060): kNN label vote over stored exemplars.

Exemplars are ``(text, label)`` pairs. A classification-shaped query retrieves the
``k`` nearest by BM25 and (when vectors are given) dense cosine, fused by
reciprocal rank, and the labels vote weighted by the fused score. The result
carries the label, its margin over the runner-up, the voting exemplars and a
label-to-meaning table (N29): per label, its most distinctive terms by tf-idf over
its own exemplars ("label 3 ~ {how, why, mean}"). Pure functions, no model.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from memspine.config import constants
from memspine.core.query_shape import content_words

__all__ = ["Exemplar", "VoteResult", "bm25_scores", "knn_vote", "label_table", "render_table"]

_BM25_K1 = 1.2
_BM25_B = 0.75


@dataclass(frozen=True)
class Exemplar:
    text: str
    label: str
    record_id: str | None = None


@dataclass
class VoteResult:
    label: str | None
    margin: float
    votes: dict[str, float] = field(default_factory=dict)
    exemplars: list[tuple[Exemplar, float]] = field(default_factory=list)
    label_table: dict[str, list[str]] = field(default_factory=dict)


_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    """The content words of ``text`` in order, repeats kept (term frequency)."""
    keep = content_words(text)
    return [w for w in _WORD.findall(text.lower()) if w in keep]


def bm25_scores(query: str, docs: Sequence[str]) -> list[float]:
    """Okapi BM25 of ``query`` against each of ``docs`` (content words only)."""
    tokenised = [_tokens(doc) for doc in docs]
    if not tokenised:
        return []
    avg = sum(len(t) for t in tokenised) / len(tokenised) or 1.0
    df: Counter[str] = Counter()
    for tokens in tokenised:
        df.update(set(tokens))
    n = len(tokenised)
    scores: list[float] = []
    terms = list(content_words(query))
    for tokens in tokenised:
        tf = Counter(tokens)
        score = 0.0
        for term in terms:
            if term not in tf:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            freq = tf[term]
            score += (
                idf
                * freq
                * (_BM25_K1 + 1)
                / (freq + _BM25_K1 * (1 - _BM25_B + _BM25_B * len(tokens) / avg))
            )
        scores.append(score)
    return scores


def _ranks(scores: Sequence[float]) -> list[int | None]:
    """1-based rank of each score (None for a zero score: not retrieved)."""
    order = sorted(range(len(scores)), key=lambda i: (-scores[i], i))
    ranks: list[int | None] = [None] * len(scores)
    for rank, index in enumerate(order, start=1):
        if scores[index] > 0:
            ranks[index] = rank
    return ranks


def knn_vote(
    query: str,
    exemplars: Sequence[Exemplar],
    dense: Sequence[float] | None = None,
    k: int = constants.KNN_VOTE_K,
    terms: int = constants.KNN_LABEL_TERMS,
) -> VoteResult:
    """The score-weighted label vote of the ``k`` exemplars nearest ``query``.

    ``dense`` (optional) is each exemplar's cosine to the query; it is fused with
    BM25 by reciprocal rank (``KNN_RRF_K``). No exemplar, or none matching on
    either leg, gives ``label=None``."""
    table = label_table(exemplars, terms=terms)
    if not exemplars:
        return VoteResult(label=None, margin=0.0, label_table=table)
    legs = [_ranks(bm25_scores(query, [e.text for e in exemplars]))]
    if dense is not None:
        legs.append(_ranks(list(dense)))
    fused: list[float] = []
    for i in range(len(exemplars)):
        ranks = [rank for leg in legs if (rank := leg[i]) is not None]
        fused.append(sum(1.0 / (constants.KNN_RRF_K + rank) for rank in ranks))
    order = sorted(range(len(exemplars)), key=lambda i: (-fused[i], i))
    top = [(exemplars[i], fused[i]) for i in order[:k] if fused[i] > 0]
    votes: dict[str, float] = {}
    for exemplar, score in top:
        votes[exemplar.label] = votes.get(exemplar.label, 0.0) + score
    if not votes:
        return VoteResult(label=None, margin=0.0, label_table=table)
    ranked = sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))
    total = sum(votes.values())
    margin = (ranked[0][1] - (ranked[1][1] if len(ranked) > 1 else 0.0)) / total
    return VoteResult(
        label=ranked[0][0], margin=margin, votes=votes, exemplars=top, label_table=table
    )


def label_table(
    exemplars: Sequence[Exemplar], terms: int = constants.KNN_LABEL_TERMS
) -> dict[str, list[str]]:
    """N29: per label, its ``terms`` most distinctive words (tf-idf, one document
    per label = the concatenation of its exemplars), ties broken alphabetically."""
    by_label: dict[str, Counter[str]] = {}
    for exemplar in exemplars:
        by_label.setdefault(exemplar.label, Counter()).update(_tokens(exemplar.text))
    n = len(by_label)
    df: Counter[str] = Counter()
    for counts in by_label.values():
        df.update(set(counts))
    table: dict[str, list[str]] = {}
    for label, counts in sorted(by_label.items()):
        total = sum(counts.values()) or 1
        weighted = {
            word: (count / total) * math.log((1 + n) / (1 + df[word]) + 1)
            for word, count in counts.items()
            if word
        }
        table[label] = [w for w, _ in sorted(weighted.items(), key=lambda kv: (-kv[1], kv[0]))][
            :terms
        ]
    return table


def render_table(table: dict[str, list[str]]) -> str:
    """The label table as text, one ``label <l> ~ {a, b, c}`` line per label."""
    return "\n".join(f"label {label} ≈ {{{', '.join(words)}}}" for label, words in table.items())
