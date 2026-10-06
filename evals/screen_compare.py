"""Paired comparison of two screening arms: evidence coverage and, when both sides have
QA scores, accuracy with an exact sign test.

    python screen_compare.py --base "runs/screen--combo-A--conv-*--memspine" \\
        --arm "runs/screen--combo-A-cards--conv-*--memspine"
    python screen_compare.py --base runs/qa-A--memspine --arm runs/qa-B--memspine \\
        --locomo data/locomo10.json --json out.json

``--base`` / ``--arm`` take run directories (each holding a ``results.jsonl``) or glob
patterns, so a run split one conversation per directory is passed as one pattern.
Rows are keyed by ``(item_id, query_id)`` (LoCoMo's ``query_id`` indexes the FULL
``qa`` list, cat 5 included); when a key appears in several directories, the most
recently modified directory's row wins (a retry replaces the original). Only keys
present on both sides are compared.

Coverage (``ev_all`` / ``ev_any`` / ``ev_frac``) is read from the rows of a
retrieval-only run. For QA rows, which carry ``retrieved_ids`` but no gold evidence,
pass ``--locomo`` and it is recomputed from the dataset's ``qa[*].evidence``.

Accuracy (QA rows only): a question is right when its score is > 0.5; an error or
unattempted row counts as wrong. The sign test is McNemar's exact form: on the
discordant questions, ``won`` (arm right, base wrong) against ``lost``, two-sided
binomial with p = 1/2. The same test is reported for ``ev_all``.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from fractions import Fraction
from pathlib import Path
from typing import Any

Key = tuple[str, str]

_EVALS = Path(__file__).resolve().parent
if str(_EVALS) not in sys.path:
    sys.path.insert(0, str(_EVALS))


def expand(patterns: Iterable[str]) -> list[Path]:
    """Run directories from paths or glob patterns (deduplicated, in mtime order)."""
    found: dict[str, Path] = {}
    for pattern in patterns:
        matches = glob.glob(pattern) or ([pattern] if Path(pattern).exists() else [])
        for match in matches:
            path = Path(match)
            if (path / "results.jsonl").exists():
                found[str(path.resolve())] = path
    return sorted(found.values(), key=lambda p: (p / "results.jsonl").stat().st_mtime)


def load_rows(dirs: Sequence[Path]) -> dict[Key, dict[str, Any]]:
    """Result rows of ``dirs`` keyed by (item_id, query_id); later directories win."""
    rows: dict[Key, dict[str, Any]] = {}
    for run_dir in dirs:
        for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("kind", "result") != "result" or "query_id" not in row:
                continue
            rows[(str(row["item_id"]), str(row["query_id"]))] = row
    return rows


def locomo_gold(path: str | Path) -> dict[Key, tuple[str, ...]]:
    """(item_id, query_id) -> gold evidence turn ids, every category (cat 5: none)."""
    from memspine_evals.datasets.locomo import LoCoMoDataset

    dataset = LoCoMoDataset(path, revision_id="auto", categories=None)
    return {
        (item.item_id, q.query_id): tuple(q.gold_turn_ids)
        for item in dataset.items()
        for q in item.queries
    }


def row_coverage(
    row: Mapping[str, Any], gold: Mapping[Key, tuple[str, ...]] | None, key: Key
) -> dict[str, Any] | None:
    """A row's ``ev_*`` fields: from its meta, else recomputed with ``gold``; None if
    neither (or the question has no gold evidence)."""
    from memspine_evals.screen import coverage

    meta = dict(row.get("meta") or {})
    if "ev_all" in meta:
        return meta if meta["ev_all"] is not None else None
    if gold is None or key not in gold or row.get("status") not in ("completed", "truncated"):
        return None
    cover = coverage(row.get("retrieved_ids") or (), gold[key])
    return cover if cover["ev_all"] is not None else None


def is_qa(row: Mapping[str, Any]) -> bool:
    """A row with an answer score (not a retrieval-only row)."""
    meta = dict(row.get("meta") or {})
    return not meta.get("retrieval_only") and "retrieval-only" not in str(row.get("protocol_id"))


def right(row: Mapping[str, Any]) -> bool:
    if row.get("status") not in ("completed", "truncated") or row.get("score") is None:
        return False
    return float(row["score"]) > 0.5


def sign_test(won: int, lost: int) -> float:
    """Exact two-sided binomial (p = 1/2) on ``won`` vs ``lost`` discordant questions."""
    n = won + lost
    if n == 0:
        return 1.0
    k = min(won, lost)
    tail = sum(math.comb(n, i) for i in range(k + 1))
    return min(1.0, float(Fraction(2 * tail, 1 << n)))


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def compare(
    base: Mapping[Key, Mapping[str, Any]],
    arm: Mapping[Key, Mapping[str, Any]],
    gold: Mapping[Key, tuple[str, ...]] | None = None,
) -> dict[str, Any]:
    """Per category (and ``all``): paired coverage deltas, context tokens, and, when
    every paired row on both sides is a QA row, accuracy deltas with the sign test."""
    keys = sorted(set(base) & set(arm))
    groups: dict[str, list[Key]] = defaultdict(list)
    for key in keys:
        groups["all"].append(key)
        groups[str(arm[key].get("type_label") or base[key].get("type_label") or "?")].append(key)
    qa = bool(keys) and all(is_qa(base[k]) and is_qa(arm[k]) for k in keys)
    out: dict[str, Any] = {
        "n_base": len(base),
        "n_arm": len(arm),
        "n_paired": len(keys),
        "qa": qa,
        "categories": {},
    }
    for label in sorted(groups, key=lambda g: (g != "all", g)):
        members = groups[label]
        cell: dict[str, Any] = {"n": len(members)}
        covered = [
            (cb, ca)
            for k in members
            if (cb := row_coverage(base[k], gold, k)) is not None
            and (ca := row_coverage(arm[k], gold, k)) is not None
        ]
        cell["n_with_evidence"] = len(covered)
        for metric in ("ev_all", "ev_any", "ev_frac"):
            b = _mean([float(cb[metric]) for cb, _ in covered])
            a = _mean([float(ca[metric]) for _, ca in covered])
            cell[metric] = {
                "base": b,
                "arm": a,
                "delta": None if a is None or b is None else a - b,
            }
        ev_won = sum(1 for cb, ca in covered if ca["ev_all"] and not cb["ev_all"])
        ev_lost = sum(1 for cb, ca in covered if cb["ev_all"] and not ca["ev_all"])
        cell["ev_all"].update(won=ev_won, lost=ev_lost, p=sign_test(ev_won, ev_lost))
        ctx_b = _mean([float(base[k].get("context_tokens") or 0) for k in members])
        ctx_a = _mean([float(arm[k].get("context_tokens") or 0) for k in members])
        cell["context_tokens"] = {
            "base": ctx_b,
            "arm": ctx_a,
            "delta": None if ctx_a is None or ctx_b is None else ctx_a - ctx_b,
        }
        if qa:
            rb = [right(base[k]) for k in members]
            ra = [right(arm[k]) for k in members]
            won = sum(1 for b, a in zip(rb, ra, strict=True) if a and not b)
            lost = sum(1 for b, a in zip(rb, ra, strict=True) if b and not a)
            acc_b, acc_a = sum(rb) / len(rb), sum(ra) / len(ra)
            cell["accuracy"] = {
                "base": acc_b,
                "arm": acc_a,
                "delta": acc_a - acc_b,
                "won": won,
                "lost": lost,
                "p": sign_test(won, lost),
            }
        out["categories"][label] = cell
    return out


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{100 * value:.1f}"


def _dpct(value: float | None) -> str:
    return "-" if value is None else f"{100 * value:+.1f}"


def _num(value: float | None, spec: str) -> str:
    return "-" if value is None else format(value, spec)


def render(result: Mapping[str, Any]) -> str:
    lines = [
        f"paired questions: {result['n_paired']} (base {result['n_base']}, arm {result['n_arm']})",
        "",
        "| cat | n | n ev | ev_all base | ev_all arm | Δ | won/lost | p | ev_any Δ | ev_frac Δ "
        "| ctx base | ctx Δ |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for label, cell in result["categories"].items():
        ev = cell["ev_all"]
        ctx = cell["context_tokens"]
        lines.append(
            f"| {label} | {cell['n']} | {cell['n_with_evidence']} | {_pct(ev['base'])} "
            f"| {_pct(ev['arm'])} | {_dpct(ev['delta'])} | {ev['won']}/{ev['lost']} "
            f"| {ev['p']:.3g} | {_dpct(cell['ev_any']['delta'])} "
            f"| {_dpct(cell['ev_frac']['delta'])} "
            f"| {_num(ctx['base'], '.0f')} | {_num(ctx['delta'], '+.0f')} |"
        )
    if result["qa"]:
        lines += [
            "",
            "| cat | n | acc base | acc arm | Δ pp | won | lost | sign-test p |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for label, cell in result["categories"].items():
            acc = cell["accuracy"]
            lines.append(
                f"| {label} | {cell['n']} | {_pct(acc['base'])} | {_pct(acc['arm'])} "
                f"| {_dpct(acc['delta'])} | {acc['won']} | {acc['lost']} | {acc['p']:.3g} |"
            )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", nargs="+", required=True, help="run dirs or glob patterns")
    ap.add_argument("--arm", nargs="+", required=True, help="run dirs or glob patterns")
    ap.add_argument("--locomo", default=None, help="locomo10.json: coverage for QA rows")
    ap.add_argument("--json", default=None, help="also write the result as JSON here")
    args = ap.parse_args(argv)
    base_dirs, arm_dirs = expand(args.base), expand(args.arm)
    if not base_dirs or not arm_dirs:
        missing = "--base" if not base_dirs else "--arm"
        raise SystemExit(f"{missing}: no run directory with a results.jsonl matched")
    gold = locomo_gold(args.locomo) if args.locomo else None
    result = compare(load_rows(base_dirs), load_rows(arm_dirs), gold)
    result["base_dirs"] = [str(d) for d in base_dirs]
    result["arm_dirs"] = [str(d) for d in arm_dirs]
    print(render(result))
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
