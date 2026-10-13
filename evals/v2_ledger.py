"""Results ledger of the v2 screens (RUNBOOK2): every sample / focus / final result, one line per run, never rewritten.

    python v2_ledger.py summary [--mode sample] [--sort net|delta]   latest result per screen, best first
    python v2_ledger.py history S13 [--mode sample]                  every iteration of one screen, oldest first
    python v2_ledger.py mark S13 promote_candidate|drop|iterate ["note"]
    python v2_ledger.py decisions                                    the current decision of every marked screen

Files (under ``evals/runs/_logs/``, which is git-ignored; override the folder with ``V2_LEDGER_DIR``):

* ``v2_results.tsv``   append-only, header row, columns ``COLUMNS`` below. Written by ``eval_sample.py`` (and by the
  queue for focus / final runs); sample rows are DESCRIPTIVE and carry ``verdict`` = ``DESCRIPTIVE``.
* ``v2_decisions.tsv`` append-only ``ts, screen, decision, note``; the last line per screen wins. MODE=final reads it:
  only ``promote_candidate`` screens reach the full set and the sealed blind sets.

Units: delta is in accuracy points (OP-Bench sample rows: mean-score points x100). For sample rows ``ci_*`` is the paired
question-level bootstrap interval of delta (same unit); for focus / final rows it is the gate's conversation-cluster
bootstrap of the net in questions (the row note says so).

``arm_digest`` (a short hash of the arm file) is recorded so a change to an arm between two iterations shows up in
``history`` even when the arm name is the same.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
DECISIONS = ("promote_candidate", "drop", "iterate")
COLUMNS = (
    "ts", "mode", "screen", "arm", "arm_digest", "reference", "commit", "slice_id", "slice_hash", "n",
    "wins", "losses", "ties", "net", "delta", "ci_low", "ci_high", "per_category", "cost", "latency_p50_ms",
    "latency_p95_ms", "comparability_key", "verdict", "notes", "tgt_rec", "ctl_lost",
)
# tgt_rec / ctl_lost (FAIL-SLICE) are filled only for targeted slices ("recovered/wrong targets", "lost/controls"); a ledger file
# written before they existed keeps its old header, and the two values then travel in ``notes`` (see ``append``).
NUMERIC = ("n", "wins", "losses", "ties", "net", "delta", "ci_low", "ci_high")


def log_dir() -> Path:
    return Path(os.environ.get("V2_LEDGER_DIR") or HERE / "runs" / "_logs")


def results_path() -> Path:
    return log_dir() / "v2_results.tsv"


def decisions_path() -> Path:
    return log_dir() / "v2_decisions.tsv"


def _clean(v: object) -> str:
    return str(v).replace("\t", " ").replace("\n", " ").replace("\r", " ")


def append(row: dict, path: Path | None = None) -> None:
    """Append one result row (missing columns are blank; unknown keys are rejected). Creates the file with its header."""
    path = path or results_path()
    unknown = set(row) - set(COLUMNS)
    if unknown:
        raise KeyError(f"unknown ledger columns: {sorted(unknown)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    full = {"ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), **row}
    cols = COLUMNS
    if not new:  # never rewrite an existing file: follow its header; columns it lacks are folded into notes
        with path.open(encoding="utf-8", newline="") as f:
            cols = tuple(f.readline().rstrip("\r\n").split("\t")) or COLUMNS
        extra = [f"{c}={full[c]}" for c in COLUMNS if c not in cols and full.get(c)]
        if extra:
            full["notes"] = (str(full.get("notes", "")) + " | " + " ".join(extra)).strip(" |")
    with path.open("a", encoding="utf-8", newline="") as f:
        if new:
            f.write("\t".join(COLUMNS) + "\n")
        f.write("\t".join(_clean(full.get(c, "")) for c in cols) + "\n")


def append_paired(r, *, mode: str, screen: str, arm: str, reference: str, commit: str = "", slice_id: str = "",
                  slice_hash: str = "", arm_digest: str = "", key: str = "", notes: str = "") -> None:
    """Ledger row from a ``paired_gate.PairedResult`` (focus / full runs; the verdict is the gate's own)."""
    per = ";".join(f"{c}:+{d['gained']}/-{d['lost']}" for c, d in r.by_category.items())
    append({
        "mode": mode, "screen": screen, "arm": arm, "arm_digest": arm_digest, "reference": reference, "commit": commit,
        "slice_id": slice_id, "slice_hash": slice_hash, "n": r.n, "wins": r.gained, "losses": r.lost,
        "ties": r.n - r.gained - r.lost, "net": r.net,
        "delta": f"{r.acc_arm - r.acc_ref:.3f}", "ci_low": f"{r.ci_low:.3f}", "ci_high": f"{r.ci_high:.3f}",
        "per_category": per, "comparability_key": key, "verdict": r.verdict, "notes": (notes + " ci=net questions, conversation-cluster bootstrap").strip(),
    })


def read(path: Path | None = None) -> list[dict]:
    path = path or results_path()
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return [dict(r) for r in csv.DictReader(f, delimiter="\t")]


def _num(r: dict, k: str) -> float:
    try:
        return float(r.get(k) or "nan")
    except ValueError:
        return float("nan")


def decisions(path: Path | None = None) -> dict[str, dict]:
    """Latest decision per screen id: ``{screen: {"decision", "note", "ts"}}``."""
    path = path or decisions_path()
    out: dict[str, dict] = {}
    if path.exists():
        with path.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f, delimiter="\t"):
                out[r["screen"]] = {"decision": r["decision"], "note": r.get("note", ""), "ts": r["ts"]}
    return out


def mark(screen: str, decision: str, note: str = "", path: Path | None = None) -> None:
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}, got {decision!r}")
    path = path or decisions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8", newline="") as f:
        if new:
            f.write("ts\tscreen\tdecision\tnote\n")
        f.write(f"{datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}\t{_clean(screen)}\t{decision}\t{_clean(note)}\n")


def promote_candidates(path: Path | None = None) -> list[str]:
    return sorted(s for s, d in decisions(path).items() if d["decision"] == "promote_candidate")


def latest(rows: list[dict], mode: str | None = None) -> list[dict]:
    """The last row (file order) of every screen, optionally for one mode."""
    last: dict[str, dict] = {}
    for r in rows:
        if mode and r["mode"] != mode:
            continue
        last[r["screen"]] = r
    return list(last.values())


def summary(rows: list[dict], mode: str | None = None, sort: str = "net") -> list[dict]:
    """Latest per screen, best first: by net, then by the interval's lower end, then by delta."""
    return sorted(latest(rows, mode), key=lambda r: (-_num(r, sort), -_num(r, "ci_low"), -_num(r, "delta")))


def history(rows: list[dict], screen: str, mode: str | None = None) -> list[dict]:
    return [r for r in rows if r["screen"] == screen and (not mode or r["mode"] == mode)]


def _tgt(r: dict, key: str) -> str:
    """tgt_rec / ctl_lost of a row: its own column, else the value folded into notes by an older ledger header."""
    v = r.get(key)
    if v:
        return v
    m = re.search(rf"\b{key}=(\d+/\d+)", r.get("notes") or "")
    return m.group(1) if m else ""


def _fmt(r: dict, dec: dict[str, dict]) -> str:
    d = dec.get(r["screen"], {}).get("decision", "-")
    tl = f" tgt_rec {_tgt(r, 'tgt_rec'):>6s} ctl_lost {_tgt(r, 'ctl_lost'):>6s}" if _tgt(r, "tgt_rec") else ""
    return (f"{r['screen']:5s} {r['mode']:6s} {r['arm'][:34]:34s} n={r['n']:>4s} +{r['wins']}/-{r['losses']}/={r['ties']} "
            f"net {r['net']:>4s} d {_num(r, 'delta'):+6.2f} [{_num(r, 'ci_low'):+.2f},{_num(r, 'ci_high'):+.2f}] "
            f"{r['slice_id'][:24]:24s} {r['commit'][:7]} {d:17s} {r['ts']}{tl}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("summary")
    s.add_argument("--mode", default=None)
    s.add_argument("--sort", choices=("net", "delta"), default="net")
    h = sub.add_parser("history")
    h.add_argument("screen")
    h.add_argument("--mode", default=None)
    m = sub.add_parser("mark")
    m.add_argument("screen")
    m.add_argument("decision", choices=DECISIONS)
    m.add_argument("note", nargs="?", default="")
    sub.add_parser("decisions")
    rc = sub.add_parser("record", help="append a verdict-only row (OP-Bench focus, blind sets: no per-question numbers)")
    for k in ("mode", "screen", "arm", "reference", "commit", "slice_id", "verdict", "notes"):
        rc.add_argument(f"--{k.replace('_', '-')}", default="")
    a = ap.parse_args(argv)
    if a.cmd == "mark":
        mark(a.screen, a.decision, a.note)
        print(f"{a.screen}: {a.decision}")
        return 0
    if a.cmd == "record":
        append({k: getattr(a, k) for k in ("mode", "screen", "arm", "reference", "commit", "slice_id", "verdict", "notes")})
        return 0
    if a.cmd == "decisions":
        for sc, d in sorted(decisions().items()):
            print(f"{sc:6s} {d['decision']:18s} {d['ts']} {d['note']}")
        return 0
    rows = read()
    if not rows:
        print(f"no results yet ({results_path()})")
        return 0
    dec = decisions()
    if a.cmd == "summary":
        print("DESCRIPTIVE results; sample rows never carry a PROMOTE verdict. Best net first.")
        print("Targeted slices (fail_*) are NOT overall accuracy: read tgt_rec (target-class recovered / wrong targets) and ctl_lost (controls lost / controls).")
        for r in summary(rows, a.mode, a.sort):
            print(_fmt(r, dec))
    else:
        for r in history(rows, a.screen, a.mode):
            print(_fmt(r, dec) + f" arm_digest={r['arm_digest']}")
            if r["per_category"]:
                print(f"      per-category: {r['per_category']}")
            if r["notes"]:
                print(f"      notes: {r['notes']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
