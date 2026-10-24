"""R14-M-16/17/18/19: re-score a finished QA run offline (no model call, no retrieval).

    python evals/r14_rescore.py runs/<run>--memspine [--errata analysis/locomo_errata.json]
        [--data data/locomo10.json] [--labels analysis/r14_reader_judge_features.csv]
        [--out runs/_analysis]

For every row of ``results.jsonl`` it prints, per LoCoMo category and overall, five accuracy columns
over the same denominator:

* ``raw``       the official judge verdict (``score``), unchanged;
* ``conventions``        raw OR the deterministic ``judge_conventions`` conventions rules (R14-M-17: list with scoped
                negation, containment with extra detail, city for country, "N years ago" met by a
                year; all supported deterministic rules are included). Only wrong rows can gain;
* ``strict``    conventions AND, for a list gold, every gold item present in the answer (R14-M-18): a gold
                list answered with one item is not credited. Rows whose gold is no list keep conventions;
* ``raw-E`` / ``strict-E``  the same two columns with the errata questions (``gold_error``,
                ``needs_image``) removed from the denominator.

The honest headline band is ``strict`` (low) to ``conventions`` (high) with a cluster-bootstrap CI by
conversation; ``raw`` sits in or above it. ``--data`` adds the R14-M-16 stratum: questions whose
every evidence turn is spoken by the other speaker than the one the question names, reported on
their own so a correct attribution is not mistaken for a miss. ``--labels`` measures precision and
recall of conventions and of the strict column on the hand labels (``hand_fn`` / ``hand_fp``) of
``analysis/r14_reader_judge_features.csv``.

Files carrying question or answer text (the credit and strict-fail listings) go to ``--out``
(default ``<runs>/_analysis``, gitignored); stdout holds counts only.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != HERE.resolve()]
sys.path.append(str(HERE))

from memspine_evals.judge_conventions import (
    check_conventions,
    strict_list_ok,
)

COLUMNS = ("raw", "conventions", "strict", "raw-E", "strict-E")
DEFAULT_ERRATA = HERE / "analysis" / "locomo_errata.json"
ERRATA_TAGS = ("gold_error", "needs_image")


def load_rows(run: Path | str) -> list[dict[str, Any]]:
    path = Path(run)
    path = path / "results.jsonl" if path.is_dir() else path
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    return [r for r in rows if r.get("kind") == "result"]


def load_errata_keys(path: Path | str | None) -> set[tuple[str, str]]:
    if not path or not Path(path).exists():
        return set()
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    out = set()
    for e in payload.get("entries", []):
        if e.get("tag") in ERRATA_TAGS:
            out.add((str(e.get("item", e.get("item_id"))), str(e.get("qid", e.get("query_id")))))
    return out


def is_abstention(row: dict[str, Any]) -> bool:
    return str(row.get("gold") or "").strip().lower().startswith("not mentioned")


def score_row(row: dict[str, Any]) -> dict[str, Any]:
    """The per-row verdicts. ``convention_rule`` is set only for a wrong row the conventions layer credits;
    ``list_missing`` only for a list gold with an absent item."""
    ok = float(row.get("score") or 0) >= 1
    question, answer, gold = row.get("question") or "", row.get("answer") or "", row.get("gold")
    binary = row.get("status") == "completed" and row.get("scale", "binary") == "binary"
    rule = None
    if binary and not ok and not is_abstention(row):
        hit = check_conventions(question, answer, gold)
        rule = hit.rule if hit is not None else None
    conventions = ok or rule is not None
    strict_list = (
        None if (not binary or is_abstention(row)) else strict_list_ok(question, answer, gold)
    )
    strict = conventions and strict_list is not False
    return {
        "raw": ok,
        "conventions": conventions,
        "strict": strict,
        "convention_rule": rule,
        "list_missing": strict_list is False,
    }


def _category(row: dict[str, Any]) -> str:
    return str(row.get("type_label") or "cat?")


def rescore(
    rows: list[dict[str, Any]],
    errata: set[tuple[str, str]],
    stratum: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Counts per category (and ``all`` / ``cat1-4``) for the five columns."""
    scored = []
    for r in rows:
        s = score_row(r)
        s["key"] = (r["item_id"], r["query_id"])
        s["cat"] = _category(r)
        s["errata"] = s["key"] in errata
        s["swapped"] = stratum is not None and s["key"] in stratum
        scored.append(s)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in scored:
        groups[s["cat"]].append(s)
        groups["all"].append(s)
        if s["cat"] in ("cat1", "cat2", "cat3", "cat4"):
            groups["cat1-4"].append(s)
    table: dict[str, dict[str, Any]] = {}
    for name, sub in groups.items():
        kept = [s for s in sub if not s["errata"]]
        table[name] = {
            "n": len(sub),
            "n_errata": len(sub) - len(kept),
            "raw": sum(s["raw"] for s in sub),
            "conventions": sum(s["conventions"] for s in sub),
            "strict": sum(s["strict"] for s in sub),
            "n-E": len(kept),
            "raw-E": sum(s["raw"] for s in kept),
            "strict-E": sum(s["strict"] for s in kept),
        }
    out: dict[str, Any] = {"table": table, "rows": scored}
    if stratum is not None:
        sw = [s for s in scored if s["swapped"]]
        rest = [s for s in scored if not s["swapped"] and s["cat"] != "cat5"]
        out["swapped"] = {
            "n": len(sw),
            "raw": sum(s["raw"] for s in sw),
            "conventions": sum(s["conventions"] for s in sw),
            "strict": sum(s["strict"] for s in sw),
            "rest_n": len(rest),
            "rest_raw": sum(s["raw"] for s in rest),
        }
    return out


# ---------------------------------------------------------------------------------------------
# R14-M-16: the speaker-swap stratum
# ---------------------------------------------------------------------------------------------
_DIA = re.compile(r"^D(\d+):(\d+)$")


def speaker_swapped(question: str, speakers: tuple[str, ...], evidence_speakers: list[str]) -> bool:
    """True when the question names exactly one of the two speakers and every evidence turn is
    spoken by the other one. Pure; ``speakers`` are the conversation's two names."""
    named = [s for s in speakers if re.search(rf"\b{re.escape(s)}(?:'s)?\b", question)]
    if len(named) != 1 or not evidence_speakers:
        return False
    return all(e != named[0] for e in evidence_speakers)


def swapped_keys(data_path: Path | str) -> set[tuple[str, str]]:
    """(item, qid) of every categories 1-4 question of a LoCoMo file whose evidence turns are all
    spoken by the speaker the question does NOT name (strict evidence parser, existence checked)."""
    from memspine_evals.datasets.locomo import parse_evidence_ids

    raw = json.loads(Path(data_path).read_text(encoding="utf-8"))
    out: set[tuple[str, str]] = set()
    for index, sample in enumerate(raw):
        conv = sample.get("conversation", {})
        speakers = tuple(v for k, v in conv.items() if k in ("speaker_a", "speaker_b"))
        by_id: dict[str, str] = {}
        for key, turns in conv.items():
            m = re.match(r"^session_(\d+)$", key)
            if m and isinstance(turns, list):
                for i, t in enumerate(turns, 1):
                    by_id[f"D{m.group(1)}:{i}"] = str(t.get("speaker", ""))
                    if t.get("dia_id"):
                        by_id[str(t["dia_id"])] = str(t.get("speaker", ""))
        for q_index, qa in enumerate(sample.get("qa") or []):
            if qa.get("category") == 5:
                continue
            ids = parse_evidence_ids([str(e) for e in qa.get("evidence") or []], conv)
            spk = [by_id[i] for i in ids if i in by_id]
            if speaker_swapped(str(qa.get("question", "")), speakers, spk):
                out.add((str(sample.get("sample_id", index)), f"{index}-{q_index}"))
    return out


# ---------------------------------------------------------------------------------------------
# hand-label precision / recall
# ---------------------------------------------------------------------------------------------
def label_metrics(scored: list[dict[str, Any]], labels_path: Path | str) -> dict[str, Any]:
    """conventions precision/recall against ``hand_fn`` and the strict column against ``hand_fp``."""
    with open(labels_path, encoding="utf-8") as fh:
        lab = {(r["item"], r["qid"]): r for r in csv.DictReader(fh)}
    credited = [s for s in scored if s["convention_rule"] is not None]
    fn_all = {k for k, r in lab.items() if r["hand_fn"]}
    fn_hard = {k for k, r in lab.items() if r["hand_fn"] in ("clear", "likely")}
    hit_all = [s for s in credited if s["key"] in fn_all]
    hit_hard = [s for s in credited if s["key"] in fn_hard]
    fp_all = {k for k, r in lab.items() if r["hand_fp"]}
    fp_hard = {k for k, r in lab.items() if r["hand_fp"] in ("clear", "likely")}
    failed = [s for s in scored if s["list_missing"] and s["raw"] and s["cat"] != "cat5"]
    f_all = [s for s in failed if s["key"] in fp_all]
    f_hard = [s for s in failed if s["key"] in fp_hard]
    return {
        "fn": {
            "convention_new_credits": len(credited),
            "hand_fn_any": len(fn_all),
            "hand_fn_clear_likely": len(fn_hard),
            "credited_hand_fn_any": len(hit_all),
            "credited_hand_fn_clear_likely": len(hit_hard),
            "credited_not_hand_fn": len(credited) - len(hit_all),
            "precision_any": len(hit_all) / len(credited) if credited else None,
            "recall_clear_likely": len(hit_hard) / len(fn_hard) if fn_hard else None,
            "recall_any": len(hit_all) / len(fn_all) if fn_all else None,
        },
        "fp": {
            "strict_dropped": len(failed),
            "hand_fp_any": len(fp_all),
            "hand_fp_clear_likely": len(fp_hard),
            "dropped_hand_fp_any": len(f_all),
            "dropped_hand_fp_clear_likely": len(f_hard),
            "dropped_not_hand_fp": len(failed) - len(f_all),
            "precision_any": len(f_all) / len(failed) if failed else None,
            "recall_clear_likely": len(f_hard) / len(fp_hard) if fp_hard else None,
            "recall_any": len(f_all) / len(fp_all) if fp_all else None,
        },
    }


# ---------------------------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------------------------
def _pct(k: int, n: int) -> str:
    return f"{100 * k / n:5.1f}" if n else "  n/a"


def band_ci(
    scored: list[dict[str, Any]], col: str, seed: int = 11, resamples: int = 1000
) -> tuple[float, float]:
    from memspine_evals.results import _bootstrap_clusters

    by_item: dict[str, list[float]] = defaultdict(list)
    for s in scored:
        by_item[s["key"][0]].append(1.0 if s[col] else 0.0)
    return _bootstrap_clusters(list(by_item.values()), seed, resamples, 0.05)


def format_table(result: dict[str, Any]) -> str:
    t = result["table"]
    order = [c for c in ("cat1", "cat2", "cat3", "cat4", "cat5", "cat1-4", "all") if c in t]
    lines = [
        f"{'cat':7} {'n':>5} {'raw':>6} {'conventions':>6} {'strict':>6} | {'n-E':>5} {'raw-E':>6} {'strict-E':>8}"
    ]
    for c in order:
        r = t[c]
        lines.append(
            f"{c:7} {r['n']:5d} {_pct(r['raw'], r['n']):>6} {_pct(r['conventions'], r['n']):>6} "
            f"{_pct(r['strict'], r['n']):>6} | {r['n-E']:5d} {_pct(r['raw-E'], r['n-E']):>6} "
            f"{_pct(r['strict-E'], r['n-E']):>8}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("runs", nargs="+", help="run dirs (or results.jsonl files)")
    ap.add_argument("--errata", default=str(DEFAULT_ERRATA))
    ap.add_argument(
        "--data", default=None, help="locomo10.json: adds the speaker-swap stratum (M-16)"
    )
    ap.add_argument(
        "--labels", default=None, help="r14_reader_judge_features.csv: precision/recall"
    )
    ap.add_argument("--out", default=None, help="dir for text-bearing listings (gitignored)")
    ap.add_argument("--json", default=None, help="write the counts-only summary here")
    args = ap.parse_args(argv)
    errata = load_errata_keys(args.errata)
    stratum = swapped_keys(args.data) if args.data else None
    summary: dict[str, Any] = {}
    for run in args.runs:
        rows = load_rows(run)
        name = Path(run).resolve().name if Path(run).is_dir() else Path(run).resolve().parent.name
        res = rescore(rows, errata, stratum)
        print(f"== {name}: {len(rows)} rows, {len(errata)} errata keys")
        print(format_table(res))
        scored = res["rows"]
        qa4 = [s for s in scored if s["cat"] in ("cat1", "cat2", "cat3", "cat4")]
        if qa4:
            lo = band_ci(qa4, "strict")
            hi = band_ci(qa4, "conventions")
            t4 = res["table"]["cat1-4"]
            print(
                f"honest band cat1-4: strict {_pct(t4['strict'], t4['n']).strip()} "
                f"[{100 * lo[0]:.1f}, {100 * lo[1]:.1f}] .. conventions {_pct(t4['conventions'], t4['n']).strip()} "
                f"[{100 * hi[0]:.1f}, {100 * hi[1]:.1f}] (raw {_pct(t4['raw'], t4['n']).strip()})"
            )
        rules: dict[str, int] = defaultdict(int)
        for s in scored:
            if s["convention_rule"]:
                rules[s["convention_rule"]] += 1
        print(
            "conventions credits by rule:",
            dict(rules),
            "| strict drops (judge-correct, list item missing):",
            sum(1 for s in scored if s["list_missing"] and s["raw"] and s["cat"] != "cat5"),
        )
        if "swapped" in res:
            w = res["swapped"]
            print(
                f"speaker-swap stratum (M-16): n={w['n']} raw={w['raw']} conventions={w['conventions']} strict={w['strict']}; "
                f"rest cat1-4 n={w['rest_n']} raw={w['rest_raw']}"
            )
        entry: dict[str, Any] = {"table": res["table"], "convention_rules": dict(rules)}
        if "swapped" in res:
            entry["swapped"] = res["swapped"]
        if args.labels:
            lm = label_metrics(scored, args.labels)
            entry["labels"] = lm
            print("hand labels (counts):", json.dumps(lm))
        summary[name] = entry
        if True:  # the listing is always written (local, gitignored)
            out_dir = Path(args.out) if args.out else Path(run).resolve().parent / "_analysis"
            out_dir.mkdir(parents=True, exist_ok=True)
            by_key = {(r["item_id"], r["query_id"]): r for r in rows}
            with open(out_dir / f"r14_rescore_{name}.tsv", "w", encoding="utf-8") as fh:
                fh.write(
                    "item\tqid\tcat\traw\tconvention_rule\tlist_missing\tstrict\tgold\tanswer\n"
                )
                for s in scored:
                    if s["convention_rule"] or (s["list_missing"] and s["raw"]):
                        r = by_key[s["key"]]
                        gold = str(r.get("gold") or "").replace("\t", " ").replace("\n", " ")
                        ans = str(r.get("answer") or "")[:300].replace("\t", " ").replace("\n", " ")
                        fh.write(
                            f"{s['key'][0]}\t{s['key'][1]}\t{s['cat']}\t{int(s['raw'])}\t"
                            f"{s['convention_rule'] or ''}\t{int(s['list_missing'])}\t{int(s['strict'])}\t{gold}\t{ans}\n"
                        )
            print(f"listing (text, local only): {out_dir / f'r14_rescore_{name}.tsv'}")
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
