"""Retrieval-only screening: evidence coverage without a reader or a judge.

A retrieval-only run (``c0-1 --retrieval-only``) ingests the history and calls the
system's read path for every question exactly as the QA run would (same engine
config, budget, ``top_k`` and read mode), then stops: no reader, no judge. Each row
records what reached the context instead:

* ``retrieved_ids``: the turn ids of the evidence left after the budget truncation;
* ``context_tokens``;
* ``meta.gold_evidence``: the dataset's gold evidence turn ids (LoCoMo ``qa[*].evidence``,
  split on ``;`` and normalised to ``D<session>:<turn>``);
* ``meta.ev_all`` (every gold turn is in the context), ``meta.ev_any`` (at least one is)
  and ``meta.ev_frac`` (the fraction that is). ``None`` for a question with no gold
  evidence (LoCoMo cat 5), which is then left out of the coverage means.

Coverage is set-based over the whole context, so it is defined for unranked read
modes (``replay``) too, where R@k is not. It credits a raw turn only: a derived
record (a mined fact, a summary) that carries the evidence but is not itself a turn
is not counted, so coverage under-reads arms whose gain is in derived records.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .contracts import ReaderAnswer
from .judge import JudgeScale, JudgeSpec, Verdict

__all__ = [
    "RETRIEVAL_ONLY_PROTOCOL",
    "SkippedJudge",
    "SkippedReader",
    "coverage",
    "coverage_summary",
    "normalise_evidence",
]

#: ``RunProtocol.protocol_id`` of a retrieval-only run (never confused with QA rows).
RETRIEVAL_ONLY_PROTOCOL = "c0-1-retrieval-only"

_DIA = re.compile(r"D(\d+)\s*:\s*(\d+)")


def normalise_evidence(ids: Iterable[str]) -> tuple[str, ...]:
    """Evidence ids -> unique ``D<s>:<t>`` ids in order (``"D8:6; D9:17"`` gives two,
    ``"D1:05"`` gives ``"D1:5"``); an id of another shape is kept, stripped."""
    out: list[str] = []
    for raw in ids:
        text = str(raw)
        found = _DIA.findall(text)
        if found:
            out.extend(f"D{int(s)}:{int(t)}" for s, t in found)
        else:
            out.extend(part.strip() for part in text.split(";") if part.strip())
    return tuple(dict.fromkeys(out))


def coverage(retrieved: Iterable[str], gold: Iterable[str]) -> dict[str, Any]:
    """``ev_all`` / ``ev_any`` / ``ev_frac`` of ``gold`` in ``retrieved`` (None without gold)."""
    gold_ids = normalise_evidence(gold)
    if not gold_ids:
        return {"ev_all": None, "ev_any": None, "ev_frac": None}
    present = set(normalise_evidence(retrieved))
    found = sum(1 for g in gold_ids if g in present)
    return {
        "ev_all": found == len(gold_ids),
        "ev_any": found > 0,
        "ev_frac": round(found / len(gold_ids), 6),
    }


def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def coverage_summary(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per category (``type_label``) and ``all``: question count, how many have gold
    evidence, mean ``ev_all`` / ``ev_any`` / ``ev_frac`` over those, mean context tokens
    over every completed row. Rows are result dicts (``ResultRow.to_dict()``)."""
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("status") not in ("completed", "truncated"):
            continue
        groups["all"].append(row)
        groups[str(row.get("type_label") or "?")].append(row)
    out: dict[str, dict[str, Any]] = {}
    for label in sorted(groups, key=lambda k: (k != "all", k)):
        members = groups[label]
        meta = [dict(r.get("meta") or {}) for r in members]
        with_gold = [m for m in meta if m.get("ev_all") is not None]
        out[label] = {
            "n": len(members),
            "n_with_evidence": len(with_gold),
            "ev_all": _mean([float(bool(m["ev_all"])) for m in with_gold]),
            "ev_any": _mean([float(bool(m["ev_any"])) for m in with_gold]),
            "ev_frac": _mean([float(m["ev_frac"]) for m in with_gold]),
            "context_tokens": _mean([float(r.get("context_tokens") or 0) for r in members]),
        }
    return out


class SkippedReader:
    """The reader of a retrieval-only run: never called (the runner skips generation).

    Its ``model`` reads ``none``, so the manifest marks the run inadmissible as a QA
    result, as for every retrieval measurement."""

    reader_id = "skipped:retrieval-only"
    model = "none"
    makes_model_calls = False

    def describe(self) -> Mapping[str, Any]:
        return {"reader_id": self.reader_id, "model": self.model, "generation": "skipped"}

    async def answer(
        self, question: str, context: str, question_date: str | None = None
    ) -> ReaderAnswer:
        raise RuntimeError("a retrieval-only run never calls its reader")


class SkippedJudge:
    """The judge of a retrieval-only run: never called. The row ``score`` is ``ev_all``."""

    handles_abstention = True

    def __init__(self) -> None:
        self.spec = JudgeSpec(
            judge_id="skipped:retrieval-only",
            scale=JudgeScale.BINARY,
            params={"score": "ev_all (gold evidence fully in context); 0 without gold"},
        )

    async def score(self, question: str, answer: str, gold: str | None) -> Verdict:
        raise RuntimeError("a retrieval-only run never calls its judge")
