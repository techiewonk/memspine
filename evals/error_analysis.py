"""Defect analysis of a C0-1 QA run: where does each wrong answer come from?

For every scored question, joins the run's evidence with the dataset's gold evidence turns
and classifies the outcome:

- ``retrieval_miss``: no gold evidence turn reached the context (a deposit/retrieval gap);
- ``partial``: some gold evidence reached the context, not all (multi-evidence questions);
- ``read_fail``: all gold evidence was in context and the answer was still wrong (reader /
  composition / temporal-resolution gap);
- ``correct``.

With the run's ``trace.jsonl`` present it also re-scores retrieval offline under the r3
protocol (R3-7), with no model calls and no re-judging:

- **unit-ranked R@k / R_all@k**: ``k`` counts ranked units (a chunk is one unit), not
  expanded turn ids; ``R_all@k`` needs every gold turn in the top ``k`` units;
- **evidence dropped after truncation**: the context is rebuilt from the trace's ranked
  evidence and the dataset's turns (verified against the trace's context SHA-256), and gold
  evidence whose line lies past the budget cut no longer counts as "in context";
- **tail duplicates** (naive RAG only): ranked units that repeat an earlier unit's turns,
  the pre-R3-2 bug that re-indexed the pending tail chunk on every query.

Rows outside ``--categories`` are dropped (default 1-4: runs before R3-1 graded LoCoMo cat 5
against the adversarial distractor, so their cat-5 verdicts are unusable).

Usage (from memspine/evals):
    python error_analysis.py --data data/locomo10.json \
        --run ../.venv/runs/c0-locomo-qa-full--memspine
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from memspine_evals.contracts import Turn
from memspine_evals.datasets import LoCoMoDataset
from memspine_evals.judge import recall_over_units
from memspine_evals.systems.baselines import _format
from memspine_evals.tokens import HeuristicTokenCounter, TokenCounter, truncate_to_budget

OUTCOMES = ("correct", "retrieval_miss", "partial", "read_fail")


def load_rows(run: Path) -> list[dict]:
    rows = []
    for line in (run / "results.jsonl").read_text("utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("kind") == "result" and row.get("status") in ("completed", "truncated"):
            rows.append(row)
    return rows


def load_manifest(run: Path) -> dict[str, Any]:
    with (run / "results.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            if row.get("kind") == "manifest":
                return row
    return {}


def load_cycles(run: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """``trace.jsonl`` cycle entries keyed by ``(item_id, query_id)``; empty if absent."""
    path = run / "trace.jsonl"
    if not path.exists():
        return {}
    cycles = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            entry = json.loads(line)
            if entry.get("kind") == "cycle":
                cycles[(entry["item_id"], entry["query_id"])] = entry
    return cycles


def _ids(raw: Any) -> list[str]:
    if not raw:
        return []
    return list(raw if isinstance(raw, list) else ast.literal_eval(raw))


def classify(row: dict, gold_ids: tuple[str, ...], in_context: Iterable[str] | None = None) -> str:
    """Outcome of one row. ``in_context`` overrides the row's ``retrieved_ids`` (e.g. with
    the evidence still visible after truncation)."""
    correct = float(row["score"]) >= 1.0
    retrieved = set(_ids(row.get("retrieved_ids")) if in_context is None else in_context)
    hit = [g for g in gold_ids if g in retrieved]
    if correct:
        return "correct"
    if not gold_ids:
        return "no_gold"
    if not hit:
        return "retrieval_miss"
    if len(hit) < len(gold_ids):
        return "partial"
    return "read_fail"


def rebuild_units(
    evidence: Sequence[Mapping[str, Any]], position: Mapping[str, int], chunked: bool
) -> list[list[str]]:
    """Ranked units from a trace's ``E_t`` (turn ids in context order, with scores).

    Turn-level arms: each evidence row is one unit. Chunked arms (naive RAG): a chunk is a
    run of consecutive history turns sharing one score, so a new unit starts when the score
    changes, the turn is not the history successor of the previous one, or the turn is
    already in the current unit (chunks overlap by one turn, and a re-indexed tail repeats).
    """
    units: list[list[str]] = []
    prev: tuple[str, float] | None = None
    for row in evidence:
        tid, score = str(row["turn_id"]), float(row["score"])
        joins = (
            chunked
            and units
            and prev is not None
            and score == prev[1]
            and tid not in units[-1]
            and position.get(tid, -2) == position.get(prev[0], -9) + 1
        )
        if joins:
            units[-1].append(tid)
        else:
            units.append([tid])
        prev = (tid, score)
    return units


def rebuild_context(
    units: Sequence[Sequence[str]],
    turns: Mapping[str, Turn],
    budget_tokens: int,
    counter: TokenCounter,
) -> tuple[str, list[tuple[str, int, int]], bool]:
    """Re-render a baseline context (``_pack`` / full-context layout) from ranked units.

    Returns ``(text, spans, cut)``: ``spans`` are ``(turn_id, start, end)`` character spans
    in the uncut text, and ``cut`` says whether the budget truncated the tail.
    """
    lines: list[str] = []
    spans: list[tuple[str, int, int]] = []
    offset = 0
    for unit in units:
        for tid in unit:
            line = _format(turns[tid])
            spans.append((tid, offset, offset + len(line)))
            lines.append(line)
            offset += len(line) + 1
    body = "\n".join(lines)
    text, _, cut = truncate_to_budget(body, budget_tokens, counter) if body else ("", 0, False)
    return text, spans, cut


def unit_recall(units: Sequence[Sequence[str]], gold: tuple[str, ...], ks: Sequence[int]) -> dict:
    """R@k (any gold turn) and R_all@k (every gold turn) over the top ``k`` units,
    each unit deduplicated against the turns of earlier units (``judge.unit_ranking``)."""
    seen: set[str] = set()
    ranked: list[tuple[str, ...]] = []
    for unit in units:
        fresh = tuple(t for t in dict.fromkeys(unit) if t not in seen)
        seen.update(fresh)
        if fresh:
            ranked.append(fresh)
    out: dict[str, float | None] = {}
    for k in ks:
        out[f"R@{k}"] = recall_over_units(ranked, gold, k)
        out[f"R_all@{k}"] = recall_over_units(ranked, gold, k, require_all=True)
    return out


def tail_duplicates(units: Sequence[Sequence[str]], k: int) -> int:
    """Units in the top ``k`` whose turn set repeats an earlier unit's exactly."""
    seen: set[frozenset[str]] = set()
    dup = 0
    for unit in units[:k]:
        key = frozenset(unit)
        dup += key in seen
        seen.add(key)
    return dup


def arm_kind(manifest: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> str:
    """``chunked`` | ``turn`` (ranked arms), ``replay`` (full-context), ``unranked``."""
    system = manifest.get("system") or {}
    config = system.get("config") or {}
    unit = str(config.get("unit") or "")
    if unit.startswith("chunk"):
        return "chunked"
    if unit == "turn":
        return "turn"
    if system.get("system_id") == "full-context":
        return "replay"
    ranked = any(
        v is not None
        for r in rows
        for v in (
            ast.literal_eval(r["recall"])
            if isinstance(r.get("recall"), str)
            else (r.get("recall") or {})
        ).values()
    )
    return "turn" if ranked else "unranked"


def reanalyse(
    run: Path,
    ds: LoCoMoDataset,
    categories: Sequence[int] = (1, 2, 3, 4),
    ks: Sequence[int] = (1, 5, 10),
    counter: TokenCounter | None = None,
) -> dict[str, Any]:
    """Offline r3 re-scoring of one LoCoMo QA run (no model calls, verdicts as recorded)."""
    counter = counter or HeuristicTokenCounter()
    manifest = load_manifest(run)
    budget = int((manifest.get("protocol") or {}).get("budget_tokens") or 4096)
    rows = load_rows(run)
    cycles = load_cycles(run)
    kind = arm_kind(manifest, rows)
    labels = {f"cat{c}" for c in categories}
    gold: dict[tuple[str, str], tuple[str, ...]] = {}
    turns: dict[str, dict[str, Turn]] = {}
    position: dict[str, dict[str, int]] = {}
    for item in ds.items():
        turns[item.item_id] = {t.turn_id: t for t in item.history}
        position[item.item_id] = {t.turn_id: i for i, t in enumerate(item.history)}
        for q in item.queries:
            gold[(item.item_id, q.query_id)] = tuple(q.gold_turn_ids)

    kept = [r for r in rows if r.get("type_label") in labels]
    acc: dict[str, list[float]] = defaultdict(list)
    outcome_old: dict[str, Counter] = defaultdict(Counter)
    outcome_new: dict[str, Counter] = defaultdict(Counter)
    rec_old: dict[str, list[float]] = defaultdict(list)
    rec_new: dict[str, list[float]] = defaultdict(list)
    n_hash = n_hash_ok = n_cut = n_dropped = n_dup_rows = n_dup_units = 0
    n_any_old = n_any_new = n_all_old = n_all_new = 0
    for row in kept:
        key = (row["item_id"], row["query_id"])
        g = gold.get(key, ())
        cat = row["type_label"]
        acc[cat].append(float(row["score"]))
        acc["ALL"].append(float(row["score"]))
        old_ids = _ids(row.get("retrieved_ids"))
        o = classify(row, g)
        outcome_old[cat][o] += 1
        outcome_old["ALL"][o] += 1
        old = (
            ast.literal_eval(row["recall"])
            if isinstance(row.get("recall"), str)
            else (row.get("recall") or {})
        )
        for name, value in old.items():
            if value is not None:
                rec_old[name].append(float(value))
        visible = set(old_ids)
        cycle = cycles.get(key)
        if cycle is not None and kind != "unranked":
            units = rebuild_units(cycle["E_t"], position[row["item_id"]], kind == "chunked")
            text, spans, cut = rebuild_context(units, turns[row["item_id"]], budget, counter)
            n_hash += 1
            n_hash_ok += hashlib.sha256(text.encode()).hexdigest() == cycle["M_ctx_t"]["sha256"]
            n_cut += cut
            if cut:
                visible = {tid for tid, _, end in spans if end <= len(text)}
            if kind in ("turn", "chunked") and g:
                for name, value in unit_recall(units, g, ks).items():
                    if value is not None:
                        rec_new[name].append(value)
            if kind == "chunked":
                dup = tail_duplicates(units, max(ks))
                n_dup_rows += dup > 0
                n_dup_units += dup
        if g:
            gs = set(g)
            n_any_old += bool(gs & set(old_ids))
            n_all_old += gs <= set(old_ids)
            n_any_new += bool(gs & visible)
            n_all_new += gs <= visible
            n_dropped += bool(gs & set(old_ids)) and not (gs & visible)
        o2 = classify(row, g, visible)
        outcome_new[cat][o2] += 1
        outcome_new["ALL"][o2] += 1

    n_gold = sum(1 for r in kept if gold.get((r["item_id"], r["query_id"])))
    mean = lambda xs: sum(xs) / len(xs) if xs else None  # noqa: E731
    return {
        "run": run.name,
        "kind": kind,
        "budget_tokens": budget,
        "n_rows_all": len(rows),
        "n_rows": len(kept),
        "accuracy": {c: (mean(v), len(v)) for c, v in sorted(acc.items())},
        "outcomes_old": {c: dict(v) for c, v in sorted(outcome_old.items())},
        "outcomes_new": {c: dict(v) for c, v in sorted(outcome_new.items())},
        "recall_turn_level_old": {k: mean(v) for k, v in sorted(rec_old.items())},
        "recall_unit_level": {k: mean(v) for k, v in sorted(rec_new.items())},
        "n_gold": n_gold,
        "gold_in_context_any_old": n_any_old,
        "gold_in_context_any_new": n_any_new,
        "gold_in_context_all_old": n_all_old,
        "gold_in_context_all_new": n_all_new,
        "evidence_dropped_rows": n_dropped,
        "contexts_rebuilt": n_hash,
        "contexts_hash_verified": n_hash_ok,
        "contexts_cut": n_cut,
        "tail_dup_rows": n_dup_rows,
        "tail_dup_units": n_dup_units,
    }


def _print_report(r: Mapping[str, Any]) -> None:
    print(f"\n== {r['run']}  kind={r['kind']}  rows {r['n_rows']} of {r['n_rows_all']}")
    head = ("category", "n", "acc", *OUTCOMES)
    print(f"{head[0]:10s} {head[1]:>5s} {head[2]:>7s} " + " ".join(f"{h:>14s}" for h in OUTCOMES))
    for cat, (a, n) in r["accuracy"].items():
        c = r["outcomes_new"].get(cat, {})
        cells = " ".join(f"{100 * c.get(k, 0) / n:13.1f}%" for k in OUTCOMES)
        print(f"{cat:10s} {n:5d} {100 * a:6.1f}% {cells}")
    if r["recall_turn_level_old"]:
        old = ", ".join(f"{k} {100 * v:.1f}" for k, v in r["recall_turn_level_old"].items())
        print(f"recall as recorded (turn-level k): {old}")
    if r["recall_unit_level"]:
        new = ", ".join(f"{k} {100 * v:.1f}" for k, v in r["recall_unit_level"].items())
        print(f"recall unit-ranked (r3): {new}")
    n = r["n_gold"] or 1
    print(
        f"gold in context (any/all): recorded {r['gold_in_context_any_old']}/"
        f"{r['gold_in_context_all_old']}, after truncation {r['gold_in_context_any_new']}/"
        f"{r['gold_in_context_all_new']} of {r['n_gold']} "
        f"({100 * r['gold_in_context_any_new'] / n:.1f}% / "
        f"{100 * r['gold_in_context_all_new'] / n:.1f}%); rows whose only gold evidence "
        f"was cut: {r['evidence_dropped_rows']}"
    )
    print(
        f"contexts rebuilt {r['contexts_rebuilt']}, SHA-256 verified "
        f"{r['contexts_hash_verified']}, cut by budget {r['contexts_cut']}"
    )
    if r["kind"] == "chunked":
        print(
            f"tail duplicates in top-10 units: {r['tail_dup_rows']} rows, "
            f"{r['tail_dup_units']} duplicate units"
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, action="append", help="run dir (repeatable)")
    ap.add_argument("--data", required=True)
    ap.add_argument(
        "--categories", default="1,2,3,4", help="LoCoMo categories to keep (default 1-4)"
    )
    ap.add_argument("--json", default=None, help="also write the reports to this JSON file")
    args = ap.parse_args()
    ds = LoCoMoDataset(args.data, revision_id="auto")
    cats = tuple(int(c) for c in args.categories.split(","))
    reports = [reanalyse(Path(run), ds, categories=cats) for run in args.run]
    for report in reports:
        _print_report(report)
    if args.json:
        Path(args.json).write_text(json.dumps(reports, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
