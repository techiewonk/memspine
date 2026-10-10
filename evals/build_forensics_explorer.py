# ruff: noqa: E501
"""Forensics explorer: one self-contained HTML file with every dev question, every screen, every gate.

    python evals/build_forensics_explorer.py [--out evals/runs/_analysis/forensics_explorer.html]
                                             [--summary evals/reports/forensics_summary.html]
                                             [--extra-screen ID ...]

CPU only, no model call, no network, one process. Re-runnable: it scans ``evals/runs`` for screens on every
invocation (``<id>-loc`` / ``<id>-opb`` runs, plus ``wvr-*-i2``), skips what has no results, and shows runs without a
``summary`` row as "running" (their finished rows still appear in the question strips).

LICENCE RULE. OP-Bench has no licence: scores may be published, data and per-question derived files may not. The
explorer carries dataset text (questions, answers, dialogue turns) and is therefore written ONLY under the gitignored
``evals/runs/_analysis/`` (the script refuses any other location, and refuses a path git does not ignore). Never
commit it. The optional summary is text-free (counts, ids, codes, numbers) and is checked against the data text
before it is written. Only this generator is meant to be committed.

Dev only BY DEFAULT. The held-out conversations (conv-43/44/47/48/49/50) are dropped the moment a dataset is loaded,
and the script aborts if any result or forensics row names one. ``--all-convs`` lifts that guard (the user decided on
2026-10-10 to use the held-out conversations in the full logged run): all 10 conversations, categories 1-4, the
baseline is ``--loc-ref`` (default ``full-persp-loc``, read from its ``--trace`` folder with ``trace_full``), the
stage codes come from ``analysis/full_persp_loc_stage_labels.txt`` and the screens are the runs named with
``--extra-run`` (a run id; ``...-opb`` ids are OP-Bench runs). Example:

    python evals/build_forensics_explorer.py --all-convs --no-summary --extra-run xb-loc-dev --extra-run qa-full-qs-eq06-fix --extra-run xb-opb-dev

OP-Bench: with --all-convs the baseline is ``--opb-ref`` (default ``full-persp-opb``, all 859 probes of the 10 personas, read from its --trace folder); xb-opb-dev (dev personas only) is a comparison run.

Inputs: baseline ``xb-loc-dev`` / ``xb-opb-dev`` (+ ``--forensics`` dirs), no-memory ``opb-base-dev``, every screen,
``analysis/catalogue/locomo_dev_failures.jsonl`` (stage codes), ``runs/_analysis/opbench_dev_failures*.jsonl``
(OP-Bench classes), ``analysis/locomo_errata.json``, ``analysis/GAP_REGISTER.md`` (gap-row text) and
``analysis/FAILURE_FORENSICS_BASELINE_2026-10-10.md`` (lever table, mirrored in ``LEVERS``).
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import html
import json
import math
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
RUNS = HERE / "runs"
ANALYSIS = HERE / "analysis"
OUT_DIR = RUNS / "_analysis"
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != HERE]
sys.path.append(str(HERE))

import eval_screen  # noqa: E402  (scoring conventions: SUBS, OPB_BAND, ok-rule)
from memspine_evals import judge_prompts as JP  # noqa: E402
from memspine_evals import opbench as OB  # noqa: E402
from memspine_evals import readers as RD  # noqa: E402
from memspine_evals import refusal as RF  # noqa: E402
from memspine_evals import trace_full as TF  # noqa: E402
from memspine_evals.datasets import LoCoMoDataset  # noqa: E402
from memspine_evals.datasets.op_bench import OPBenchDataset, persona_share  # noqa: E402
from memspine_evals.date_check import date_equivalent  # noqa: E402
from memspine_evals.judge_conventions import check_conventions  # noqa: E402

LOC_REF, OPB_REF, OPB_BASE = "xb-loc-dev", "xb-opb-dev", "opb-base-dev"
HELDOUT = ("conv-43", "conv-44", "conv-47", "conv-48", "conv-49", "conv-50")
CAT_NAME = {
    "cat1": "multi-hop",
    "cat2": "temporal",
    "cat3": "open-domain",
    "cat4": "single-hop",
    "cat5": "adversarial",
}
CAT_ORDER = ["cat4", "cat1", "cat2", "cat3", "cat5"]
STAGE_NAME = {
    "a": "a write path",
    "b": "b recall (no leg)",
    "c": "c cut (fusion / rerank / budget)",
    "d": "d reader",
    "e": "e judge",
    "f": "f gold / dataset error",
}
FUNNEL_ORDER = ["recall", "fusion", "rerank", "gate", "assembly"]
OPB_PASS = 0.5  # a probe "fails" below 0.5, the rule of the failure catalogue
SIZE_LIMIT = 40_000_000
ALL_CONVS = False  # set by --all-convs: all 10 conversations, cat 1-4, no held-out guard

# --all-convs mode: the stage codes of analysis/full_persp_loc_stage_labels.txt, with the built feature and gaps each maps to.
FULL_CODES = {
    "b": (
        "b",
        "recall: no leg returned a gold turn",
        ["B1", "B8", "I4", "I67"],
        "intent list trigger, agentic multi-step read, decomposition planner",
        "query shape + sufficiency check",
        "read.list_trigger=intent (I4); read.agentic (I67, screen pending)",
        "+1-3 reads per question",
    ),
    "cf": (
        "c",
        "cut at fusion: in a leg, outside the pool",
        ["I75", "B7", "B10", "I9"],
        "reserve pool slots for the top of every leg; perspective leg as multiplier; pool 3 + chunked rerank",
        "per-leg top-3 floor in the pool",
        "read.candidate_pool; rerank_chunk_chars (I9); I75 not built",
        "pool +0 (reserved slots) or +rerank pairs",
    ),
    "cr": (
        "c",
        "cut at rerank_keep",
        ["B10", "I9"],
        "rerank_context 1-2; rerank_keep 15",
        "score margin to the keep boundary",
        "read.rerank_keep; rerank_context (B10)",
        "+0-2 rerank pairs",
    ),
    "d:det": (
        "d",
        "reader: wrong detail (evidence in context)",
        ["C1", "C2", "B10", "I6"],
        "top-hit-first block, rerank_context, token window",
        "rerank score order in the prompt",
        "rerank_context (B10); replay_window_unit=tokens (I6)",
        "none",
    ),
    "d:lst": (
        "d",
        "reader: incomplete list",
        ["C6", "I56", "I4"],
        "list-shaped prompt, enumerate then merge",
        "is_set_question / intent trigger",
        "routed_generic list arm; grounded_generic_list (C6)",
        "+1 reader call on list questions",
    ),
    "d:cnt": (
        "d",
        "reader: wrong count",
        ["I56"],
        "enumerate-then-count with event identity",
        "is_count",
        "--count-verify (I56)",
        "+1 reader call on count questions",
    ),
    "d:dar": (
        "d",
        "reader: date / duration arithmetic, wrong date",
        ["I57", "I76", "I79"],
        "duration solver by code, bare-weekday annotation, anchor-vs-event date",
        "is_duration / is_temporal",
        "--date-repair (I57, when questions only); I76 and I79 not built",
        "none (code)",
    ),
    "d:ref": (
        "d",
        "reader: refusal or premise denial",
        ["I1", "I61", "I3"],
        "neutral retry, premise-tolerant answering",
        "first answer is a refusal",
        "retry_refusal neutral (I1, on in this run); I61 open",
        "+1 reader call",
    ),
    "d:inf": (
        "d",
        "reader: inference refused or wrong",
        ["I3", "C3", "C11"],
        "inference route, larger reader",
        "is_inference",
        "routed_generic (I3/C6), screen pending; C11 blocked by GPU memory",
        "+0-1 reader call",
    ),
    "d:dis": (
        "d",
        "reader: distractor line",
        ["C1", "B10"],
        "top-hit-first block, rerank_context",
        "-",
        "rerank_context (B10)",
        "none",
    ),
    "d:spk": (
        "d",
        "reader: wrong speaker",
        ["I59", "I63"],
        "owner check at read, owner labels",
        "spk / sub tags",
        "read owner check (I59, built, opt-in)",
        "none",
    ),
    "e": (
        "e",
        "judge: the answer states the gold fact",
        ["I58", "I77"],
        "judge conventions plus containment and date-range equivalence",
        "-",
        "--judge-conventions (I58, reported as a second column)",
        "none",
    ),
    "f": (
        "f",
        "gold / dataset error (errata)",
        ["I62"],
        "report with and without errata",
        "-",
        "locomo_errata.json",
        "none",
    ),
}
# --all-convs mode: levers to 90 on the full 1,540 (FULL_RUN_FORENSICS_2026-10-10.md section 5); expected questions at the stated capture.
FULL_LEVERS = [
    dict(
        n=1,
        name="Leg-protected pool, perspective leg as multiplier (I75, NEW)",
        q14=14.0,
        q15=14.0,
        screens=[],
    ),
    dict(
        n=2,
        name="Duration / interval solver by code + date repair (I76 NEW, I57, I79 NEW)",
        q14=12.0,
        q15=12.0,
        screens=[],
    ),
    dict(
        n=3,
        name="Agentic read, intent trigger, planner for recall (I67, I4)",
        q14=14.0,
        q15=14.0,
        screens=[],
    ),
    dict(
        n=4,
        name="Wider pool, chunked rerank, rerank_context (B10, I9, pool 3)",
        q14=11.0,
        q15=11.0,
        screens=[],
    ),
    dict(
        n=5,
        name="Inference route, premise-tolerant answering (routed_generic, I61, I1)",
        q14=9.0,
        q15=9.0,
        screens=[],
    ),
    dict(
        n=6,
        name="List and count steps (I56, C6, routed_generic list arm)",
        q14=8.0,
        q15=8.0,
        screens=[],
    ),
    dict(
        n=7,
        name="Detail: top-hit-first, rerank_context, token window, owner check (B10, I6, I59)",
        q14=7.0,
        q15=7.0,
        screens=[],
    ),
]
LEG_CAP = 30

SCREEN_DESC = {
    "r3-relgate": "relevance gate with OpenDecider yes/no (I29/I37): empty context for off-topic requests",
    "r3-generic": "generic grounded prompt / infer route (I3, C6)",
    "r3-intent": "list-mode intent trigger (I4)",
    "r3-dedupe": "near-duplicate hit dedupe (I31)",
    "r3-latest": "latest-wins on conflicting values (I17)",
    "r3-norecord": "no-record hint: say so when nothing supports the asserted event (I32)",
    "r3d-relnamed": "relevance gate + I74 named-entity bypass",
    "r3-persp": "perspective layer: subject-weighted speaker vote (I39)",
    "r3-paxes": "perspective axes spk/sub/ask (I39, I47, I54)",
    "wvr-i2": "word-vector leg (B15), LoCoMo only",
}

# Path to 89.6: FAILURE_FORENSICS_BASELINE_2026-10-10.md section 2 (capture-rate assumptions on measured class
# sizes). q = expected questions, cat 1-4 / cat 1-5 (the doc's own numbers; lever 2 was measured at half of it).
LEVERS = [
    dict(
        n=1,
        name="Subject / owner check at read (I59, I39)",
        q14=0.0,
        q15=20.0,
        screens=["r3-persp", "r3-paxes"],
    ),
    dict(
        n=2,
        name="Judge conventions (I58)",
        q14=8.0,
        q15=8.0,
        screens=[],
        measured_note="measured offline: +0.7 pt cat 1-4 / +0.5 pt cat 1-5 (4 credits), measurement only",
    ),
    dict(
        n=3,
        name="Recall for set questions (list trigger, subject legs, word-vector leg)",
        q14=9.0,
        q15=9.0,
        screens=["r3-intent", "r3-persp", "wvr-i2"],
    ),
    dict(
        n=4,
        name="Inference route and neutral retry",
        q14=6.0,
        q15=6.0,
        screens=["r3-generic", "r3c-routed"],
    ),
    dict(
        n=5,
        name="Wider pool, chunked rerank, rerank_context",
        q14=6.0,
        q15=6.0,
        screens=["r4-rctx1", "r4-rctx2"],
    ),
    dict(
        n=6,
        name="Near-match gate for cat 5 (no-record, relevance gate)",
        q14=0.0,
        q15=4.0,
        screens=["r3-norecord", "r3-relgate", "r3d-relnamed", "r3c-storecal", "r3c-sens"],
    ),
    dict(n=7, name="Date repair by code (I57)", q14=4.0, q15=4.0, screens=[]),
    dict(
        n=8,
        name="Enumerate-then-count, dedupe, list prompt",
        q14=4.0,
        q15=4.0,
        screens=["r3-dedupe", "r3c-routed"],
    ),
    dict(
        n=9,
        name="Top-hit-first block, rerank_context, token window",
        q14=3.5,
        q15=3.5,
        screens=["r4-rctx1", "r4-rctx2"],
    ),
]


# --------------------------------------------------------------------------------------------- small helpers
def jl(path: Path) -> list[dict]:
    """Tolerant JSONL read: a running run may end in a half-written line."""
    out: list[dict] = []
    if not path.exists():
        return out
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def jlz(path: Path) -> list[dict]:
    """``jl`` that also reads the gzipped twin a ``--trace-full`` run leaves for big files."""
    if path.exists():
        return jl(path)
    gz = Path(f"{path}.gz")
    if not gz.exists():
        return []
    import gzip
    import io

    out: list[dict] = []
    with gzip.open(gz, "rt", encoding="utf-8", errors="replace") as fh:
        for line in io.StringIO(fh.read()):
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def log_dir(run: str) -> Path:
    """The folder with a run's stage logs. A ``--trace-full`` folder ``<run>--trace`` that holds a
    ``forensics.jsonl`` (or its gzip twin) wins: it carries the ``trace_full`` block that the plain
    ``<run>--forensics`` folder of the same run lacks. Else ``<run>--forensics``, else the trace folder."""
    f = RUNS / f"{run}--forensics"
    t = RUNS / f"{run}--trace"
    if t.is_dir() and any((t / n).exists() for n in ("forensics.jsonl", "forensics.jsonl.gz")):
        return t
    return f if f.is_dir() or not t.is_dir() else t


def clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def run_dir(run: str) -> Path | None:
    for p in sorted(RUNS.glob(f"{run}--*")):
        if p.is_dir() and not p.name.endswith("--forensics"):
            return p
    return None


def check_heldout(rows: list[dict], what: str) -> None:
    if ALL_CONVS:
        return
    for r in rows:
        item = str(r.get("item_id") or r.get("item") or "")
        if item.split(":")[0] in HELDOUT:
            raise SystemExit(f"{what}: row names held-out item {item}; refusing to continue")


def ok_rule(r: dict) -> bool:
    """Same rule as eval_screen.locomo: date-checked score when recorded, completed, non-empty answer."""
    sc = r.get("score_date_checked", r.get("score"))
    return (
        float(sc or 0) >= 1
        and r.get("status") == "completed"
        and bool((r.get("answer") or "").strip())
    )


# --------------------------------------------------------------------------------------------- datasets
def load_dev_loc():
    split = json.loads((ANALYSIS / "locomo_split.json").read_text(encoding="utf-8"))
    dev = set(split["dev_items"])
    ds = LoCoMoDataset(REPO / "data" / "locomo10.json", "auto")
    if ALL_CONVS:
        dev = {item.item_id for item in ds.items()}
    tt: dict[str, dict] = {}
    qs: dict[str, dict] = {}
    for item in ds.items():
        if item.item_id not in dev:
            continue
        tt[item.item_id] = {
            t.turn_id: [t.speaker, t.text, None, t.session_id, t.timestamp] for t in item.history
        }
        for q in item.queries:
            if ALL_CONVS and q.type_label == "cat5":
                continue  # the full logged run is categories 1-4
            ev = []
            for e in (
                q.meta.get("distractor_evidence", ())
                if q.meta.get("adversarial")
                else q.gold_turn_ids
            ):
                ev += [p.strip() for p in str(e).split(";") if p.strip()]
            qs[f"{item.item_id}:{q.query_id}"] = dict(
                item=item.item_id,
                id=q.query_id,
                q=q.text,
                cc=q.type_label,
                adv=bool(q.meta.get("adversarial")),
                ev=[] if q.meta.get("adversarial") else ev,
                dv=ev if q.meta.get("adversarial") else [],
            )
    del ds
    return sorted(dev), tt, qs


def load_dev_opb():
    split = json.loads((ANALYSIS / "opbench_persona_split.json").read_text(encoding="utf-8"))
    dev = set(split["dev_items"])
    ds = OPBenchDataset(HERE / "data" / "opbench_src", "auto")
    meta: dict[str, dict] = {}
    for item in ds.items():
        if item.item_id not in dev and not ALL_CONVS:
            continue
        for q in item.queries:
            meta[q.query_id] = dict(
                item=item.item_id,
                persona=item.meta.get("persona"),
                qm=dict(q.meta),
                dev=item.item_id in dev,
            )
    del ds
    return meta


# --------------------------------------------------------------------------------------------- run loading
def load_run(run: str, expected: int) -> dict:
    d = run_dir(run)
    if d is None:
        return dict(id=run, status="missing", rows={}, n=0, exp=expected, dir=None)
    rows = jl(d / "results.jsonl")
    check_heldout(rows, run)
    manifest = next((r for r in rows if r.get("kind") == "manifest"), None)
    summary = next((r for r in rows if r.get("kind") == "summary"), None)
    res = {}
    for r in rows:
        if r.get("kind") == "result":
            res[f"{r.get('item_id')}:{r.get('query_id')}"] = r
    status = "complete" if summary is not None else "running"
    return dict(
        id=run,
        status=status,
        rows=res,
        n=len(res),
        exp=expected,
        dir=d,
        manifest=manifest,
        summary=summary,
    )


def opb_subs(path: Path) -> dict | None:
    if not path or not (path / "opbench_summary.json").exists():
        return None
    o = json.loads((path / "opbench_summary.json").read_text(encoding="utf-8"))["official"]
    tt, st = o.get("by_task_type", {}), o.get("sycophancy_subtypes", {})

    def avg(dd, k):
        return float(dd[k]["average"]) if k in dd else None

    flat = {
        "irrelevance_fully_irrelevant": avg(tt, "irrelevance_easy"),
        "irrelevance_baiting": avg(tt, "irrelevance_hard"),
        "sycophancy_fact": avg(st, "sycophancy/fact"),
        "sycophancy_value": avg(st, "sycophancy/value"),
        "sycophancy_memory": avg(st, "sycophancy/memory"),
        "repetition": avg(tt, "diversity"),
        "official_overall": float(o["overall"]["average"]),
    }
    return {k: round(100 * v, 2) for k, v in flat.items() if v is not None}


def discover_screens(extra: list[str]) -> list[str]:
    ids = set(extra)
    for p in RUNS.iterdir():
        n = p.name
        if (
            not p.is_dir()
            or "--" in n
            or n.startswith(("xb-", "opb-base", "_", "full-", "qa-full-"))
        ):
            continue
        m = re.match(r"^(.+)-(loc|opb)$", n)
        if m and not n.endswith("-t2"):
            ids.add(m.group(1))
        elif re.match(r"^wvr-(.+-)?i2$", n):
            ids.add(n)
    keep = []
    for sid in sorted(ids):
        has = [
            (RUNS / f"{sid}-{k}").is_dir() and run_dir(f"{sid}-{k}") for k in ("loc", "opb")
        ] or [False]
        if any(has) or run_dir(sid):
            keep.append(sid)
    return keep


def screen_runs(sid: str) -> dict[str, str | None]:
    out = {"loc": None, "opb": None}
    if ALL_CONVS:  # --extra-run ids are run ids, not screen prefixes
        if run_dir(sid):
            out["opb" if re.search(r"-opb(-|$)", sid) else "loc"] = sid
        return out
    if run_dir(f"{sid}-loc"):
        out["loc"] = f"{sid}-loc"
    elif re.match(r"^wvr-(.+-)?i2$", sid) and run_dir(sid):
        out["loc"] = sid
    if run_dir(f"{sid}-opb"):
        out["opb"] = f"{sid}-opb"
    return out


# --------------------------------------------------------------------------------------------- forensics
def rk(lst) -> dict:
    out = {}
    for e in lst or []:
        out.setdefault(e["turn"], e["r"])
    return out


def fx_compact(x: dict, gold: list[str]) -> dict:
    vec, lex, fused, pool, fin = (
        rk(x.get(k)) for k in ("vector", "lexical", "fused", "pool", "final")
    )
    extra = {n: rk(leg) for n, leg in (x.get("extra_legs") or {}).items()}
    rs = x.get("rerank_scores") or []
    order = sorted(rs, key=lambda e: -e["score"])
    rr = {e["turn"]: i + 1 for i, e in enumerate(order)}
    rsc = {e["turn"]: e["score"] for e in rs}
    ctx = [c["turn"] for c in x.get("context_records") or []]
    cset = set(ctx)
    g = []
    for t in gold:
        v, lx = vec.get(t), lex.get(t)
        xr = {n: m[t] for n, m in extra.items() if t in m}
        inleg = v is not None or lx is not None or bool(xr)
        inpool = t in fused or t in pool
        if not inleg and not inpool:
            lost = "recall"
        elif not inpool:
            lost = "fusion"
        elif t not in fin:
            lost = "rerank"
        elif t not in cset:
            lost = "gate" if not ctx else "assembly"
        else:
            lost = "ok"
        if lost not in ("ok",) and t in cset:
            lost = "rescued"
        g.append(
            [
                t,
                v,
                lx,
                xr or None,
                fused.get(t),
                rr.get(t),
                rsc.get(t),
                fin.get(t),
                int(t in cset),
                lost,
            ]
        )
    fs = None
    if g:
        sev = [r[9] for r in g if r[9] in FUNNEL_ORDER]
        fs = min(sev, key=FUNNEL_ORDER.index) if sev else "ok"

    def lst(seq, key="score"):
        return [[e["turn"], round(e.get(key, 0), 3)] for e in (seq or [])[:LEG_CAP]]

    tr = dict(
        v=lst(x.get("vector")),
        l=lst(x.get("lexical")),
        x={n: lst(m) for n, m in (x.get("extra_legs") or {}).items() if m},
        f=lst(x.get("fused")),
        p=[[e["turn"], round(e["score"], 3)] for e in rs],
        fin=lst(x.get("final")),
        pr=x.get("reranker"),
    )
    return dict(
        xf=x.get("trace_full"),
        g=g,
        tr=tr,
        tok=x.get("context_tokens"),
        fs=fs,
        ce=not ctx,
        ctx=ctx,
        fin=[e["turn"] for e in (x.get("final") or [])][:12],
        legs=[n for n, m in extra.items() if m],
        dec=x.get("decisions"),
        rb=x.get("relevance_bypass"),
        mx=round(max((e["score"] for e in rs), default=0), 4) if rs else None,
        nr=len(rs) if rs else None,
    )


TEXT_INDEX: dict[
    tuple[str, str], list[str]
] = {}  # (item, question text) -> keys in dataset order (legacy logs)


def load_fx(
    run: str, gold_by_key: dict[str, list[str]], tt: dict, ann_seen: dict
) -> dict[str, dict]:
    p = log_dir(run) / "forensics.jsonl"
    out = {}
    seen: collections.Counter = collections.Counter()
    for x in jlz(p):
        check_heldout([x], run + " forensics")
        if x.get("query_id") is not None:
            key = f"{x['item']}:{x['query_id']}"
        else:  # older stage logs carry no query_id: pair by question text, first to first
            tk = (x["item"], x["query"])
            n = seen[tk]
            seen[tk] += 1
            cands = TEXT_INDEX.get(tk, [])
            if n >= len(cands):
                continue
            key = cands[n]
        out[key] = fx_compact(x, gold_by_key.get(key, []))
        for c in x.get("context_records") or []:
            ann_seen.setdefault((x["item"], c["turn"]), c["text"])
    return out


INGEST_KNOWN = {
    "run_id", "item", "turn", "session", "speaker", "source_timestamp", "source_text", "written",
    "record_id", "stored_text", "text_identical", "valid_from", "memory_type", "group_id",
    "session_id", "batch_size", "quarantined", "trust", "status",
}  # fmt: skip


def load_ingest(run: str) -> dict[str, dict]:
    """Write side per turn: what the engine stored for it (ingest.jsonl)."""
    out: dict[str, dict] = {}
    for x in jlz(log_dir(run) / "ingest.jsonl"):
        check_heldout([x], run + " ingest")
        rec = clean(
            dict(
                w=x.get("written"), q=x.get("quarantined"), tr=x.get("trust"),
                vf=x.get("valid_from"), mt=x.get("memory_type"), g=x.get("group_id"),
                rid=x.get("record_id"), st=x.get("status"), ti=x.get("text_identical"),
                stx=None if x.get("text_identical") else x.get("stored_text"),
                bs=x.get("batch_size"),
                ex={k: v for k, v in x.items() if k not in INGEST_KNOWN and k != "write_timers"} or None,
                wt=x.get("write_timers"),
            )
        )  # fmt: skip
        out[f"{x['item']}|{x['turn']}"] = rec
    return out


# --------------------------------------------------------------------------------------------- gap register
def parse_gaps() -> dict:
    out = {}
    path = ANALYSIS / "GAP_REGISTER.md"
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\|\s*([A-Z]+\d*(?:-\d+)?)\s*\|", line)
        if not m:
            continue
        cols = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cols) < 5 or m.group(1) in out:
            continue
        out[m.group(1)] = dict(t=cols[1][:300], e=cols[2][:700], s=cols[3][:700], st=cols[-1][:400])
    return out


# --------------------------------------------------------------------------------------------- build
def build(args) -> dict:
    dev_items, tt, loc_q = load_dev_loc()
    opb_meta = load_dev_opb()
    gaps_all = parse_gaps()
    errata_raw = json.loads((ANALYSIS / "locomo_errata.json").read_text(encoding="utf-8"))[
        "entries"
    ]
    errata = collections.defaultdict(list)
    for e in errata_raw:
        errata[f"{e['item']}:{e['qid']}"].append(
            dict(
                tag=e["tag"],
                reason=e.get("reason"),
                borderline=e.get("borderline"),
                source=e.get("source"),
            )
        )

    cat_rows = {}
    if ALL_CONVS:
        cc_of = {k: v["cc"] for k, v in loc_q.items()}
        for line in (
            (ANALYSIS / "full_persp_loc_stage_labels.txt").read_text(encoding="utf-8").splitlines()
        ):
            if line.startswith("#") or not line.strip():
                continue
            cv, qid, code = line.split()
            k = f"{cv}:{qid}"
            if k not in cc_of or code not in FULL_CODES:
                continue
            st, sub, gids, fix, mech, feat, cost = FULL_CODES[code]
            cat_rows[k] = dict(
                query_id=k, category=CAT_NAME[cc_of[k]], failure_code=code, failure_stage=st,
                sub_type=sub, gap_ids=gids, generic_fix=fix, decision_mechanism=mech,
                built_feature=feat, fix_runtime_cost=cost, evidence=None, errata_candidate=None,
            )  # fmt: skip
    else:
        for x in jl(ANALYSIS / "catalogue" / "locomo_dev_failures.jsonl"):
            cat_rows[x["query_id"]] = x
    opb_cat = {x["query_id"]: x for x in jl(OUT_DIR / "opbench_dev_failures_catalogue.jsonl")}
    opb_sig = {x["query_id"]: x for x in jl(OUT_DIR / "opbench_dev_failures.jsonl")}

    # ---- baseline + screens
    n_loc_exp = (
        len(loc_q) if ALL_CONVS else sum(1 for k in loc_q if k.startswith(("conv-26:", "conv-30:")))
    )
    ref = load_run(LOC_REF, len(loc_q))
    if ref["status"] != "complete" or ref["n"] != len(loc_q):
        raise SystemExit(f"baseline {LOC_REF} incomplete: {ref['n']} of {len(loc_q)}")
    opb_ref = load_run(OPB_REF, len(opb_meta))
    if ALL_CONVS and opb_ref["n"] == 0:
        raise SystemExit(f"OP-Bench baseline {OPB_REF} has no rows")
    opb_base = load_run(OPB_BASE, len(opb_meta))

    screens = (
        [r for r in args.extra_run if run_dir(r)]
        if ALL_CONVS
        else discover_screens(args.extra_screen)
    )
    sruns = {}
    for sid in screens:
        names = screen_runs(sid)
        sruns[sid] = dict(
            loc=load_run(names["loc"], n_loc_exp) if names["loc"] else None,
            opb=load_run(names["opb"], len(opb_meta)) if names["opb"] else None,
        )

    # complete first, running last
    def order(sid):
        runs = [r for r in sruns[sid].values() if r]
        return (any(r["status"] != "complete" for r in runs), sid)

    screens.sort(key=order)

    gold_by_key = {k: v["ev"] for k, v in loc_q.items()}
    TEXT_INDEX.clear()
    for k, v in loc_q.items():
        TEXT_INDEX.setdefault((v["item"], v["q"]), []).append(k)
    ann_seen: dict = {}
    fx = {LOC_REF: load_fx(LOC_REF, gold_by_key, tt, ann_seen)}
    for sid in screens:
        r = sruns[sid]["loc"]
        if r:
            fx[r["id"]] = load_fx(r["id"], gold_by_key, tt, ann_seen)
    for (conv, turn), text in ann_seen.items():
        t = tt.get(conv, {}).get(turn)
        if t and text != f"{t[0]}: {t[1]}":
            t[2] = text

    # write side: what the engine stored per turn (baseline stage log); screens compared for drift
    ing = {LOC_REF: load_ingest(LOC_REF)}
    for sid in screens:
        r = sruns[sid]["loc"]
        if r:
            ing[r["id"]] = load_ingest(r["id"])
    mem: dict[str, dict] = {}
    for key, rec in ing[LOC_REF].items():
        conv_id, turn = key.split("|", 1)
        mem.setdefault(conv_id, {})[turn] = rec
    ingest_diff = {}
    for rid, rows in ing.items():
        if rid == LOC_REF or not rows:
            continue
        ingest_diff[rid] = sum(
            1
            for k, v in rows.items()
            if any(v.get(f) != ing[LOC_REF].get(k, {}).get(f) for f in ("w", "q", "stx", "vf"))
        )

    loc_ids = [LOC_REF] + [sruns[s]["loc"]["id"] for s in screens if sruns[s]["loc"]]
    opb_fx_runs = [OPB_REF] + [sruns[s]["opb"]["id"] for s in screens if sruns[s]["opb"]]
    opb_ids = [OPB_BASE, OPB_REF] + [sruns[s]["opb"]["id"] for s in screens if sruns[s]["opb"]]
    loc_runs = {LOC_REF: ref}
    loc_runs.update({sruns[s]["loc"]["id"]: sruns[s]["loc"] for s in screens if sruns[s]["loc"]})
    opb_runs = {OPB_BASE: opb_base, OPB_REF: opb_ref}
    opb_runs.update({sruns[s]["opb"]["id"]: sruns[s]["opb"] for s in screens if sruns[s]["opb"]})

    # OP-Bench stage logs (full runs keep forensics.jsonl with trace_full), keyed by query id
    for rid in opb_fx_runs:
        d = {}
        for x in jlz(log_dir(rid) / "forensics.jsonl"):
            qid = x.get("query_id")
            if qid in opb_meta:
                d[qid] = fx_compact(x, [])
        if d:
            fx[rid] = d

    # ---- gate definitions: fn(row, fxc) -> True/False/None ; logged(run) -> bool
    def rt_logged(run):
        m = (run.get("manifest") or {}).get("reader", {}).get("params", {})
        return bool(m.get("retry_refusal"))

    def has_fx_key(run, key):
        f = fx.get(run["id"]) or {}
        return any(v.get(key) is not None for v in f.values())

    def dec_hit(fxc, task, label=None):
        for d in (fxc or {}).get("dec") or []:
            if d.get("task") == task and (
                label is None
                or (d.get("final") or d.get("label"))
                in (label if isinstance(label, tuple) else (label,))
            ):
                return True
        return False

    GATES = [
        dict(
            id="relgate",
            label="Relevance gate closed (empty context)",
            desc="relevance gate decider (I29/I37): context_records empty, or context_tokens == 0 where no stage log exists",
            logged=lambda run: True,
            fn=lambda row, f: f["ce"] if f else (row.get("ct") or 0) == 0,
        ),
        dict(
            id="i74",
            label="I74 named-entity bypass fired",
            desc="relevance_bypass.fired in forensics.jsonl",
            logged=lambda run: has_fx_key(run, "rb"),
            fn=lambda row, f: bool(((f or {}).get("rb") or {}).get("fired")),
        ),
        dict(
            id="listmode",
            label="List-mode trigger (decider says set / list)",
            desc="decisions[task=list_mode] resolves to set or list",
            logged=lambda run: has_fx_key(run, "dec"),
            fn=lambda row, f: dec_hit(f, "list_mode", ("set", "list")),
        ),
        dict(
            id="reldec",
            label="Relevance decider says irrelevant",
            desc="decisions[task=relevance] resolves to irrelevant (the I28 OpenDecider yes/no)",
            logged=lambda run: has_fx_key(run, "dec"),
            fn=lambda row, f: dec_hit(f, "relevance", "irrelevant"),
        ),
        dict(
            id="bridge",
            label="Bridge gate",
            desc="decisions[task=bridge*]; no run logs a bridge decision yet",
            logged=lambda run: any(
                str(d.get("task", "")).startswith("bridge")
                for v in (fx.get(run["id"]) or {}).values()
                for d in v.get("dec") or []
            ),
            fn=lambda row, f: any(
                str(d.get("task", "")).startswith("bridge") for d in (f or {}).get("dec") or []
            ),
        ),
        dict(
            id="leg_speaker",
            label="Speaker / subject vote leg fired",
            desc="extra_legs speaker_vote* non-empty (perspective, I39/R2-2)",
            logged=lambda run: run["id"] in fx,
            fn=lambda row, f: any(n.startswith("speaker_vote") for n in (f or {}).get("legs", [])),
        ),
        dict(
            id="leg_temporal",
            label="Temporal leg fired",
            desc="extra_legs temporal non-empty",
            logged=lambda run: run["id"] in fx,
            fn=lambda row, f: "temporal" in (f or {}).get("legs", []),
        ),
        dict(
            id="retry",
            label="Refusal retry fired",
            desc="meta.retry_refusal (reader re-asked after a refusal); logged only when the run enables retry",
            logged=rt_logged,
            fn=lambda row, f: bool(row.get("rt")),
        ),
        dict(
            id="retry_acc",
            label="Refusal retry accepted",
            desc="meta.retry_accepted: the second answer replaced the first",
            logged=rt_logged,
            fn=lambda row, f: bool(row.get("rt") and row.get("ra")),
        ),
        dict(
            id="datecheck",
            label="Date check changed the verdict",
            desc="score_date_checked != score where recorded; else offline recompute (memspine_evals.date_check)",
            logged=lambda run: True,
            fn=lambda row, f: bool(row.get("dchg")),
        ),
        dict(
            id="conv",
            label="Judge conventions would credit",
            desc="score_conventions != score where recorded; else offline recompute (memspine_evals.judge_conventions)",
            logged=lambda run: True,
            fn=lambda row, f: bool(row.get("vchg")),
        ),
        dict(
            id="cut_recall",
            label="Gold lost: in no leg (recall)",
            desc="funnel stage; touched = the question has a gold turn in no leg",
            logged=lambda run: run["id"] in fx,
            fn=lambda row, f: bool(f and f["fs"] == "recall"),
        ),
        dict(
            id="cut_fusion",
            label="Gold lost at fusion (pool cut)",
            desc="in a leg, outside the fused pool (candidate_pool x keep)",
            logged=lambda run: run["id"] in fx,
            fn=lambda row, f: bool(f and f["fs"] == "fusion"),
        ),
        dict(
            id="cut_rerank",
            label="Gold lost at rerank floor / cut",
            desc="in the reranker pool, not in the final list",
            logged=lambda run: run["id"] in fx,
            fn=lambda row, f: bool(f and f["fs"] == "rerank"),
        ),
        dict(
            id="cut_gate",
            label="Gold lost: gate emptied the context",
            desc="gold in the final list, but the context came back empty (relevance gate closed)",
            logged=lambda run: run["id"] in fx,
            fn=lambda row, f: bool(f and f["fs"] == "gate"),
        ),
        dict(
            id="cut_budget",
            label="Gold lost at assembly / budget",
            desc="in the final list, not in the context; or context_truncated",
            logged=lambda run: run["id"] in fx,
            fn=lambda row, f: bool((f and f["fs"] == "assembly") or row.get("trunc")),
        ),
    ]

    known_legs = {"speaker_vote", "speaker_vote_a", "speaker_vote_b", "temporal"}
    for leg in sorted(
        {n for run_fx in fx.values() for c in run_fx.values() for n in c["legs"]} - known_legs
    ):
        GATES.insert(
            6,
            dict(
                id=f"leg_{leg}",
                label=f"Extra leg fired: {leg}",
                desc=f"extra_legs[{leg}] non-empty (e.g. the word-vector leg, B15)",
                logged=lambda run: run["id"] in fx,
                fn=lambda row, f, leg=leg: leg in (f or {}).get("legs", []),
            ),
        )

    # ---- LoCoMo rows (compact per run)
    def loc_row(r: dict, qk: str) -> dict:
        meta = r.get("meta") or {}
        sc, sd = r.get("score"), r.get("score_date_checked")
        sv = r.get("score_conventions", meta.get("score_conventions"))
        ok = ok_rule(r)
        wrong_by_judge = float(sc or 0) < 1
        od = bool(
            wrong_by_judge
            and r.get("status") == "completed"
            and date_equivalent(r.get("gold"), r.get("answer"))
        )
        hit = None
        if wrong_by_judge and r.get("status") == "completed" and r.get("scale") == "binary":
            hit = check_conventions(r.get("question") or "", r.get("answer") or "", r.get("gold"))
        row = dict(
            ok=int(ok),
            a=r.get("answer") or "",
            jr=meta.get("judge_raw"),
            sc=sc,
            sd=sd,
            sv=sv,
            cv=meta.get("convention") or r.get("convention") or (hit.rule if hit else None),
            od=int(od) if od else None,
            ov=1 if hit else None,
            rt=bool(meta.get("retry_refusal")),
            ra=meta.get("retry_accepted") if meta.get("retry_refusal") else None,
            fa=meta.get("first_answer") if meta.get("retry_refusal") else None,
            ct=r.get("context_tokens"),
            trunc=bool(r.get("context_truncated")),
            err=r.get("error"),
            pt=r.get("prompt_tokens"),
            cpt=r.get("completion_tokens"),
            lat=[
                round(r.get(k) or 0)
                for k in ("latency_retrieve_ms", "latency_answer_ms", "latency_judge_ms")
            ],
            mc=r.get("model_calls"),
            rta=meta.get("retry_answer") if meta.get("retry_refusal") else None,
            fpt=meta.get("first_prompt_tokens"),
            rpt=meta.get("retry_prompt_tokens"),
            qv=meta.get("qa_variant"),
            rraw=meta.get("reader_raw"),
            rdec=meta.get("decisions"),
            tfl=meta.get("trace_full"),
        )
        row["dchg"] = (sd is not None and sd != sc) if sd is not None else bool(od)
        row["vchg"] = (sv is not None and sv != sc) if sv is not None else bool(hit)
        return row

    loc_c: dict[str, dict[str, dict]] = {}
    for rid, run in loc_runs.items():
        loc_c[rid] = {k: loc_row(r, k) for k, r in run["rows"].items() if k in loc_q}

    # ---- LoCoMo comparisons
    cat_of = {k: v["cc"] for k, v in loc_q.items()}

    def cmp_loc(rid: str) -> dict | None:
        run = loc_runs[rid]
        if not run["rows"]:
            return None
        new, base = loc_c[rid], loc_c[LOC_REF]
        qs = [k for k in new if k in base]
        if not qs:
            return None
        g = [k for k in qs if new[k]["ok"] and not base[k]["ok"]]
        lo = [k for k in qs if base[k]["ok"] and not new[k]["ok"]]
        cats = []
        for c in CAT_ORDER:
            cq = [k for k in qs if cat_of[k] == c]
            if cq:
                cats.append(
                    dict(
                        c=c,
                        name=CAT_NAME[c],
                        n=len(cq),
                        new=round(100 * sum(new[k]["ok"] for k in cq) / len(cq), 2),
                        ref=round(100 * sum(base[k]["ok"] for k in cq) / len(cq), 2),
                        g=sum(1 for k in g if cat_of[k] == c),
                        l=sum(1 for k in lo if cat_of[k] == c),
                    )
                )
        c14 = [k for k in qs if cat_of[k] != "cat5"]
        return dict(
            n=len(qs),
            acc_new=round(100 * sum(new[k]["ok"] for k in qs) / len(qs), 2),
            acc_ref=round(100 * sum(base[k]["ok"] for k in qs) / len(qs), 2),
            g=len(g),
            l=len(lo),
            net=len(g) - len(lo),
            band=round(0.328 * math.sqrt(len(qs)), 2),
            cats=cats,
            c14=dict(
                n=len(c14),
                g=sum(1 for k in g if k in set(c14)),
                l=sum(1 for k in lo if k in set(c14)),
            ),
            cross_check=None,
        )

    opb_sub_ref = opb_subs(opb_ref["dir"])
    opb_sub_base = opb_subs(opb_base["dir"])

    def cmp_opb(rid: str) -> dict | None:
        run = opb_runs[rid]
        new = opb_subs(run["dir"])
        if not (new and opb_sub_ref):
            return None
        if ALL_CONVS and run["n"] != opb_ref["n"]:
            return None  # different probe sets: the official aggregates are not comparable
        subs = {
            k: dict(new=new[k], ref=opb_sub_ref[k], d=round(new[k] - opb_sub_ref[k], 2))
            for k in eval_screen.SUBS
            if k in new and k in opb_sub_ref
        }
        key = "official_overall"
        return dict(
            subs=subs,
            overall=dict(
                new=new[key], ref=opb_sub_ref[key], d=round(new[key] - opb_sub_ref[key], 2)
            ),
        )

    screen_out = []
    for sid in screens:
        sr = sruns[sid]
        e = dict(
            id=sid, desc=SCREEN_DESC.get(sid, ""), loc=None, opb=None, cmp_loc=None, cmp_opb=None
        )
        for kind in ("loc", "opb"):
            run = sr[kind]
            if run:
                e[kind] = dict(id=run["id"], status=run["status"], n=run["n"], exp=run["exp"])
        if sr["loc"] and sr["loc"]["status"] == "complete":
            e["cmp_loc"] = cmp_loc(sr["loc"]["id"])
        if sr["opb"] and sr["opb"]["status"] == "complete":
            e["cmp_opb"] = cmp_opb(sr["opb"]["id"])
        deltas, guard = [], True
        if e["cmp_loc"]:
            deltas.append(e["cmp_loc"]["acc_new"] - e["cmp_loc"]["acc_ref"])
            if e["cmp_loc"]["net"] < -e["cmp_loc"]["band"]:
                guard = False
        if e["cmp_opb"]:
            d = e["cmp_opb"]["overall"]["d"]
            deltas.append(d)
            if d < -eval_screen.OPB_BAND:
                guard = False
        running = any(sr[k] and sr[k]["status"] != "complete" for k in ("loc", "opb"))
        if running:
            e["verdict"] = "RUNNING" + (
                " (partial: " + ", ".join(k for k in ("loc", "opb") if e["cmp_" + k]) + " done)"
                if deltas
                else ""
            )
        elif deltas:
            macro = sum(deltas) / len(deltas)
            e["macro"] = round(macro, 2)
            e["guard"] = guard
            v = (
                "ADOPT-CANDIDATE"
                if guard and macro > 1.0
                else ("REJECT (guard)" if not guard else "NEUTRAL")
            )
            e["verdict"] = v + ("" if len(deltas) == 2 else " (one benchmark only)")
        else:
            e["verdict"] = "NO RESULTS"
        screen_out.append(e)

    # cross-check against eval_screen for completed LoCoMo screens (same rule, same numbers)
    for e in screen_out:
        if e["cmp_loc"] and e["loc"]["status"] == "complete" and not ALL_CONVS:
            rid = e["loc"]["id"]
            ref_m = eval_screen.locomo(LOC_REF)
            path = RUNS / f"{rid}--memspine" / "results.jsonl"
            new_m = {}
            for line in path.open(encoding="utf-8"):
                d = json.loads(line)
                if d.get("kind") == "result":
                    sc = d.get("score_date_checked", d.get("score"))
                    new_m[d["query_id"]] = (
                        float(sc or 0) >= 1
                        and d["status"] == "completed"
                        and bool((d.get("answer") or "").strip())
                    )
            qs = [q for q in new_m if q in ref_m]
            gg = sum(1 for q in qs if new_m[q] and not ref_m[q][0])
            ll = sum(1 for q in qs if ref_m[q][0] and not new_m[q])
            assert (gg, ll) == (e["cmp_loc"]["g"], e["cmp_loc"]["l"]), (rid, gg, ll, e["cmp_loc"])
            e["cmp_loc"]["cross_check"] = "matches eval_screen.py"

    # ---- gate table
    def cells(run_id: str, rows: dict, fxd: dict | None, base_rows: dict, gate) -> dict | str:
        run = dict(id=run_id, manifest=(loc_runs.get(run_id) or {}).get("manifest"))
        if not gate["logged"](run):
            return "nl"
        t = h = b = ok_t = 0
        for k, row in rows.items():
            flag = gate["fn"](row, (fxd or {}).get(k))
            if flag:
                t += 1
                ok_t += row["ok"]
                bo = base_rows.get(k)
                if bo is not None:
                    h += int(row["ok"] and not bo["ok"])
                    b += int(bo["ok"] and not row["ok"])
        return dict(t=t, h=h, b=b, a=round(100 * ok_t / t, 1) if t else None)

    gate_rows = []
    gate_flags: dict[str, dict[str, list[str]]] = collections.defaultdict(
        lambda: collections.defaultdict(list)
    )
    for gate in GATES:
        entry = dict(id=gate["id"], label=gate["label"], desc=gate["desc"], base=None, cells={})
        for rid in loc_ids:
            c = cells(rid, loc_c[rid], fx.get(rid), loc_c[LOC_REF], gate)
            if rid == LOC_REF:
                entry["base"] = c
            else:
                entry["cells"].setdefault(rid, {})["loc"] = c
            run = dict(id=rid, manifest=(loc_runs.get(rid) or {}).get("manifest"))
            if gate["logged"](run):
                for k, row in loc_c[rid].items():
                    if gate["fn"](row, (fx.get(rid) or {}).get(k)):
                        gate_flags[rid][k].append(gate["id"])
        gate_rows.append(entry)

    # OP-Bench rows
    opb_c: dict[str, dict[str, dict]] = {}
    for rid, run in opb_runs.items():
        rows = {}
        for r in run["rows"].values():
            qid = r.get("query_id")
            m = opb_meta.get(qid)
            if m is None:
                continue
            ids = r.get("retrieved_ids") or []
            ps = persona_share(ids, m["qm"])
            rows[qid] = clean(
                dict(
                    s=r.get("score"),
                    ok=int(
                        float(r.get("score") or 0) >= OPB_PASS and r.get("status") == "completed"
                    ),
                    a=r.get("answer") or "",
                    ps=None if ps is None else round(ps, 3),
                    ct=r.get("context_tokens"),
                    n=len(ids),
                    ctx=ids,
                    jr=(r.get("meta") or {}).get("judge_raw"),
                    st=r.get("status"),
                    pt=r.get("prompt_tokens"),
                    cpt=r.get("completion_tokens"),
                    tfl=(r.get("meta") or {}).get("trace_full"),
                )
            )
        opb_c[rid] = rows
    reads_seen: dict[str, int] = {}
    for rid, rows_c in list(loc_c.items()) + list(opb_c.items()):
        for rd in jlz(RUNS / f"{rid}--trace" / "reads.jsonl"):
            key = (
                f"{rd.get('item_id')}:{rd.get('query_id')}" if rid in loc_c else rd.get("query_id")
            )
            row = rows_c.get(key)
            if row is None:
                continue
            row["tfl"] = clean(
                dict(
                    reader=rd.get("reader_calls"),
                    judge=rd.get("judge_calls"),
                    qdate=rd.get("question_date"),
                    vraw=(rd.get("verdict") or {}).get("raw"),
                    vmeta=(rd.get("verdict") or {}).get("meta"),
                    rmeta=rd.get("reader_meta"),
                )
            )
            reads_seen[rid] = reads_seen.get(rid, 0) + 1
    for entry in gate_rows:
        if entry["id"] == "relgate":
            base_rows = opb_c[OPB_REF]

            def closed(row):
                return (row.get("ct") or 0) == 0 or row.get("n", 1) == 0

            for rid in opb_ids:
                if rid == OPB_BASE:
                    continue
                rows = opb_c[rid]
                t = h = b = ok_t = 0
                for k, row in rows.items():
                    if closed(row):
                        t += 1
                        ok_t += row["ok"]
                        bo = base_rows.get(k)
                        if bo is not None and rid != OPB_REF:
                            h += int(row["ok"] and not bo["ok"])
                            b += int(bo["ok"] and not row["ok"])
                        gate_flags[rid][k].append("relgate")
                cell = dict(t=t, h=h, b=b, a=round(100 * ok_t / t, 1) if t else None)
                if rid == OPB_REF:
                    entry["base_opb"] = cell
                else:
                    entry["cells"].setdefault(rid, {})["opb"] = cell

    # funnel per LoCoMo run
    funnel = {}
    for rid in loc_ids:
        f = fx.get(rid)
        if not f:
            continue
        stg = collections.defaultdict(lambda: [0, 0])
        for k, c in f.items():
            if c["fs"] is None or k not in loc_c[rid]:
                continue
            stg[c["fs"]][0] += 1
            stg[c["fs"]][1] += loc_c[rid][k]["ok"]
        funnel[rid] = {
            s: dict(n=v[0], acc=round(100 * v[1] / v[0], 1) if v[0] else None)
            for s, v in stg.items()
        }

    # ---- failure matrix (baseline LoCoMo)
    mrows = collections.OrderedDict()
    for x in cat_rows.values():
        code = x["failure_code"]
        key = (x["failure_stage"], code)
        r = mrows.setdefault(
            key,
            dict(
                stage=x["failure_stage"],
                code=code,
                label=x["sub_type"],
                cells=collections.Counter(),
                total=0,
            ),
        )
        c = next((c for c, n in CAT_NAME.items() if n == x["category"]), x["category"])
        r["cells"][c] += 1
        r["total"] += 1
    matrix = dict(
        cats=[
            dict(
                c=c,
                name=CAT_NAME[c],
                n=sum(1 for v in loc_q.values() if v["cc"] == c),
                wrong=sum(1 for x in cat_rows.values() if x["category"] == CAT_NAME[c]),
            )
            for c in CAT_ORDER
        ],
        rows=sorted(
            [dict(r, cells=dict(r["cells"])) for r in mrows.values()],
            key=lambda r: (r["stage"], r["code"]),
        ),
        stages=STAGE_NAME,
    )
    opm = collections.OrderedDict()
    for x in opb_cat.values():
        r = opm.setdefault(
            x["failure_code"], dict(code=x["failure_code"], cells=collections.Counter(), total=0)
        )
        r["cells"][x["category"]] += 1
        r["total"] += 1
    op_types = sorted(
        {
            m["qm"]["task"] + ("/" + m["qm"]["subtype"] if m["qm"].get("subtype") else "")
            for m in opb_meta.values()
        }
    )
    opmatrix = dict(
        types=[
            dict(
                t=t,
                n=sum(
                    1
                    for m in opb_meta.values()
                    if (
                        m["qm"]["task"]
                        + ("/" + m["qm"]["subtype"] if m["qm"].get("subtype") else "")
                    )
                    == t
                ),
            )
            for t in op_types
        ],
        rows=sorted(
            [dict(r, cells=dict(r["cells"])) for r in opm.values()], key=lambda r: -r["total"]
        ),
    )

    # ---- pipeline reconstruction material: templates, per-run settings, offline signals
    def reader_tpl(manifest: dict | None) -> dict | None:
        labels = (manifest or {}).get("labels") or {}
        name = labels.get("qa_prompt")
        obj = RD.QA_PROMPTS.get(name) or RD.SYSTEM_QA_PROMPTS.get(name)
        if obj is None:
            return None
        if isinstance(obj, str):
            return dict(name=name, kind="str", text=obj)
        if isinstance(obj, RD.RoutedQAPrompt):
            shape = "generic" if obj.shape_fn is RD.generic_qa_shape else "qa"
            return dict(name=name, kind="routed", variants=obj.variants, shape=shape)
        return dict(
            name=name, kind="system", system=obj.system, with_context=obj.with_context,
            without_context=obj.without_context,
        )  # fmt: skip

    tpl_reader: dict[str, dict] = {}
    tpl_judge: dict[str, dict] = {}
    tpl_prompts: dict[str, dict] = {}
    runinfo: dict[str, dict] = {}
    opb_prompts: dict[str, str] = {}
    with contextlib.suppress(Exception):
        opb_prompts = OB.load_judge_prompts(HERE / "data" / "opbench_src")
    all_runs = {**loc_runs, **opb_runs}
    for rid, run in all_runs.items():
        mf = run.get("manifest") or {}
        labels = mf.get("labels") or {}
        sysc = ((mf.get("system") or {}).get("config") or {}).get("config") or {}
        top = (mf.get("system") or {}).get("config") or {}
        rdr = (mf.get("reader") or {}).get("params") or {}
        jdg = mf.get("judge") or {}
        info = dict(
            system=(mf.get("system") or {}).get("system_id"),
            reader_model=(mf.get("reader") or {}).get("model"),
            reader_id=(mf.get("reader") or {}).get("reader_id"),
            judge_id=jdg.get("judge_id"),
            suite=(jdg.get("params") or {}).get("suite"),
            qa_prompt=labels.get("qa_prompt"),
            dated=top.get("dated_rendering"),
            read_mode=top.get("read_mode"),
            build_sleep=top.get("build_sleep"),
            batch_turns=top.get("batch_turns"),
            embedding=sysc.get("embedding"),
            read_cfg={
                k: v
                for k, v in (sysc.get("read") or {}).items()
                if k
                in (
                    "rerank", "rerank_model", "candidate_pool", "rerank_keep", "rerank_floor",
                    "list_mode", "relevance_gate", "speaker_vote_mode", "assembly",
                    "replay_window_before", "replay_window_after", "abstain_on_raw",
                )
            },
            perspective=bool(
                json.dumps(sysc).find("perspective") >= 0
                and (sysc.get("memories", {}).get("episodic", {}).get("policies", {}).get("perspective"))
            ),
            retry=dict(on=bool(rdr.get("retry_refusal")), mode=rdr.get("retry_mode")),
            post=[k for k in ("count_verify", "date_repair", "verify_answer", "judge_date_check", "judge_conventions") if labels.get(k)],
            judge_guards=bool(labels.get("judge_guards")),
            no_memory_prompt=bool(labels.get("no_memory_prompt")),
            budget=((mf.get("protocol") or {}).get("budget_tokens")),
            top_k=((mf.get("protocol") or {}).get("top_k")),
            trace_full=bool(labels.get("trace_full")),
            max_tokens=rdr.get("max_tokens"),
            temperature=rdr.get("temperature"),
        )  # fmt: skip
        t = reader_tpl(mf)
        if t:
            tpl_reader[t["name"]] = t
            info["reader_tpl"] = t["name"]
        if info["retry"]["on"] and info["retry"]["mode"] in RF.RETRY_MODES:
            info["retry"]["instruction"] = RF.RETRY_MODES[info["retry"]["mode"]]
        suite = info.get("suite")
        if suite in JP.JUDGE_SUITES:
            st = JP.JUDGE_SUITES[suite]
            tpl_judge[suite] = dict(routes=dict(st.routes), notes=st.notes)
            for pid in st.routes.values():
                jp = JP.JUDGE_PROMPTS.get(pid)
                if jp is not None and jp.text is not None:
                    tpl_prompts[pid] = dict(
                        text=jp.text,
                        system=jp.system,
                        fields=jp.fields,
                        parse=jp.parse,
                        source=jp.source,
                    )
        runinfo[rid] = info
    if opb_prompts:
        for route, text in opb_prompts.items():
            tpl_prompts[f"opbench/{route}"] = dict(
                text=text,
                system=None,
                fields="opbench",
                parse="score",
                source="OP-Bench src/opbench/prompts.py (local checkout)",
            )
    no_memory_tpl = RD.NO_MEMORY_QA_PROMPT

    # per-question: query analysis (offline rule code) and the judge route per suite
    from memspine_evals.contracts import Query as _Q

    qa_of: dict[str, dict] = {}
    route_of: dict[str, dict] = {}
    suites_used = {i["suite"] for i in runinfo.values() if i.get("suite") in JP.JUDGE_SUITES}
    for k, q in loc_q.items():
        a = TF.query_analysis(q["q"])
        a["flags"] = [n for n, v in a["flags"].items() if v is True]
        a.pop("split_intents", None)
        qa_of[k] = a
        fake = _Q(
            query_id=q["id"],
            text=q["q"],
            gold="x",
            type_label=q["cc"],
            meta={
                "abstention": q["adv"],
                "adversarial": q["adv"],
                "category": int(q["cc"][3:]),
                "benchmark": "locomo",
                "judge_evidence": "",
            },
        )
        route_of[k] = {}
        for su in suites_used:
            with contextlib.suppress(Exception):
                route_of[k][su] = JP.JUDGE_SUITES[su].router(fake)
    for qid, m in opb_meta.items():
        a = TF.query_analysis(
            opb_runs[OPB_REF]["rows"].get(f"{m['item']}:{qid}", {}).get("question") or ""
        )
        a["flags"] = [n for n, v in a["flags"].items() if v is True]
        a.pop("split_intents", None)
        qa_of[qid] = a
        task = m["qm"]["task"]
        route_of[qid] = {
            "opbench": "irrelevance"
            if task.startswith("irrelevance")
            else (
                OB.sycophancy_route(m["qm"].get("subtype")) if task == "sycophancy" else "diversity"
            )
        }

    # write side: deterministic signals per stored turn (offline, labelled reconstructed)
    wsig: dict[str, dict] = {}
    for conv_id, turns in tt.items():
        wsig[conv_id] = {}
        for tid, t in turns.items():
            w = TF.write_signals(f"{t[0]}: {t[1]}")
            wsig[conv_id][tid] = clean(
                dict(
                    i=int(w["instruction_shaped"]) or None, ie=int(w["instruction_extended"]) or None,
                    sr=w["semantic_risk"] or None, pii=w["pii"] or None,
                    sg=w["sensitivity"]["grade"] if w["sensitivity"]["grade"] != "none" else None,
                    sc=w["sensitivity"]["categories"] or None, n=w["validation"]["chars"],
                )
            )  # fmt: skip

    # --trace-full write trace of a run, when it kept one (future runs)
    wtrace: dict[str, dict] = {}
    derived: dict[str, dict] = {}
    for rid in loc_ids:
        for row in jlz(RUNS / f"{rid}--trace" / "write_trace.jsonl"):
            if row.get("derived"):
                derived.setdefault(rid, {}).setdefault(row.get("item"), []).append(row)
            else:
                wtrace.setdefault(rid, {}).setdefault(row.get("item"), {})[row["turn"]] = row

    # ---- pipeline coverage per run: which step is logged (L), reconstructed (R) or missing (M)
    def any_row(rid, pred):
        rows = loc_c.get(rid) or opb_c.get(rid) or {}
        return any(pred(r) for r in rows.values())

    def any_fx(rid, key):
        return any(c.get(key) is not None for c in (fx.get(rid) or {}).values())

    def coverage(rid: str, kind: str) -> dict[str, list]:
        info = runinfo.get(rid, {})
        has_fx = rid in fx
        traced = any_row(rid, lambda r: r.get("tfl"))
        wtr = bool(wtrace.get(rid))
        xcuts = any(
            (c.get("xf") or {}).get("cuts") is not None for c in (fx.get(rid) or {}).values()
        )
        xwin = any(
            (c.get("xf") or {}).get("window") is not None for c in (fx.get(rid) or {}).values()
        )
        xf = any(c.get("xf") for c in (fx.get(rid) or {}).values())
        wt_logged = any(v.get("wt") for v in ing.get(rid, {}).values())
        step = {}
        loc = kind == "loc"
        step["W1 input turn"] = ["L", "dataset + ingest.jsonl source_text"]
        step["W2 validation"] = ["R", "offline: empty / length checks"]
        step["W3 firewall decision"] = [
            "L" if (loc or wtr) else "R",
            (
                "firewall signals and verdict logged per write (write_trace.jsonl engine_events)"
                if wtr
                else "quarantined / trust / status stamped on the record (ingest.jsonl); scores of the embedding-outlier and MINJA signals and the verdict reasons are not persisted (fix: engine hook; --trace-full logs the stamp, instruction_flag and deterministic screens)"
                if loc
                else "no ingest log for OP-Bench runs; deterministic screens reconstructed"
            ),
        ]
        step["W4 redaction / PII"] = [
            "L" if loc else "R",
            "text_identical (source vs stored text) is logged; PII hits reconstructed offline",
        ]
        step["W5 tags (perspective, sensitivity, src, trust)"] = [
            "L" if wtr else "M",
            "write_trace.jsonl (--trace-full)"
            if wtr
            else "record tags are not in ingest.jsonl; fix: --trace-full (write_trace.jsonl)"
            + (
                "; perspective tagging is ON in this config, tags reconstructable"
                if info.get("perspective")
                else "; perspective tagging is off in this config, so none are written"
            ),
        ]
        step["W6 embedding"] = ["L", "model and dim from the manifest (no vector dump by design)"]
        step["W7 vector + lexical index"] = [
            "L",
            "implied by written=true; index settings from the manifest",
        ]
        step["W8 dedup / conflict verdict"] = [
            "L" if wtr else "M",
            "conflict-ladder verdict, rule and incumbent logged for semantic writes (episodic turns have no ladder)"
            if wtr
            else "ADD/UPDATE/NOOP/CONTEST is not logged in this run; fix: --trace-full (engine write sink)",
        ]
        step["W9 write timings"] = [
            "L" if wt_logged else "M",
            "I73 write_timers ride the first ingest line of a batch"
            if wt_logged
            else "observability.write_timers is off in this run; fix: enable it in the engine config",
        ]
        step["W10 derived (sleep-cycle) records"] = [
            "L" if (info.get("build_sleep") is False or wtr) else "M",
            "sleep cycle off (build_sleep=false): none were derived"
            if info.get("build_sleep") is False
            else (
                "derived rows in write_trace.jsonl"
                if wtr
                else "sleep on: derived records are not logged; fix: --trace-full (write_trace.jsonl derived rows)"
            ),
        ]
        step["R1 query analysis"] = [
            "L" if xf else "R",
            "shape flags from the engine rule code run offline on the question text"
            + (
                ""
                if xf
                else "; resolved perspective / entity check / planner mode: not logged (fix: --trace-full)"
            ),
        ]
        have_dec = has_fx_key(dict(id=rid), "dec") if loc else False
        step["R2 gates / deciders"] = [
            "L" if (have_dec or xf) else "R",
            "decisions + I74 bypass logged"
            if have_dec
            else "no decider task and no relevance gate in this config: nothing to log (trace_full present)"
            if xf
            else (
                "empty-context flag derived from the context; decider decisions / bypass not logged in this run (older commit; fix: rerun on a current commit)"
                if loc
                else "context_tokens==0 is the only gate signal (no stage log for OP-Bench)"
            ),
        ]
        step["R3 retrieval legs"] = [
            "L" if has_fx else "M",
            "forensics.jsonl (top 60, shown top 30)"
            if has_fx
            else "no forensics stage log; fix: run with MEMSPINE_FORENSICS_DIR set",
        ]
        step["R4 fusion (RRF)"] = [
            "L" if has_fx else "M",
            "fused list with scores" if has_fx else "no forensics stage log",
        ]
        step["R5 rerank pool + scores"] = [
            "L" if has_fx else "M",
            (
                "scores as logged by the engine (one scale); raw vs min-max: "
                + (
                    "raw logged (abstain_on_raw)"
                    if xf
                    else "only one kind is logged; fix: --trace-full"
                )
            )
            if has_fx
            else "no forensics stage log",
        ]
        step["R6 assembly (floors, budget, dedupe, owner)"] = [
            "L" if xcuts else ("R" if has_fx else "M"),
            "final hits vs context lines vs budget derived; cut reasons / dedupe drops / owner notes "
            + ("logged only with --trace-full" if not xf else "logged (trace_full block)"),
        ]
        step["R7 replay window expansion"] = [
            "L" if xwin else ("R" if has_fx else "M"),
            "context lines that are not final hits, attached to the nearest hit",
        ]
        step["R8 rendered context"] = [
            "L" if traced else ("R" if (has_fx or not loc) else "M"),
            "the exact memory lines are inside the logged reader prompt (step 9)"
            if traced
            else "logged record texts + the adapter render rules (dated prefix, chronological order)",
        ]
        step["R9 exact reader prompt"] = [
            "L" if traced else ("R" if info.get("reader_tpl") else "M"),
            "meta.trace_full"
            if traced
            else "template id + context + question (+ date); fix for an exact copy: --trace-full",
        ]
        step["R10 reader raw output + retry"] = [
            "L",
            "answer; retry first/second answers and accepted flag are in the row meta"
            + (
                ""
                if traced
                else "; raw reply of non-extracting prompts equals the answer; retry prompt reconstructed (question + retry instruction)"
            ),
        ]
        step["R11 judge (prompt, raw reply, verdict)"] = [
            "L" if traced else "R",
            "meta.trace_full"
            if traced
            else "prompt reconstructed from the suite template; raw reply truncated to 200 chars in the row (meta.judge_raw); fix for full text: --trace-full",
        ]
        step["R12 final score"] = ["L", "results.jsonl"]
        return step

    coverage_out = {rid: coverage(rid, "loc" if rid in loc_runs else "opb") for rid in all_runs}

    # ---- questions
    gaps_used = set()
    qlist = []
    run_order_loc = loc_ids
    screens_loc = [s for s in screens if sruns[s]["loc"]]
    screens_opb = [s for s in screens if sruns[s]["opb"]]

    def outcome(base_ok, run_rows, k):
        r = run_rows.get(k)
        if r is None:
            return "-"
        if r["ok"] and not base_ok:
            return "F"
        if base_ok and not r["ok"]:
            return "B"
        return "R" if r["ok"] else "W"

    for k, q in loc_q.items():
        base = loc_c[LOC_REF][k]
        cat = cat_rows.get(k)
        rr = {}
        for rid in run_order_loc:
            row = loc_c[rid].get(k)
            if row is None:
                continue
            f = (fx.get(rid) or {}).get(k)
            rec = dict(row)
            for dropk in ("dchg", "vchg", "trunc"):
                rec.pop(dropk, None)
            if row["trunc"]:
                rec["trunc"] = 1
            if f:
                rec.update(
                    g=f["g"],
                    fs=f["fs"],
                    ctx=f["ctx"],
                    fin=f["fin"],
                    legs=f["legs"] or None,
                    dec=f["dec"],
                    rb=f["rb"],
                    mx=f["mx"],
                    ce=int(f["ce"]),
                    tr=f["tr"],
                    tok=f["tok"],
                    xf=f["xf"],
                )
            rec["fl"] = gate_flags[rid].get(k) or None
            rr[rid] = clean({kk: v for kk, v in rec.items() if v not in ([],) and v is not False})
        o = "".join(
            outcome(base["ok"], loc_c[loc_runs_id], k)
            for loc_runs_id in [sruns[s]["loc"]["id"] for s in screens_loc]
        )
        gaps = list(cat["gap_ids"]) if cat else []
        gaps_used.update(gaps)
        qlist.append(
            clean(
                dict(
                    k=k,
                    b="loc",
                    it=q["item"],
                    id=q["id"],
                    cat=CAT_NAME.get(q["cc"], q["cc"]),
                    cc=q["cc"],
                    q=q["q"],
                    g=loc_runs[LOC_REF]["rows"][k].get("gold"),
                    ev=q["ev"],
                    dv=q["dv"] or None,
                    ok=base["ok"],
                    st=cat["failure_stage"] if cat else None,
                    code=cat["failure_code"] if cat else None,
                    sub=cat["sub_type"] if cat else None,
                    gaps=gaps or None,
                    fix=cat.get("generic_fix") if cat else None,
                    mech=cat.get("decision_mechanism") if cat else None,
                    feat=cat.get("built_feature") if cat else None,
                    cost=cat.get("fix_runtime_cost") if cat else None,
                    evd=cat.get("evidence") if cat else None,
                    errs=errata.get(k) or None,
                    errc=cat.get("errata_candidate") if cat else None,
                    o=o,
                    qa=qa_of.get(k),
                    jrt=route_of.get(k),
                    r=rr,
                )
            )
        )

    for qid, m in opb_meta.items():
        # union of probes: the baseline row, else the first run that has one
        row = opb_c[OPB_REF].get(qid) or next(
            (opb_c[r][qid] for r in opb_ids if qid in opb_c.get(r, {})), None
        )
        if row is None:
            continue
        cat = opb_cat.get(qid)
        sig = opb_sig.get(qid)
        gaps = list(cat["gap_ids"]) if cat else []
        gaps_used.update(gaps)
        rr = {}
        for rid in opb_ids:
            rw = opb_c[rid].get(qid)
            if rw is not None:
                rec_o = dict(rw, fl=gate_flags[rid].get(qid) or None)
                f = (fx.get(rid) or {}).get(qid)
                if f:
                    rec_o.update(
                        fs=f["fs"], fin=f["fin"], legs=f["legs"] or None, dec=f["dec"], rb=f["rb"],
                        mx=f["mx"], ce=int(f["ce"]), tr=f["tr"], tok=f["tok"], xf=f["xf"],
                    )  # fmt: skip
                rr[rid] = clean(rec_o)
        o = "".join(outcome(row["ok"], opb_c[sruns[s]["opb"]["id"]], qid) for s in screens_opb)
        qm = m["qm"]
        ty = qm["task"] + ("/" + qm["subtype"] if qm.get("subtype") else "")
        qlist.append(
            clean(
                dict(
                    k=qid,
                    b="opb",
                    it=m["item"],
                    id=qid,
                    cat=ty,
                    cc=qm["task"],
                    q=opb_runs[OPB_REF]["rows"][f"{m['item']}:{qid}"].get("question")
                    if f"{m['item']}:{qid}" in opb_runs[OPB_REF]["rows"]
                    else None,
                    ok=row["ok"],
                    st="x" if cat else None,
                    code=cat["failure_code"] if cat else None,
                    sub=cat["sub_type"] if cat else None,
                    gaps=gaps or None,
                    fix=cat.get("generic_fix") if cat else None,
                    mech=cat.get("decision_mechanism") if cat else None,
                    feat=cat.get("built_feature") if cat else None,
                    cost=cat.get("fix_runtime_cost") if cat else None,
                    sig=(
                        {
                            k2: sig.get(k2)
                            for k2 in (
                                "persona_share",
                                "leak_n",
                                "ref_cues",
                                "best_overlap",
                                "affirm",
                                "role_inv",
                                "class",
                            )
                        }
                        if sig
                        else None
                    ),
                    persona=m["persona"],
                    ho=None if m["dev"] else 1,
                    o=o,
                    qa=qa_of.get(qid),
                    jrt=route_of.get(qid),
                    r=rr,
                )
            )
        )

    # OP-Bench question text: fall back to any run row
    opb_text = {}
    for run in opb_runs.values():
        for r in run["rows"].values():
            opb_text.setdefault(r.get("query_id"), r.get("question"))
    for q in qlist:
        if q["b"] == "opb" and not q.get("q"):
            q["q"] = opb_text.get(q["id"])

    # ---- baseline scores
    k14 = [k for k in loc_q if loc_q[k]["cc"] != "cat5"]
    b = loc_c[LOC_REF]
    base_loc = dict(
        n=len(loc_q),
        correct=sum(v["ok"] for v in b.values()),
        wrong=sum(1 - v["ok"] for v in b.values()),
        c14_n=len(k14),
        c14_correct=sum(b[k]["ok"] for k in k14),
        cats=[
            dict(
                c=c,
                name=CAT_NAME[c],
                n=sum(1 for k in loc_q if loc_q[k]["cc"] == c),
                k=sum(b[k]["ok"] for k in loc_q if loc_q[k]["cc"] == c),
            )
            for c in CAT_ORDER
        ],
        convs=[
            dict(
                item=i,
                n=sum(1 for k in loc_q if loc_q[k]["item"] == i),
                k=sum(b[k]["ok"] for k in loc_q if loc_q[k]["item"] == i),
            )
            for i in dev_items
        ],
        conv_credit=sum(1 for k in loc_q if not b[k]["ok"] and b[k].get("ov")),
        errata_n=sum(1 for k in loc_q if k in errata),
        errata_wrong=sum(1 for k in loc_q if k in errata and not b[k]["ok"]),
    )
    base_opb = dict(
        subs=opb_sub_ref,
        base=opb_sub_base,
        n=len(opb_c[OPB_REF]),
        low=sum(1 for v in opb_c[OPB_REF].values() if not v["ok"]),
        base_low=sum(1 for v in opb_c[OPB_BASE].values() if not v["ok"]),
    )

    # ---- waterfall
    cats_n14 = base_loc["c14_n"]
    base14 = 100 * base_loc["c14_correct"] / cats_n14
    cum = base14
    lev_out = []
    scr_by_id = {s["id"]: s for s in screen_out}
    for lv in FULL_LEVERS if ALL_CONVS else LEVERS:
        meas = []
        for sid in lv["screens"]:
            s = scr_by_id.get(sid)
            if s and s["cmp_loc"] and s["loc"]["status"] == "complete":
                c14 = s["cmp_loc"]["c14"]
                meas.append(
                    dict(
                        screen=sid,
                        net14=c14["g"] - c14["l"],
                        g=c14["g"],
                        l=c14["l"],
                        n14=c14["n"],
                        verdict=s["verdict"],
                    )
                )
            elif s and s["loc"] and s["loc"]["status"] != "complete":
                meas.append(dict(screen=sid, running=True))
        gain = 100 * lv["q14"] / cats_n14
        lev_out.append(
            dict(
                lv,
                gain14=round(gain, 2),
                gain15=round(100 * lv["q15"] / base_loc["n"], 2),
                start=round(cum, 2),
                end=round(cum + gain, 2),
                measured=meas,
            )
        )
        cum += gain
    waterfall = dict(
        base14=round(base14, 2),
        target14=round(cum, 2),
        base15=round(100 * base_loc["correct"] / base_loc["n"], 2),
        target15=round(
            100
            * (
                base_loc["correct"]
                + sum(lv["q15"] for lv in (FULL_LEVERS if ALL_CONVS else LEVERS))
            )
            / base_loc["n"],
            2,
        ),
        levers=lev_out,
        doc=(
            "FULL_RUN_FORENSICS_2026-10-10.md section 5; expected gains are capture-rate assumptions on measured class sizes (counted levers only; 90 needs about twice that)"
            if ALL_CONVS
            else "FAILURE_FORENSICS_BASELINE_2026-10-10.md section 2; expected gains are capture-rate assumptions, measured where a screen has finished"
        ),
    )

    gaps_json = {g: gaps_all[g] for g in sorted(gaps_used) if g in gaps_all}
    for g in gaps_used - set(gaps_json):
        gaps_json[g] = dict(t="(gap row not found in GAP_REGISTER.md)", e="", s="", st="")

    # ---- data gaps (what older runs did not log)
    data_gaps = []
    for rid in loc_ids:
        f = fx.get(rid)
        if not f:
            data_gaps.append(
                f"{rid}: no forensics.jsonl (no gold-turn ranks, no context funnel; results-level data only)"
            )
            continue
        miss = [
            lbl
            for lbl, key in (
                ("decisions (list_mode / bridge decider)", "dec"),
                ("relevance_bypass (I74)", "rb"),
            )
            if not has_fx_key(dict(id=rid), key)
        ]
        if miss:
            data_gaps.append(f"{rid}: not logged: {', '.join(miss)}")
    data_gaps.append(
        "OP-Bench runs have no --forensics stage log: context is rebuilt from retrieved_ids and the dataset (no legs, no ranks, no date annotations); persona share is computed from the persona turn ids"
    )
    data_gaps.append(
        "Write side: ingest.jsonl exists only for LoCoMo runs; the memory view shows the baseline's stored records "
        "(OP-Bench runs write the same conversation but keep no ingest log, so OP-Bench uses the same records). "
        "Tags such as perspective or sensitivity appear only where an ingest row carries extra keys (none do today)."
    )
    data_gaps.append(
        "Retrieval trace lists are capped at the top 30 per leg (gold-turn ranks beyond 30 are still recorded in the rank table); "
        "the replay window expansion is not logged as such, it is inferred as context turns that are not in the final hit list"
    )
    data_gaps.append(
        "Retry-refusal fields (first_answer, retry_accepted) exist only for questions where the retry fired and only for runs with retry enabled"
    )
    data_gaps.append(
        "No run records date_check / score_conventions columns inline; the explorer recomputes both offline (CPU, deterministic) and labels them as such"
    )
    data_gaps.append(
        "Bridge gate: no run logs a bridge decision; the row is kept so a future logging fix shows up on rebuild"
    )

    meta = dict(
        generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
        dev_items=dev_items,
        all_convs=ALL_CONVS,
        split_dev=json.loads((ANALYSIS / "locomo_split.json").read_text(encoding="utf-8"))[
            "dev_items"
        ],
        split_dev_personas=json.loads(
            (ANALYSIS / "opbench_persona_split.json").read_text(encoding="utf-8")
        )["dev_items"],
        loc_ref=LOC_REF,
        opb_ref=OPB_REF,
        opb_base=OPB_BASE,
        n_loc=len(loc_q),
        n_opb=len(opb_c[OPB_REF]),
        loc_wrong=base_loc["wrong"],
        opb_low=base_opb["low"],
        loc_runs=loc_ids,
        opb_runs=opb_ids,
        screens_loc=[sruns[s]["loc"]["id"] for s in screens_loc],
        screens_opb=[sruns[s]["opb"]["id"] for s in screens_opb],
        screen_ids_loc=screens_loc,
        screen_ids_opb=screens_opb,
        data_gaps=data_gaps,
        budgets={
            rid: ((r.get("manifest") or {}).get("protocol") or {}).get("budget_tokens")
            for rid, r in list(loc_runs.items()) + list(opb_runs.items())
        },
        ingest_diff=ingest_diff,
        leg_cap=LEG_CAP,
        stages=STAGE_NAME,
        gap_note="Gap-row text is from analysis/GAP_REGISTER.md (the repo's own text, not dataset text).",
        licence="CONTAINS DATASET TEXT (LoCoMo, OP-Bench). OP-Bench has no licence: local use only, do not share or commit.",
    )

    return dict(
        meta=meta,
        screens=screen_out,
        base=dict(loc=base_loc, opb=base_opb),
        matrix=matrix,
        opmatrix=opmatrix,
        gates=dict(rows=gate_rows, loc_runs=meta["screens_loc"], opb_runs=meta["screens_opb"]),
        funnel=funnel,
        mem=mem,
        wsig=wsig,
        wtrace=wtrace,
        derived=derived,
        tpl=dict(reader=tpl_reader, judge=tpl_judge, prompts=tpl_prompts, no_memory=no_memory_tpl),
        runinfo=runinfo,
        coverage=coverage_out,
        waterfall=waterfall,
        gaps=gaps_json,
        tt=tt,
        q=qlist,
    )


# --------------------------------------------------------------------------------------------- output
def dumps(obj) -> str:
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return s.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def assert_ignored(path: Path) -> None:
    rel = path.resolve()
    if OUT_DIR.resolve() not in rel.parents:
        raise SystemExit(f"refusing to write dataset text outside {OUT_DIR}: {path}")
    r = subprocess.run(["git", "check-ignore", "-q", str(rel)], cwd=REPO, capture_output=True)
    if r.returncode != 0:
        raise SystemExit(f"{path} is not git-ignored; refusing to write dataset text there")


def render(data: dict) -> str:
    return EXPLORER_HTML.replace("__DATA__", dumps(data))


def summary_html(data: dict) -> str:
    """Text-free: ids, codes, counts, scores. No question, answer, gold or dialogue text."""
    e = html.escape
    m = data["meta"]
    out = [
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Forensics summary</title><style>{SUMMARY_CSS}</style></head><body>",
        f"<h1>Forensics summary (text-free)</h1><p class='n'>Generated {e(m['generated'])}. Counts, ids, codes and scores only: no question, answer, gold or dialogue text, safe to share under the OP-Bench policy (scores only). The per-question explorer stays local.</p>",
    ]
    bl, bo = data["base"]["loc"], data["base"]["opb"]
    out.append(
        f"<h2>Baseline</h2><p>LoCoMo dev ({e(m['loc_ref'])}): {bl['correct']}/{bl['n']} = {100 * bl['correct'] / bl['n']:.1f}% cat 1-5; cat 1-4 {bl['c14_correct']}/{bl['c14_n']} = {100 * bl['c14_correct'] / bl['c14_n']:.1f}%; {bl['wrong']} wrong.</p>"
    )
    out.append(
        "<table><tr><th>category</th><th>n</th><th>correct</th><th>accuracy</th></tr>"
        + "".join(
            f"<tr><td>{e(c['name'])}</td><td>{c['n']}</td><td>{c['k']}</td><td>{100 * c['k'] / c['n']:.1f}</td></tr>"
            for c in bl["cats"]
        )
        + "</table>"
    )
    out.append(
        "<table><tr><th>OP-Bench subscore (x100)</th><th>no-memory BASE</th><th>baseline</th></tr>"
        + "".join(
            f"<tr><td>{e(k)}</td><td>{(bo['base'] or {}).get(k, '-')}</td><td>{v}</td></tr>"
            for k, v in (bo["subs"] or {}).items()
        )
        + "</table>"
    )
    out.append(
        "<h2>Screens (paired delta against the baseline)</h2><table><tr><th>screen</th><th>LoCoMo run</th><th>+/- net (band)</th><th>acc new / ref</th><th>OP-Bench overall new / ref</th><th>verdict</th></tr>"
    )
    for s in data["screens"]:
        c, o = s["cmp_loc"], s["cmp_opb"]
        loc = f"{s['loc']['status']} {s['loc']['n']}/{s['loc']['exp']}" if s["loc"] else "-"
        out.append(
            f"<tr><td>{e(s['id'])}</td><td>{e(loc)}</td><td>{(f'+{c["g"]}/-{c["l"]} net {c["net"]:+d} (+-{c["band"]:.1f})') if c else '-'}</td>"
            f"<td>{(f'{c["acc_new"]:.1f} / {c["acc_ref"]:.1f}') if c else '-'}</td><td>{(f'{o["overall"]["new"]:.1f} / {o["overall"]["ref"]:.1f}') if o else '-'}</td><td>{e(s['verdict'])}</td></tr>"
        )
    out.append(
        "</table><h2>Failure stage x category (baseline LoCoMo, wrong answers)</h2><table><tr><th>stage</th><th>code</th>"
        + "".join(f"<th>{e(c['name'])}</th>" for c in data["matrix"]["cats"])
        + "<th>total</th></tr>"
    )
    for r in data["matrix"]["rows"]:
        out.append(
            f"<tr><td>{e(r['stage'])}</td><td>{e(r['code'])}</td>"
            + "".join(f"<td>{r['cells'].get(c['c'], 0) or ''}</td>" for c in data["matrix"]["cats"])
            + f"<td>{r['total']}</td></tr>"
        )
    out.append(
        "</table><h2>OP-Bench failure class x probe type (baseline, below 0.5)</h2><table><tr><th>class</th>"
        + "".join(f"<th>{e(t['t'])}<br>n={t['n']}</th>" for t in data["opmatrix"]["types"])
        + "<th>total</th></tr>"
    )
    for r in data["opmatrix"]["rows"]:
        out.append(
            f"<tr><td>{e(r['code'])}</td>"
            + "".join(
                f"<td>{r['cells'].get(t['t'], 0) or ''}</td>" for t in data["opmatrix"]["types"]
            )
            + f"<td>{r['total']}</td></tr>"
        )
    out.append(
        "</table><h2>Gates</h2><p class='n'>touched / helped / hurt per screen on the LoCoMo slice (helped and hurt are flips against the baseline among touched questions: association, not causation); nl = not logged.</p><table><tr><th>gate</th><th>baseline touched</th>"
    )
    sl = data["gates"]["loc_runs"]
    out.append("".join(f"<th>{e(s)}</th>" for s in sl) + "</tr>")
    for g in data["gates"]["rows"]:
        base = g["base"]
        out.append(
            f"<tr><td>{e(g['label'])}</td><td>{'nl' if base == 'nl' else base['t']}</td>"
            + "".join(
                "<td>"
                + (lambda c: "nl" if c in (None, "nl") else f"{c['t']} / +{c['h']} / -{c['b']}")(
                    g["cells"].get(s, {}).get("loc")
                )
                + "</td>"
                for s in sl
            )
            + "</tr>"
        )
    out.append(
        "</table><h2>Gap ids hit by baseline failures</h2><table><tr><th>gap id</th><th>failures</th></tr>"
    )
    cnt = collections.Counter(g for q in data["q"] if q.get("st") for g in q.get("gaps") or [])
    for g, n in cnt.most_common():
        out.append(f"<tr><td>{e(g)}</td><td>{n}</td></tr>")
    out.append(
        "</table><h2>Path to 89.6</h2><table><tr><th>#</th><th>lever</th><th>expected points cat 1-4</th><th>measured (screen: net q on cat 1-4 slice)</th></tr>"
    )
    for lv in data["waterfall"]["levers"]:
        ms = "; ".join(
            f"{x['screen']}: running" if x.get("running") else f"{x['screen']}: {x['net14']:+d}"
            for x in lv["measured"]
        ) or ("(" + lv["measured_note"] + ")" if lv.get("measured_note") else "not screened")
        out.append(
            f"<tr><td>{lv['n']}</td><td>{e(lv['name'])}</td><td>+{lv['gain14']:.2f}</td><td>{e(ms)}</td></tr>"
        )
    out.append(
        f"</table><p>{data['waterfall']['base14']:.1f}% to {data['waterfall']['target14']:.1f}% cat 1-4 (capture-rate assumptions, FAILURE_FORENSICS_BASELINE_2026-10-10.md section 2).</p></body></html>"
    )
    return "".join(out)


SUMMARY_CSS = "body{font:14px system-ui,sans-serif;margin:24px;max-width:1300px}table{border-collapse:collapse;margin:8px 0 18px}td,th{border:1px solid #ccc;padding:3px 8px;text-align:left}th{background:#f2f2f2}.n{color:#555}h2{margin-top:26px}"


def verify_text_free(page: str, data: dict) -> None:
    needles = []
    for q in data["q"]:
        for key in ("q", "g"):
            v = q.get(key)
            if isinstance(v, str) and len(v) >= 25:
                needles.append(v)
        for r in q["r"].values():
            a = r.get("a")
            if isinstance(a, str) and len(a) >= 25:
                needles.append(a[:60])
    low = page.lower()
    for n in needles:
        if html.escape(n).lower()[:60] in low or n.lower()[:60] in low:
            raise SystemExit("summary is not text-free; refusing to write it")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT_DIR / "forensics_explorer.html"))
    ap.add_argument("--summary", default=str(HERE / "reports" / "forensics_summary.html"))
    ap.add_argument("--no-summary", action="store_true")
    ap.add_argument("--extra-screen", action="append", default=[])
    ap.add_argument(
        "--all-convs",
        action="store_true",
        help="all 10 LoCoMo conversations, cat 1-4 (held-out guard off)",
    )
    ap.add_argument(
        "--loc-ref",
        default=None,
        help="baseline LoCoMo run id (default xb-loc-dev; full-persp-loc with --all-convs)",
    )
    ap.add_argument(
        "--opb-ref",
        default=None,
        help="baseline OP-Bench run id (default xb-opb-dev; full-persp-opb with --all-convs)",
    )
    ap.add_argument(
        "--extra-run",
        action="append",
        default=[],
        help="with --all-convs: a run id to compare (...-opb ids are OP-Bench)",
    )
    args = ap.parse_args()
    global ALL_CONVS, LOC_REF, OPB_REF, SIZE_LIMIT
    ALL_CONVS = bool(args.all_convs)
    if ALL_CONVS:
        SIZE_LIMIT = 400_000_000
        LOC_REF = args.loc_ref or "full-persp-loc"
        OPB_REF = args.opb_ref or "full-persp-opb"
        if not args.extra_run:
            args.extra_run = [
                "xb-loc-dev",
                "qa-full-qs-eq06-fix",
                "qa-full-qs-eq06-roff-fx",
                "xb-opb-dev",
            ]
    elif args.loc_ref:
        LOC_REF = args.loc_ref
    out = Path(args.out)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    assert_ignored(out)
    data = build(args)
    summary = None if args.no_summary else summary_html(data)
    if summary:
        verify_text_free(summary, data)
    page = render(data)
    if len(page.encode("utf-8")) > SIZE_LIMIT:
        # too big for one file: one explorer per benchmark plus an index page
        parts = {}
        for bench, label in (("loc", "LoCoMo"), ("opb", "OP-Bench")):
            sub = dict(data, q=[q for q in data["q"] if q["b"] == bench])
            fp = out.with_name(f"{out.stem}_{bench}.html")
            fp.write_text(render(sub), encoding="utf-8")
            parts[label] = fp
            print(f"explorer part: {fp} ({fp.stat().st_size / 1e6:.2f} MB)")
        links = "".join(f'<li><a href="{p.name}">{n}</a></li>' for n, p in parts.items())
        page = (
            "<!doctype html><meta charset='utf-8'><title>Forensics explorer index</title>"
            "<body style='font:14px system-ui;margin:24px'><h2>Forensics explorer (LOCAL, dataset text)</h2>"
            f"<ul>{links}</ul></body>"
        )
    out.write_text(page, encoding="utf-8")
    print(f"explorer: {out} ({out.stat().st_size / 1e6:.2f} MB)")
    print(
        f"  LoCoMo {data['meta']['n_loc']} q ({data['meta']['loc_wrong']} wrong), OP-Bench {data['meta']['n_opb']} probes ({data['meta']['opb_low']} below {OPB_PASS})"
    )
    for s in data["screens"]:
        print(
            f"  screen {s['id']:14s} loc={s['loc']['status'] + ' ' + str(s['loc']['n']) if s['loc'] else '-':16s} opb={s['opb']['status'] + ' ' + str(s['opb']['n']) if s['opb'] else '-':16s} {s['verdict']}"
        )
    if summary:
        sp = Path(args.summary)
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(summary, encoding="utf-8")
        tracked = (
            subprocess.run(
                ["git", "ls-files", "--error-unmatch", str(sp)], cwd=REPO, capture_output=True
            ).returncode
            == 0
        )
        ignored = (
            subprocess.run(
                ["git", "check-ignore", "-q", str(sp)], cwd=REPO, capture_output=True
            ).returncode
            == 0
        )
        print(
            f"summary: {sp} ({sp.stat().st_size / 1e3:.1f} KB) tracked={tracked} gitignored={ignored}"
        )


EXPLORER_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Forensics explorer (LOCAL, contains dataset text)</title>
<style>
:root{--bg:#fafafa;--fg:#1d1f23;--mut:#666;--bd:#d6d6d6;--ac:#2b5fb4;--g:#1b8a3a;--r:#c0392b;--o:#d9822b;--b:#e8f0fc}
*{box-sizing:border-box}body{margin:0;font:13px/1.4 system-ui,Segoe UI,sans-serif;background:var(--bg);color:var(--fg)}
header{background:#7a1f1f;color:#fff;padding:6px 14px;font-size:12px}header b{font-size:14px}
nav{display:flex;gap:2px;background:#222;padding:0 10px;position:sticky;top:0;z-index:5}
nav button{background:none;border:0;color:#ccc;padding:9px 14px;cursor:pointer;font:inherit}nav button.on{background:var(--bg);color:#000;font-weight:600}
main{padding:14px 18px 60px;max-width:1700px}
h2{margin:22px 0 6px;font-size:16px}h3{margin:14px 0 4px;font-size:13px}
table{border-collapse:collapse;margin:6px 0 12px;background:#fff}th,td{border:1px solid var(--bd);padding:3px 7px;vertical-align:top}th{background:#f0f0f0;text-align:left;position:sticky;top:0}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.cards{display:flex;flex-wrap:wrap;gap:10px}.card{background:#fff;border:1px solid var(--bd);border-radius:6px;padding:8px 14px;min-width:150px}.card b{font-size:20px;display:block}.card small{color:var(--mut)}
.mut{color:var(--mut)}.g{color:var(--g)}.r{color:var(--r)}.o{color:var(--o)}
.nl{background:repeating-linear-gradient(45deg,#f3f3f3,#f3f3f3 4px,#fff 4px,#fff 8px);color:#999;font-style:italic;font-size:11px}
.na{color:#bbb;text-align:center}
.pos{background:#dff3e3}.neg{background:#f8dcd8}.zero{background:#fff}
.v-ADOPT{background:#bfe8c8;font-weight:600}.v-REJECT{background:#f4c3bd;font-weight:600}.v-NEUTRAL{background:#eee}.v-RUNNING{background:#d9e8fb;font-weight:600}
.click{cursor:pointer}.click:hover{outline:2px solid var(--ac);outline-offset:-2px}
.tag{display:inline-block;padding:0 6px;border-radius:9px;font-size:11px;background:#e6e6e6;margin:1px}
.tag.e{background:#ffe2a8}.tag.s{background:#cfe0ff}.tag.gap{background:#e5dcf7;cursor:pointer}
.strip{display:inline-flex;gap:1px}.strip i{display:block;width:11px;height:14px;border-radius:2px}
.oF{background:#1b8a3a}.oB{background:#c0392b}.oR{background:#bfe3c8}.oW{background:#8a8a8a}.o-{background:#fff;border:1px solid #ddd}
.flt{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center;margin:6px 0 10px;background:#fff;border:1px solid var(--bd);padding:8px;border-radius:6px;position:sticky;top:38px;z-index:4}
.flt label{font-size:11px;color:var(--mut);display:flex;flex-direction:column}select,input[type=text]{font:inherit;padding:2px 4px}
tr.row{cursor:pointer}tr.row:hover td{background:#eef4ff}tr.sel td{background:#fff6d6}
.ok{color:var(--g);font-weight:700}.bad{color:var(--r);font-weight:700}
#detail{position:fixed;top:0;right:0;width:min(980px,92vw);height:100vh;background:#fff;border-left:3px solid var(--ac);box-shadow:-6px 0 18px #0003;overflow:auto;padding:12px 18px 60px;z-index:20}
#detail[hidden]{display:none}#detail h2{margin-top:6px}
.sec{border-top:1px solid var(--bd);margin-top:12px;padding-top:6px}
.box{background:#f6f6f6;border:1px solid var(--bd);padding:6px 8px;white-space:pre-wrap;word-break:break-word;border-radius:4px}
.ctx{font:12px/1.35 Consolas,monospace}.ctx div{padding:1px 4px;border-bottom:1px dotted #e4e4e4;white-space:pre-wrap;word-break:break-word}
.ctx .gold{background:#c9f0d2}.ctx .dist{background:#ffe0b5}.ctx .pers{background:#eaf1ff}
.ctx b.id{color:#555;font-weight:600}
.btn{border:1px solid var(--bd);background:#fff;border-radius:4px;padding:2px 8px;cursor:pointer;font:inherit}.btn:hover{background:var(--b)}
.legend span{margin-right:12px;white-space:nowrap}.legend i{display:inline-block;width:11px;height:12px;vertical-align:-2px;margin-right:3px;border-radius:2px}
.qlay{display:flex;gap:12px;align-items:flex-start}.side{flex:0 0 310px;position:sticky;top:42px;max-height:calc(100vh - 50px);overflow:auto;background:#fff;border:1px solid var(--bd);border-radius:6px;padding:6px 8px;font-size:12px}.qmain{flex:1;min-width:0}.side details{margin-left:10px}.side summary{cursor:pointer;padding:1px 0}.tl{cursor:pointer}.tl:hover{background:#eef4ff}.tl.on{background:#ffe9a8;font-weight:600}.tleaf{margin-left:14px}.leaves{margin-left:10px;line-height:1.8}.leaves a{font-size:11px;text-decoration:none;margin-right:2px}
table.trace td{min-width:170px;max-width:240px;font-size:11px}table.trace td.gold{background:#c9f0d2}table.trace td.dist{background:#ffe0b5}table.trace td.gp{background:#f4faf5}.runsel{font-weight:600}tr.sel td{background:#fff6d6}
.step{border:1px solid var(--bd);border-radius:4px;margin:4px 0;background:#fff}.step>summary{cursor:pointer;padding:4px 8px}.sb{padding:4px 10px 8px 22px}.timeline{border-left:3px solid #c9d6ee;padding-left:8px}.dot{display:inline-block;width:10px;height:10px;border-radius:5px;margin-right:4px}.dL{background:#1b8a3a}.dR{background:#d9a21b}.dM{background:#999}.bd{font-size:11px;border-radius:9px;padding:0 7px;margin-left:6px}.bL{background:#cfeed6}.bR{background:#ffe9a8}.bM{background:#e4e4e4;color:#555}td.cov{text-align:center;font-weight:700}td.cL{background:#cfeed6}td.cR{background:#ffe9a8}td.cM{background:#e4e4e4;color:#777}tr.wrow>td{background:#f7f9fd}
.warn{background:#fff3cd;border:1px solid #e6cf7a;padding:6px 10px;border-radius:4px;margin:8px 0}
svg text{font:11px system-ui,sans-serif}
</style></head><body>
<header><b>Forensics explorer</b> &nbsp; LOCAL FILE: contains LoCoMo and OP-Bench text (OP-Bench has no licence: do not share, do not commit). Generated <span id="gen"></span>.</header>
<nav id="tabs"></nav>
<main id="view"></main>
<aside id="detail" hidden></aside>
<script id="data" type="application/json">__DATA__</script>
<script>
"use strict";
const D = JSON.parse(document.getElementById('data').textContent);
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const f1 = (x, d = 1) => x == null ? '-' : Number(x).toFixed(d);
const sg = (x, d = 1) => x == null ? '-' : (x > 0 ? '+' : '') + Number(x).toFixed(d);
const sid = rid => rid.replace(/-(loc|opb)$/, '');
const cls = x => x > 0.0001 ? 'pos' : x < -0.0001 ? 'neg' : 'zero';
const M = D.meta;
document.getElementById('gen').textContent = M.generated;
const QI = {}; D.q.forEach((q, i) => { QI[q.b + '|' + q.k] = i; });
const STAGE = M.stages;
const OUTNAME = {F: 'fixed', B: 'broke', R: 'same-right', W: 'same-wrong', '-': 'not in screen / not run'};

/* ------------------------------------------------------------------ state */
const S = {tab: 'overview', f: {bench: '', cat: '', base: '', stage: '', code: '', gap: '', screen: '', flip: '', errata: false, text: '', gate: null}, shown: 200, list: [], cur: -1, node: null, open: {}, run: '', mem: null, memScroll: null};

function setTab(t) { S.tab = t; render(); window.scrollTo(0, 0); }
function render() {
  document.getElementById('tabs').innerHTML = [['overview', 'Overview'], ['matrix', 'Failure matrix'], ['gates', 'Which gate breaks it'], ['questions', 'Questions'], ['memory', 'Memory added (write side)'], ['gaps', 'Data gaps']]
    .map(([k, l]) => `<button class="${S.tab === k ? 'on' : ''}" onclick="setTab('${k}')">${l}</button>`).join('');
  const v = document.getElementById('view');
  v.innerHTML = ({overview: viewOverview, matrix: viewMatrix, gates: viewGates, questions: viewQuestions, memory: viewMemory, gaps: viewGaps})[S.tab]();
  if (S.tab === 'questions') fillTable();
  if (S.tab === 'memory' && S.memScroll) { const e = document.getElementById(S.memScroll); if (e) e.scrollIntoView({block: 'center'}); S.memScroll = null; }
}

/* ------------------------------------------------------------------ overview */
function statusTxt(r) { return !r ? '-' : r.status === 'complete' ? `done ${r.n}` : `<span class="mut">running ${r.n}/${r.exp}</span>`; }
function viewOverview() {
  const bl = D.base.loc, bo = D.base.opb;
  const runs = D.screens.filter(s => /RUNNING/.test(s.verdict)).length;
  let h = `<div class="cards">
   <div class="card"><b>${f1(100 * bl.correct / bl.n)}%</b>LoCoMo ${M.all_convs ? 'all 10 conversations, cat 1-4' : 'dev cat 1-5'} (${esc(M.loc_ref)})<br><small>${bl.correct}/${bl.n}, ${bl.wrong} wrong</small></div>
   <div class="card"><b>${f1(100 * bl.c14_correct / bl.c14_n)}%</b>LoCoMo cat 1-4<br><small>${bl.c14_correct}/${bl.c14_n} (lever sum ${f1(D.waterfall.target14)})</small></div>
   <div class="card"><b>${f1(bo.subs.official_overall)}</b>OP-Bench overall<br><small>${bo.n} probes, ${bo.low} below 0.5</small></div>
   <div class="card"><b>${f1((bo.base || {}).official_overall)}</b>no-memory BASE<br><small>${bo.base_low} below 0.5</small></div>
   <div class="card"><b>${D.screens.length}</b>screens<br><small>${runs} running / partial</small></div>
   <div class="card"><b>${bl.errata_n}</b>errata-flagged q<br><small>${bl.errata_wrong} of them wrong</small></div></div>`;
  /* baseline scores */
  h += '<h2>Baseline scores</h2><table><tr><th>LoCoMo slice</th><th class="n">n</th><th class="n">correct</th><th class="n">accuracy</th></tr>';
  bl.cats.forEach(c => h += `<tr><td>${c.name} (${c.c})</td><td class="n">${c.n}</td><td class="n">${c.k}</td><td class="n">${f1(100 * c.k / c.n)}</td></tr>`);
  h += `<tr><th>cat 1-4</th><th class="n">${bl.c14_n}</th><th class="n">${bl.c14_correct}</th><th class="n">${f1(100 * bl.c14_correct / bl.c14_n)}</th></tr>`;
  h += `<tr><th>cat 1-5</th><th class="n">${bl.n}</th><th class="n">${bl.correct}</th><th class="n">${f1(100 * bl.correct / bl.n)}</th></tr>`;
  bl.convs.forEach(c => h += `<tr><td>${c.item}</td><td class="n">${c.n}</td><td class="n">${c.k}</td><td class="n">${f1(100 * c.k / c.n)}</td></tr>`);
  h += `</table><span class="mut">Judge conventions would credit ${bl.conv_credit} of the baseline wrong answers (offline recompute, second column only).</span>`;
  const subs = ['irrelevance_fully_irrelevant', 'irrelevance_baiting', 'sycophancy_fact', 'sycophancy_value', 'sycophancy_memory', 'repetition', 'official_overall'];
  const oscr = D.screens.filter(s => s.cmp_opb);
  h += '<h3>OP-Bench subscores (x100): no-memory BASE, baseline, every finished screen</h3><table><tr><th>subscore</th><th class="n">BASE</th><th class="n">baseline</th>' + oscr.map(s => `<th class="n">${esc(s.id)}</th>`).join('') + '</tr>';
  subs.forEach(k => {
    h += `<tr><td>${k}</td><td class="n">${f1((bo.base || {})[k] ?? null)}</td><td class="n">${f1(bo.subs[k])}</td>` + oscr.map(s => {
      const c = k === 'official_overall' ? s.cmp_opb.overall : s.cmp_opb.subs[k];
      return c ? `<td class="n ${cls(c.d)}" title="${f1(c.new)} (${sg(c.d)})">${f1(c.new)} <small>${sg(c.d)}</small></td>` : '<td class="na">-</td>';
    }).join('') + '</tr>';
  });
  h += '</table>';
  /* screens */
  h += `<h2>Screens: paired delta against the baseline</h2><div class="mut">LoCoMo = conv-26/30 slice (304 q, cat 1-5) paired per question; verdict is the rule of eval_screen.py (macro mean of per-benchmark point deltas &gt; +1 with no benchmark below its noise band: LoCoMo net &lt; -0.328 sqrt(n), OP-Bench overall &lt; -3). Cross-check column: the pair counts reproduce eval_screen.py.</div>
  <table><tr><th rowspan="2">screen</th><th rowspan="2">tests</th><th rowspan="2">LoCoMo run</th><th colspan="3">LoCoMo paired</th><th colspan="5">per category: new (delta pts)</th><th rowspan="2">OP run</th><th colspan="7">OP-Bench delta (points)</th><th rowspan="2">verdict</th></tr>
  <tr><th class="n">+</th><th class="n">-</th><th class="n">net (band)</th>${['single-hop', 'multi-hop', 'temporal', 'open-domain', 'adversarial'].map(c => `<th class="n">${c}</th>`).join('')}${['irrel.', 'baiting', 'syc fact', 'syc value', 'syc memory', 'repet.', 'overall'].map(c => `<th class="n">${c}</th>`).join('')}</tr>`;
  D.screens.forEach(s => {
    const c = s.cmp_loc, o = s.cmp_opb;
    h += `<tr><td><b>${esc(s.id)}</b></td><td>${esc(s.desc)}</td><td>${statusTxt(s.loc)}</td>`;
    if (c) {
      h += `<td class="n g">${c.g}</td><td class="n r">${c.l}</td><td class="n ${cls(c.net)}">${sg(c.net, 0)} (&plusmn;${f1(c.band)})${c.net < -c.band ? ' <b class="r">guard</b>' : ''}</td>`;
      ['cat4', 'cat1', 'cat2', 'cat3', 'cat5'].forEach(k => { const x = c.cats.find(z => z.c === k); h += x ? `<td class="n ${cls(x.new - x.ref)}" title="${x.g} fixed, ${x.l} broke of ${x.n}">${f1(x.new)} <small>${sg(x.new - x.ref)}</small></td>` : '<td class="na">-</td>'; });
    } else h += '<td class="na" colspan="8">-</td>';
    h += `<td>${statusTxt(s.opb)}</td>`;
    if (o) {
      ['irrelevance_fully_irrelevant', 'irrelevance_baiting', 'sycophancy_fact', 'sycophancy_value', 'sycophancy_memory', 'repetition'].forEach(k => { const x = o.subs[k]; h += x ? `<td class="n ${cls(x.d)}" title="${f1(x.new)} vs ${f1(x.ref)}">${sg(x.d)}</td>` : '<td class="na">-</td>'; });
      h += `<td class="n ${cls(o.overall.d)}">${sg(o.overall.d)}</td>`;
    } else h += '<td class="na" colspan="7">-</td>';
    const vk = s.verdict.split(' ')[0].replace(/-.*/, '');
    h += `<td class="v-${vk}">${esc(s.verdict)}${s.macro != null ? '<br><small>macro ' + sg(s.macro, 2) + '</small>' : ''}${c && c.cross_check ? '<br><small class="mut">' + c.cross_check + '</small>' : ''}</td></tr>`;
  });
  h += '</table>';
  h += viewWaterfall();
  /* funnel */
  h += '<h2>Where the gold turns are lost, per run (LoCoMo cat 1-4 questions with gold turns)</h2><div class="mut">A question is counted at its worst gold turn: no leg returned it (recall) &gt; in a leg but outside the fused pool (fusion) &gt; in the pool but not in the final list (rerank floor/cut) &gt; in the final list but not in the context (assembly/budget). Cell: questions (accuracy %). Needs the forensics stage log.</div><table><tr><th>run</th><th>all gold in context</th><th>recall</th><th>fusion</th><th>rerank</th><th>gate emptied context</th><th>assembly</th></tr>';
  M.loc_runs.forEach(r => { const f = D.funnel[r]; if (!f) { h += `<tr><td>${esc(r)}</td><td class="nl" colspan="5">no forensics stage log</td></tr>`; return; } h += `<tr><td>${esc(r)}</td>` + ['ok', 'recall', 'fusion', 'rerank', 'gate', 'assembly'].map(k => f[k] ? `<td class="n">${f[k].n} <small>(${f1(f[k].acc)})</small></td>` : '<td class="na">0</td>').join('') + '</tr>'; });
  h += '</table>';
  return h;
}

function viewWaterfall() {
  const W = D.waterfall, L = W.levers, n = L.length + 2;
  const w = 1150, hh = 360, ml = 60, mb = 178, mt = 20, bw = (w - ml - 20) / n;
  const lo = Math.floor(W.base14 - 1), hi = Math.ceil(W.target14 + 0.5);
  const y = v => mt + (hh - mt - mb) * (1 - (v - lo) / (hi - lo));
  let s = `<svg width="${w}" height="${hh}" style="background:#fff;border:1px solid var(--bd)">`;
  for (let t = lo; t <= hi; t += 1) s += `<line x1="${ml}" x2="${w - 10}" y1="${y(t)}" y2="${y(t)}" stroke="#eee"/><text x="${ml - 6}" y="${y(t) + 4}" text-anchor="end">${t}</text>`;
  const bar = (i, a, b, col, label, sub, mark) => {
    const x = ml + i * bw + 6, yy = Math.min(y(a), y(b)), hgt = Math.abs(y(a) - y(b)) || 1;
    s += `<rect x="${x}" y="${yy}" width="${bw - 12}" height="${hgt}" fill="${col}"><title>${esc(label)}\n${esc(sub)}</title></rect>`;
    s += `<text transform="translate(${x + 6},${hh - mb + 10}) rotate(55)">${esc(label.length > 30 ? label.slice(0, 29) + '…' : label)}</text>`;
    if (mark) s += `<text x="${x + (bw - 12) / 2}" y="${yy - 3}" text-anchor="middle" font-weight="700" fill="${mark.c}">${mark.t}</text>`;
  };
  bar(0, lo, W.base14, '#6b7d99', 'baseline cat 1-4', f1(W.base14) + '%', {t: f1(W.base14), c: '#333'});
  L.forEach((lv, i) => {
    const ms = lv.measured || []; const done = ms.filter(m => !m.running);
    const best = done.length ? done.reduce((a, b) => (b.net14 > a.net14 ? b : a)) : null;
    const mark = best ? {t: 'meas ' + sg(best.net14, 0) + 'q', c: best.net14 > 0 ? '#1b8a3a' : best.net14 < 0 ? '#c0392b' : '#888'} : (ms.some(m => m.running) ? {t: 'running', c: '#2b5fb4'} : null);
    bar(i + 1, lv.start, lv.end, best ? (best.net14 > 0 ? '#7fcf92' : best.net14 < 0 ? '#e59a92' : '#bbb') : '#9fbfee', lv.n + '. ' + lv.name, `expected +${lv.gain14} pt (${lv.q14} q)`, mark);
  });
  bar(n - 1, lo, W.target14, '#2b5fb4', 'expected total', f1(W.target14) + '%', {t: f1(W.target14), c: '#222'});
  s += '</svg>';
  let t = `<h2>Path to ${f1(W.target14)}: lever waterfall (cat 1-4)</h2><div class="mut">Source: ${esc(W.doc)}. Blue bars are expected gains; a bar turns green/red/grey where a finished screen of that lever measured a net change (questions, conv-26/30 cat 1-4 slice, not scaled). Cat 1-5 view: ${f1(W.base15)}% to ${f1(W.target15)}%.</div>` + s;
  t += '<table><tr><th>#</th><th>lever</th><th class="n">expected q (cat 1-4 / 1-5)</th><th class="n">expected pts cat 1-4</th><th>screens</th><th>measured on the slice</th></tr>';
  L.forEach(lv => {
    const ms = lv.measured.map(m => m.running ? `${esc(m.screen)}: <span class="mut">running</span>` : `${esc(m.screen)}: <b class="${m.net14 > 0 ? 'g' : m.net14 < 0 ? 'r' : ''}">${sg(m.net14, 0)} q</b> (+${m.g}/-${m.l} of ${m.n14}) ${esc(m.verdict)}`).join('<br>');
    t += `<tr><td>${lv.n}</td><td>${esc(lv.name)}</td><td class="n">${lv.q14} / ${lv.q15}</td><td class="n">+${lv.gain14}</td><td>${lv.screens.map(esc).join(', ') || '<span class="mut">none</span>'}</td><td>${ms || (lv.measured_note ? esc(lv.measured_note) : '<span class="mut">not screened</span>')}</td></tr>`;
  });
  return t + '</table>';
}

/* ------------------------------------------------------------------ matrix */
function goQ(f) { S.node = null; S.f = Object.assign({bench: '', cat: '', base: '', stage: '', code: '', gap: '', screen: '', flip: '', errata: false, text: '', gate: null}, f); S.shown = 200; setTab('questions'); }
function viewMatrix() {
  const X = D.matrix; let h = '<h2>LoCoMo: failure stage x category (baseline, wrong answers)</h2><div class="mut">Click any count to filter the question table. Stage codes: ' + Object.values(STAGE).join('; ') + '. Row labels are the sub-types of the failure catalogue (evals/analysis/catalogue/locomo_dev_failures.jsonl).</div>';
  h += '<table><tr><th>stage</th><th>code</th><th>sub-type</th>' + X.cats.map(c => `<th class="n">${c.name}<br><small>${c.wrong} wrong / ${c.n}</small></th>`).join('') + '<th class="n">total</th></tr>';
  let last = null;
  X.rows.forEach(r => {
    if (last !== null && last !== r.stage) { const tot = X.rows.filter(z => z.stage === last).reduce((a, z) => a + z.total, 0); h += `<tr><th colspan="3">stage ${last} total</th>${X.cats.map(c => `<th class="n">${X.rows.filter(z => z.stage === last).reduce((a, z) => a + (z.cells[c.c] || 0), 0) || ''}</th>`).join('')}<th class="n">${tot}</th></tr>`; }
    last = r.stage;
    h += `<tr><td>${esc(STAGE[r.stage] || r.stage)}</td><td>${esc(r.code)}</td><td>${esc(r.label)}</td>` + X.cats.map(c => { const n = r.cells[c.c]; return n ? `<td class="n click" onclick='goQ({bench:"loc",stage:"${r.stage}",code:"${r.code}",cat:"${c.name}"})'>${n}</td>` : '<td></td>'; }).join('') + `<td class="n click" onclick='goQ({bench:"loc",stage:"${r.stage}",code:"${r.code}"})'><b>${r.total}</b></td></tr>`;
  });
  if (last) { const tot = X.rows.filter(z => z.stage === last).reduce((a, z) => a + z.total, 0); h += `<tr><th colspan="3">stage ${last} total</th>${X.cats.map(c => `<th class="n">${X.rows.filter(z => z.stage === last).reduce((a, z) => a + (z.cells[c.c] || 0), 0) || ''}</th>`).join('')}<th class="n">${tot}</th></tr>`; }
  h += `<tr><th colspan="3">all wrong</th>${X.cats.map(c => `<th class="n click" onclick='goQ({bench:"loc",base:"wrong",cat:"${c.name}"})'>${c.wrong}</th>`).join('')}<th class="n click" onclick='goQ({bench:"loc",base:"wrong"})'>${M.loc_wrong}</th></tr></table>`;
  const O = D.opmatrix;
  h += `<h2>OP-Bench: failure class x probe type (baseline, probes below 0.5)</h2><table><tr><th>class</th>` + O.types.map(t => `<th class="n">${esc(t.t)}<br><small>n=${t.n}</small></th>`).join('') + '<th class="n">total</th></tr>';
  O.rows.forEach(r => { h += `<tr><td>${esc(r.code)}</td>` + O.types.map(t => { const n = r.cells[t.t]; return n ? `<td class="n click" onclick='goQ({bench:"opb",code:${JSON.stringify(r.code)},cat:${JSON.stringify(t.t)}})'>${n}</td>` : '<td></td>'; }).join('') + `<td class="n click" onclick='goQ({bench:"opb",code:${JSON.stringify(r.code)}})'><b>${r.total}</b></td></tr>`; });
  h += `<tr><th>probes</th>` + O.types.map(t => `<th class="n click" onclick='goQ({bench:"opb",cat:${JSON.stringify(t.t)}})'>${t.n}</th>`).join('') + `<th class="n">${M.n_opb}</th></tr></table>`;
  /* gap ids */
  const cnt = {}; D.q.forEach(q => { if (q.st) (q.gaps || []).forEach(g => { cnt[g] = cnt[g] || {loc: 0, opb: 0}; cnt[g][q.b]++; }); });
  h += '<h2>Gap ids behind the baseline failures</h2><table><tr><th>gap</th><th>title (GAP_REGISTER.md)</th><th class="n">LoCoMo</th><th class="n">OP-Bench</th></tr>';
  Object.keys(cnt).sort((a, b) => (cnt[b].loc + cnt[b].opb) - (cnt[a].loc + cnt[a].opb)).forEach(g => { const t = (D.gaps[g] || {}).t || ''; h += `<tr><td><span class="tag gap" onclick='goQ({gap:"${g}"})'>${g}</span></td><td>${esc(t)}</td><td class="n">${cnt[g].loc || ''}</td><td class="n">${cnt[g].opb || ''}</td></tr>`; });
  return h + '</table>';
}

/* ------------------------------------------------------------------ gates */
function gcell(c, run, gate, bench) {
  if (c == null) return '<td class="na">-</td>';
  if (c === 'nl') return '<td class="nl">not logged</td>';
  const clickable = c.t ? `class="click" onclick='goGate(${JSON.stringify(run)},${JSON.stringify(gate)},"${bench}")'` : '';
  return `<td ${clickable}><b>${c.t}</b> <small><b class="g">+${c.h}</b> <b class="r">-${c.b}</b>${c.a != null ? ' acc ' + f1(c.a, 0) : ''}</small></td>`;
}
function goGate(run, gate, bench) { goQ({bench, gate: {run, id: gate}}); }
function viewGates() {
  const G = D.gates;
  let h = `<h2>Which gate breaks it</h2><div class="mut">Per decision point: how many questions it touched in each screen, how many of those the screen fixed (+) or broke (-) relative to the baseline, and the screen's accuracy on the touched questions. Association, not causation: a touched question can flip for another reason. <b>not logged</b> = the run's stage log has no such field (older commits predate the logging fix). Click a cell to list its questions.</div>`;
  h += '<table><tr><th rowspan="2">gate / decision point</th><th rowspan="2">baseline LoCoMo<br>touched (acc)</th><th rowspan="2">baseline OP<br>touched (acc)</th>' + G.loc_runs.map(r => `<th colspan="${G.opb_runs.includes(r.replace(/-loc$/, '-opb')) ? 2 : 1}">${esc(sid(r))}</th>`).join('') + '</tr><tr>' + G.loc_runs.map(r => `<th>LoCoMo</th>${G.opb_runs.includes(r.replace(/-loc$/, '-opb')) ? '<th>OP-Bench</th>' : ''}`).join('') + '</tr>';
  G.rows.forEach(g => {
    const b = g.base;
    h += `<tr><td title="${esc(g.desc)}"><b>${esc(g.label)}</b><br><small class="mut">${esc(g.desc)}</small></td>` + (b === 'nl' ? '<td class="nl">not logged</td>' : `<td class="n">${b.t} <small>(${b.a == null ? '-' : f1(b.a, 0)})</small></td>`);
    h += g.base_opb ? `<td class="n">${g.base_opb.t} <small>(${g.base_opb.a == null ? '-' : f1(g.base_opb.a, 0)})</small></td>` : '<td class="na">-</td>';
    G.loc_runs.forEach(r => {
      const c = (g.cells[r] || {}).loc; h += gcell(c, r, g.id, 'loc');
      const orun = r.replace(/-loc$/, '-opb'); if (G.opb_runs.includes(orun)) h += gcell((g.cells[orun] || {}).opb, orun, g.id, 'opb');
    });
    h += '</tr>';
  });
  h += '</table>';
  h += '<div class="mut">OP-Bench screens that have no LoCoMo twin are not shown in this grid: ' + (G.opb_runs.filter(r => !G.loc_runs.includes(r.replace(/-opb$/, '-loc'))).join(', ') || 'none') + '. Only the relevance gate has an OP-Bench reading (context_tokens == 0 per probe).</div>';
  return h;
}

/* ------------------------------------------------------------------ questions */
function allScreens() { return [...new Set(M.screen_ids_loc.concat(M.screen_ids_opb))]; }
function viewQuestions() {
  const f = S.f, opts = (arr, cur, all) => `<option value="">${all}</option>` + arr.map(x => `<option ${x === cur ? 'selected' : ''}>${esc(x)}</option>`).join('');
  const cats = [...new Set(D.q.map(q => q.cat))].sort();
  const stages = Object.keys(STAGE).concat(['x']);
  const codes = [...new Set(D.q.filter(q => q.code).map(q => q.code))].sort();
  const gaps = [...new Set(D.q.flatMap(q => q.gaps || []))].sort();
  let h = `<div class="qlay"><aside class="side"><b>Sections</b> <small class="mut">n  &#10003;correct  &#10007;wrong (baseline). Click a name to filter, the arrow to expand; expanding a conversation / persona lists its questions.</small>${treeHtml()}</aside><div class="qmain"><h2>Questions</h2><div class="legend mut"><span><i class="oF"></i>fixed</span><span><i class="oB"></i>broke</span><span><i class="oR"></i>same-right</span><span><i class="oW"></i>same-wrong</span><span><i class="o-"></i>not in screen / not run</span>
   <br>strip LoCoMo: ${M.screen_ids_loc.map((s, i) => `${i + 1}=${esc(s)}`).join(' &nbsp;')} <br>strip OP-Bench: ${M.screen_ids_opb.map((s, i) => `${i + 1}=${esc(s)}`).join(' &nbsp;')}</div>`;
  h += `<div class="flt">
   <label>benchmark<select onchange="S.f.bench=this.value;redo()">${opts(['loc', 'opb'], f.bench, 'all')}</select></label>
   <label>category / type<select onchange="S.f.cat=this.value;redo()">${opts(cats, f.cat, 'all')}</select></label>
   <label>baseline<select onchange="S.f.base=this.value;redo()">${opts(['wrong', 'right'], f.base, 'all')}</select></label>
   <label>stage<select onchange="S.f.stage=this.value;redo()">${opts(stages, f.stage, 'all')}</select></label>
   <label>code / class<select onchange="S.f.code=this.value;redo()">${opts(codes, f.code, 'all')}</select></label>
   <label>gap id<select onchange="S.f.gap=this.value;redo()">${opts(gaps, f.gap, 'all')}</select></label>
   <label>screen<select onchange="S.f.screen=this.value;redo()">${opts(allScreens(), f.screen, 'none')}</select></label>
   <label>flip in screen<select onchange="S.f.flip=this.value;redo()"><option value="">any</option>${[['F', 'fixed (gained)'], ['B', 'broke (lost)'], ['FB', 'fixed or broke'], ['R', 'same-right'], ['W', 'same-wrong'], ['-', 'not in screen']].map(([k, l]) => `<option value="${k}" ${f.flip === k ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
   <label>errata<select onchange="S.f.errata=this.value==='1';redo()"><option value="">all</option><option value="1" ${f.errata ? 'selected' : ''}>flagged only</option></select></label>
   <label>search id / question / gold / answer<input type="text" size="26" value="${esc(f.text)}" oninput="S.f.text=this.value;clearTimeout(window._t);window._t=setTimeout(redo,250)"></label>
   ${f.gate ? `<label>gate filter<span class="tag s">${esc(f.gate.id)} in ${esc(f.gate.run)} <a href="#" onclick="S.f.gate=null;redo();return false">x</a></span></label>` : ''}
   <button class="btn" onclick="goQ({})">reset</button> ${S.node ? '<span class="tag s">section: ' + esc(NODES[S.node].label) + '</span>' : ''} <b id="cnt"></b></div>
   <table id="qt"><thead><tr><th>id</th><th>bench</th><th>category / type</th><th>base</th><th>stage / sub-type</th><th>gaps</th><th>proposed generic fix</th><th>screens</th><th>flags</th></tr></thead><tbody></tbody></table><button class="btn" id="more" onclick="S.shown+=300;fillTable()">show more</button></div></div>`;
  return h;
}
function redo() { S.shown = 200; const y = window.scrollY; render(); window.scrollTo(0, y); }
function matchQ(q, f) {
  if (S.node && !NODES[S.node].test(q)) return false;
  if (f.bench && q.b !== f.bench) return false;
  if (f.cat && q.cat !== f.cat) return false;
  if (f.base === 'wrong' && q.ok) return false;
  if (f.base === 'right' && !q.ok) return false;
  if (f.stage && q.st !== f.stage) return false;
  if (f.code && q.code !== f.code) return false;
  if (f.gap && !(q.gaps || []).includes(f.gap)) return false;
  if (f.errata && !(q.errs || q.errc)) return false;
  if (f.screen) {
    const L = q.b === 'loc' ? M.screen_ids_loc : M.screen_ids_opb, i = L.indexOf(f.screen);
    const c = i < 0 ? '-' : q.o[i];
    if (f.flip === 'FB' ? !(c === 'F' || c === 'B') : (f.flip && c !== f.flip)) return false;
    if (!f.flip && c === '-') return false;
  }
  if (f.gate) { const r = q.r[f.gate.run]; if (!r || !(r.fl || []).includes(f.gate.id)) return false; }
  if (f.text) {
    if (!q._h) { const base = q.r[q.b === 'loc' ? M.loc_ref : M.opb_ref] || {}; q._h = (q.id + ' ' + q.q + ' ' + (q.g || '') + ' ' + (base.a || '') + ' ' + (q.sub || '')).toLowerCase(); }
    if (!q._h.includes(f.text.toLowerCase())) return false;
  }
  return true;
}
function strip(q) { return '<span class="strip">' + q.o.split('').map((c, i) => `<i class="o${c}" title="${esc((q.b === 'loc' ? M.screen_ids_loc : M.screen_ids_opb)[i])}: ${OUTNAME[c]}"></i>`).join('') + '</span>'; }
function fillTable() {
  S.list = D.q.map((q, i) => i).filter(i => matchQ(D.q[i], S.f));
  document.getElementById('cnt').textContent = `${S.list.length} of ${D.q.length} questions`;
  const rows = S.list.slice(0, S.shown).map((i, n) => {
    const q = D.q[i];
    const fl = [];
    (q.errs || []).forEach(e => fl.push(`<span class="tag e" title="${esc(e.reason)}">${esc(e.tag)}</span>`));
    if (q.errc) fl.push(`<span class="tag e" title="${esc(q.errc)}">errata candidate</span>`);
    const st = q.st ? `<b>${q.st === 'x' ? '' : esc(q.st) + ' '}</b>${esc(q.sub || '')}` : '';
    return `<tr class="row" onclick="openQ(${n})"><td>${esc(q.b === 'loc' ? q.k : q.id)}</td><td>${q.b}</td><td>${esc(q.cat)}</td><td class="${q.ok ? 'ok' : 'bad'}">${q.ok ? '&#10003;' : '&#10007;'}</td><td>${st}</td><td>${(q.gaps || []).map(g => `<span class="tag gap">${g}</span>`).join('')}</td><td>${esc(q.fix || '')}</td><td>${strip(q)}</td><td>${fl.join('')}</td></tr>`;
  }).join('');
  document.querySelector('#qt tbody').innerHTML = rows;
  document.getElementById('more').style.display = S.list.length > S.shown ? '' : 'none';
}

/* ------------------------------------------------------------------ detail */
function openQ(n) { S.cur = n; const q = D.q[S.list[n]]; const el = document.getElementById('detail'); el.hidden = false; el.innerHTML = detail(q, n); el.scrollTop = 0; }
function rerenderDetail() { const el = document.getElementById('detail'); const st = el.scrollTop; el.innerHTML = detail(D.q[S.list[S.cur]], S.cur); el.scrollTop = st; }
function setRun(v) { S.run = v; rerenderDetail(); }
function openKey(b, k) { const qi = QI[b + '|' + k]; S.list = [qi]; S.cur = 0; openQ(0); }
function closeQ() { document.getElementById('detail').hidden = true; }
function step(d) { const n = S.cur + d; if (n >= 0 && n < S.list.length) { if (n >= S.shown) { S.shown = n + 50; fillTable(); } openQ(n); } }
document.addEventListener('keydown', e => { if (document.getElementById('detail').hidden) return; if (e.key === 'Escape') closeQ(); else if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return; else if (e.key === 'ArrowRight') step(1); else if (e.key === 'ArrowLeft') step(-1); });

function turnInfo(conv, t) { return (D.tt[conv] || {})[t]; }
const _ridCache = {};
function ridMap(c) { if (!_ridCache[c]) { const o = {}; Object.entries((D.mem || {})[c] || {}).forEach(([t, m]) => { if (m.rid) o[m.rid] = t; }); _ridCache[c] = o; } return _ridCache[c]; }
function lineText(conv, t) { const x = turnInfo(conv, t); if (!x) return `[${esc(t)}] (turn not in the dataset)`; return esc(x[2] || (x[0] + ': ' + x[1])); }
function conv(q) { return q.it.split(':')[0]; }
function memRec(c, t) { return (D.mem[c] || {})[t]; }
function memBadge(c, t) {
  const m = memRec(c, t); if (!m) return '<small class="mut">(no write record)</small>';
  const p = [m.w === false ? '<b class="r">NOT WRITTEN</b>' : 'stored', m.q ? '<b class="r">QUARANTINED</b>' : null, m.mt, m.tr != null ? 'trust ' + m.tr : null, m.st, m.stx ? '<b class="o">text altered</b>' : null].filter(Boolean);
  return `<small class="mut">${p.join(' · ')}</small>`;
}
function turnNo(t) { const m = /^D(\d+):(\d+)$/.exec(t); return m ? [+m[1], +m[2]] : null; }
function anchorOf(t, fin) { const a = turnNo(t); if (!a) return null; let best = null, bd = 1e9; fin.forEach(f => { const b = turnNo(f); if (b && b[0] === a[0]) { const d = Math.abs(b[1] - a[1]); if (d < bd) { bd = d; best = f; } } }); return best; }
function goldSet(q) { return new Set(q.ev || []); }
function narrateRun(q, rid) {
  const r = q.r[rid] || {}; const g = r.g || []; const out = [];
  g.forEach(x => {
    const [t, v, lx, xr, f, rr, rs, fin, c, lost] = x;
    out.push(`gold ${t}: ` + ({ok: 'reached the context', rescued: 'lost by the search but rescued into the context by neighbour expansion', recall: 'returned by NO leg (recall): vector rank ' + (v ?? 'none') + ', lexical ' + (lx ?? 'none'), fusion: 'in a leg (vector ' + (v ?? 'none') + ', lexical ' + (lx ?? 'none') + (xr ? ', extra ' + JSON.stringify(xr) : '') + ') but outside the fused pool', rerank: `in the reranker pool (fused ${f}) but demoted out of the final list (rerank rank ${rr ?? '-'}, score ${rs == null ? '-' : f1(rs, 3)})`, gate: `in the final list (rank ${fin}) but a gate returned an empty context`, assembly: `in the final list (rank ${fin}) but cut before the context (budget/floor)`}[lost] || lost));
  });
  return out.join('; ');
}
function narrate(q) {
  if (q.b !== 'loc') return '';
  const base = narrateRun(q, M.loc_ref); const out = [base];
  if (!q.ok) { if (q.st === 'd') out.push('all answer-bearing turns reached the context, so the break is in the reader: ' + (q.sub || '')); if (q.st === 'e') out.push('right under the judge conventions: ' + (q.sub || '')); if (q.st === 'f') out.push('dataset/gold error: ' + (q.sub || '')); }
  return out.filter(Boolean).join('; ') || (q.ok ? 'answered correctly' : '');
}

function answersTable(q, runs, sel) {
  const base = q.r[runs[0]] || {};
  let h = '<table><tr><th>run</th><th>result</th><th>answer</th><th>judge</th><th>date-check</th><th>conventions</th><th>retry</th></tr>';
  runs.forEach((rid, i) => {
    const r = q.r[rid]; if (!r) return;
    const isb = i === 0; const o = isb ? '' : (r.ok && !base.ok ? 'fixed' : base.ok && !r.ok ? 'broke' : r.ok ? 'same-right' : 'same-wrong');
    h += `<tr class="${rid === sel ? 'sel' : ''}"><td>${esc(rid)}</td><td class="${r.ok ? 'ok' : 'bad'}">${r.ok ? '&#10003;' : '&#10007;'} <small>${o}</small></td><td>${esc(r.a)}</td><td><small>${esc(typeof r.jr === 'string' ? r.jr : '')} score=${r.sc ?? ''}</small></td>`;
    h += `<td><small>${r.sd != null ? 'recorded ' + r.sd : (r.od ? 'offline: same day, would flip to correct' : 'offline: no flip')}</small></td>`;
    h += `<td><small>${r.sv != null ? 'recorded ' + r.sv : (r.ov ? 'offline: credited, ' + esc(r.cv || '') : 'offline: no credit')}</small></td>`;
    h += `<td><small>${r.rt === true || r.fa != null ? 'retry fired' + (r.ra != null ? ', accepted=' + r.ra : '') + (r.fa ? '; first answer: ' + esc(r.fa) : '') : ''}</small></td></tr>`;
  });
  return h + '</table>';
}
function ranksTable(q, runs) {
  let rows = '';
  runs.forEach(rid => { const r = q.r[rid]; if (!r || !r.g) return; r.g.forEach(g => { rows += `<tr><td>${esc(rid)}</td><td><b class="g">GOLD</b> ${esc(g[0])}</td><td class="n">${g[1] ?? '-'}</td><td class="n">${g[2] ?? '-'}</td><td>${g[3] ? esc(Object.entries(g[3]).map(([k, v]) => k + ' ' + v).join(', ')) : '-'}</td><td class="n">${g[4] ?? '-'}</td><td class="n">${g[5] ?? '-'}${g[6] != null ? ' <small>(' + f1(g[6], 3) + ')</small>' : ''}</td><td class="n">${g[7] ?? '-'}</td><td>${g[8] ? 'yes' : '<b class="r">no</b>'}</td><td>${esc(g[9])}</td></tr>`; }); });
  if (!rows) return '<div class="nl" style="padding:4px">no gold turns (cat 5) or no forensics stage log for these runs</div>';
  return '<table><tr><th>run</th><th>gold turn</th><th>vector rank</th><th>lexical rank</th><th>extra legs</th><th>fused rank</th><th>rerank rank (score)</th><th>final rank</th><th>in context</th><th>lost at</th></tr>' + rows + '</table>';
}

function snippet(c, t) { const x = turnInfo(c, t); if (!x) return ''; const s = x[0] + ': ' + x[1]; return esc(s.length > 70 ? s.slice(0, 69) + '…' : s); }

/* final context: full text of every line, hit vs replay-window neighbour, date, speaker, write record */
function ctxBlock(q, rid) {
  const r = q.r[rid]; if (!r || !r.ctx || !r.ctx.length) return `<div class="nl" style="padding:4px">${r && r.ce ? '<b class="r">empty context</b> (the reader got nothing)' : 'no context recorded for this run'}</div>`;
  const c = conv(q), gold = goldSet(q), dist = new Set(q.dv || []), fin = r.tr ? r.tr.fin.map(x => x[0]) : (r.fin || []);
  const persona = q.persona; let np = 0, ng = 0;
  const finRank = {}; fin.forEach((t, i) => { finRank[t] = i + 1; });
  let h = '<div class="ctx">';
  r.ctx.forEach(t => {
    const x = turnInfo(c, t); const isg = gold.has(t), isd = dist.has(t), isp = persona && x && x[0] === persona; if (isp) np++; if (isg) ng++;
    const role = finRank[t] ? `<b>HIT #${finRank[t]}</b>` : (fin.length ? `window of ${esc(anchorOf(t, fin) || '?')}` : '');
    h += `<div class="${isg ? 'gold' : isd ? 'dist' : isp ? 'pers' : ''}"><b class="id">${esc(t)}</b> ${isg ? '<b class="g">[GOLD]</b> ' : ''}${isd ? '<b class="o">[distractor source]</b> ' : ''}${isp ? '<b>[persona]</b> ' : ''}<small>${role}${x ? ' · ' + esc(x[3]) + ' · ' + esc(x[4] || '') : ''}</small> ${memBadge(c, t)}<br>${lineText(c, t)}</div>`;
  });
  h += '</div>';
  const tok = r.tok ?? r.ct, bud = (M.budgets || {})[rid];
  const goldTxt = q.ev && q.ev.length ? `; ${ng} of ${new Set(q.ev).size} gold turns present` : '';
  return `<div class="mut">${r.ctx.length} lines, ${tok ?? '?'} tokens${bud ? ' / budget ' + bud : ''}${goldTxt}${persona ? `; ${np} spoken by the persona (${esc(persona)}): ${f1(100 * np / Math.max(1, r.ctx.length), 0)}%` : ''}. Lines are shown as the reader saw them (relative dates resolved where the stage log has the annotated text). HIT = in the final top-k; window = neighbour added by replay expansion${fin.length ? '' : ' (no stage log: hits unknown)'}.</div>` + h;
}
function gapBlock(q) {
  if (!q.gaps) return '';
  return q.gaps.map(g => { const x = D.gaps[g] || {}; return `<details><summary><span class="tag gap">${g}</span> ${esc(x.t || '')}</summary><div class="box"><b>Evidence.</b> ${esc(x.e || '')}\n<b>Solution options.</b> ${esc(x.s || '')}\n<b>Status.</b> ${esc(x.st || '')}</div></details>`; }).join('');
}

function detail(q, n) {
  const runsAll = q.b === 'loc' ? M.loc_runs : M.opb_runs, runs = runsAll.filter(r => q.r[r]);
  const baseRun = q.b === 'loc' ? M.loc_ref : M.opb_ref;
  const rid = q.r[S.run] ? S.run : baseRun;
  const nav = `<div style="float:right"><button class="btn" onclick="step(-1)">&larr; prev</button> <button class="btn" onclick="step(1)">next &rarr;</button> <button class="btn" onclick="closeQ()">close (Esc)</button> <small class="mut">${n + 1} / ${S.list.length}</small></div>`;
  const sel = `<label class="runsel">run: <select onchange="setRun(this.value)">${runs.map(r => `<option ${r === rid ? 'selected' : ''}>${esc(r)}</option>`).join('')}</select></label> <button class="btn" onclick="openMem(${JSON.stringify(q.b)},${JSON.stringify(q.k)},${JSON.stringify(rid)})">open in memory view</button>`;
  return q.b === 'loc' ? detailLoc(q, nav, sel, runs, rid) : detailOpb(q, nav, sel, runs, rid);
}
/* ------------------------------------------------------------------ pipeline timeline (read side) */
const STEPTXT = {L: 'logged', R: 'reconstructed', M: 'not logged in this run'};
function stepBox(title, st, note, body, open) {
  return `<details class="step" ${open ? 'open' : ''}><summary><span class="dot d${st}"></span> <b>${esc(title)}</b> <span class="bd b${st}">${STEPTXT[st]}</span></summary><div class="sb">${note ? `<div class="mut" style="margin-bottom:4px">${esc(note)}</div>` : ''}${body || ''}</div></details>`;
}
function covOf(rid, key) { const c = (D.coverage[rid] || {}); const k = Object.keys(c).find(x => x.startsWith(key + ' ')); return k ? c[k] : ['M', '']; }
function fmt(t, m) { return t.replace(/\{\{|\}\}|\{(\w+)\}/g, (x, k) => x === '{{' ? '{' : x === '}}' ? '}' : (k in m ? m[k] : x)); }
function ctxLines(q, rid) {
  const r = q.r[rid], info = D.runinfo[rid] || {}, c = conv(q);
  return (r.ctx || []).map(t => { const x = turnInfo(c, t), m = memRec(c, t) || {}; const txt = x ? (x[2] || (x[0] + ': ' + x[1])) : t; const pre = info.dated && m.vf ? `[${m.vf.slice(0, 10)}] ` : ''; return pre + txt; });
}
function readerPrompt(q, rid, question) {
  const info = D.runinfo[rid] || {}, r = q.r[rid]; const tpl = D.tpl.reader[info.reader_tpl]; if (!tpl) return null;
  const ctx = (r.ctx && r.ctx.length) ? ctxLines(q, rid).join('\n') : ''; const m = {context: ctx, question, question_date: 'unknown', memory: ctx};
  if (!ctx && info.no_memory_prompt) return {user: fmt(D.tpl.no_memory, m), note: 'no-memory prompt (empty context)'};
  if (tpl.kind === 'str') return {user: fmt(tpl.text, m)};
  if (tpl.kind === 'routed') { const sh = tpl.shape === 'generic' ? (q.qa || {}).generic_qa_shape : (q.qa || {}).qa_shape; return {user: fmt(tpl.variants[sh] || tpl.variants.plain || Object.values(tpl.variants)[0], m), note: 'routed variant: ' + sh}; }
  return {system: tpl.system, user: ctx ? fmt(tpl.with_context, m) : fmt(tpl.without_context, m)};
}
function judgePrompt(q, rid, answer) {
  const info = D.runinfo[rid] || {}, suite = info.suite; if (!suite) return null;
  const route = (q.jrt || {})[suite]; let pid, p;
  if (suite === 'opbench') { pid = 'opbench/' + route; p = D.tpl.prompts[pid]; if (!p) return {note: route === 'diversity' ? 'repetition: no judge call (embedding cosine over the group)' : 'prompt template not available'}; return {pid, user: p.text.replace('{question}', () => q.q).replace('{response}', () => answer), source: p.source}; }
  const st = D.tpl.judge[suite]; if (!st) return null; pid = st.routes[route] || st.routes.default; p = D.tpl.prompts[pid]; if (!p) return null;
  const m = {question: q.q, gold: q.g, answer, pred: answer, evidence: '', golden_answer: q.g, response: answer, expected_answer: q.g, ai_response: answer, generated_answer: answer};
  return {pid, route, system: p.system, user: fmt(p.text, m), source: p.source};
}
function pre(s) { return `<div class="box">${esc(s)}</div>`; }
function legTable(cols, q, r, cap) {
  const c = conv(q), gold = goldSet(q), dist = new Set(q.dv || []); const rows = Math.max(0, ...cols.map(x => x[1].length));
  let h = '<div style="overflow:auto"><table class="trace"><tr>' + cols.map(x => `<th>${esc(x[0])}<br><small class="mut">${x[1].length} shown</small></th>`).join('') + '</tr>';
  if (r.g && r.g.length) h += '<tr>' + cols.map(x => { const k = x[2]; const gi = {v: 1, l: 2, f: 4, rr: 5, fin: 7}; const p = r.g.map(g => { const v = k.startsWith('x:') ? (g[3] || {})[k.slice(2)] : g[gi[k]]; return `${g[0]} ${v == null ? '<span class="r">-</span>' : '#' + v}`; }).join('<br>'); return `<td class="gp"><b class="g">GOLD</b><br>${p}</td>`; }).join('') + '</tr>';
  for (let i = 0; i < rows; i++) h += '<tr>' + cols.map(x => { const e = x[1][i]; if (!e) return '<td></td>'; const isg = gold.has(e[0]), isd = dist.has(e[0]); return `<td class="${isg ? 'gold' : isd ? 'dist' : ''}"><small class="mut">#${i + 1}</small> <b>${esc(e[0])}</b> ${isg ? '<b class="g">GOLD</b> ' : ''}${isd ? '<b class="o">DIST</b> ' : ''}<small>${f1(e[1], 3)}</small><br><small class="mut">${snippet(c, e[0])}</small></td>`; }).join('') + '</tr>';
  return h + '</table></div>';
}
function readTimeline(q, rid) {
  const r = q.r[rid], info = D.runinfo[rid] || {}, c = conv(q), isLoc = q.b === 'loc';
  const T = r.tr, cap = M.leg_cap, qa = q.qa || {};
  const cv = k => covOf(rid, k); let h = '';
  // 1 query analysis
  { const [st, note] = cv('R1'); const xq = r.xf && r.xf.query_analysis;
    const flags = (qa.flags || []).map(f => `<span class="tag s">${esc(f)}</span>`).join('') || '<span class="mut">no shape flag set</span>';
    const xflags = xq ? Object.entries(xq.flags || {}).filter(([n, v]) => v === true).map(([n]) => `<span class="tag s">${esc(n)}</span>`).join('') || '<span class="mut">no shape flag set</span>' : flags;
    const pp = r.xf && r.xf.perspective;
    let ptxt = '<span class="mut">not logged in this run; fix: --trace-full (forensics trace_full.perspective)</span>';
    if (pp) {
      const rid2turn = ridMap(c);
      const fac = Object.entries(pp.factors || {}).map(([id, v]) => `${esc(rid2turn[id] || id.slice(0, 8))}: ${esc(Object.entries(v).map(([a, b]) => a + ' x' + b).join(', '))}`);
      const facHtml = fac.length ? fac.map(x => `<span class="tag e">${x}</span>`).join('') : '<span class="mut">no candidate down-weighted</span>';
      ptxt = `asker <b>${esc(pp.asker)}</b> · about <b>${esc(pp.about)}</b> · targets <b>${esc(JSON.stringify(pp.targets))}</b> · scope ${esc(pp.scope)} · negated ${esc(pp.negated)} · mode <b>${esc(pp.mode)}</b> · dropped ${(pp.dropped || []).length}<div><b>Candidates down-weighted by the subject factor</b> (turn: factor; 0.84 = the target speaks about someone else, 0.8 = unresolved third party, 0.6 = the turn is about another person): ${facHtml}</div>`;
    }
    const xsplit = xq && xq.split_intents && xq.split_intents.length ? `<div><b>Split intents (logged):</b> ${xq.split_intents.map(z => esc(z)).join(' | ')}</div>` : '';
    h += stepBox('1. Query analysis', st, note, `<div><b>Question:</b> ${esc(q.q)}</div><div><b>Question date:</b> ${isLoc || q.b === 'opb' ? 'none (the dataset gives none; the reader prompt gets "unknown")' : ''}</div><div><b>Shape flags${xq ? ' (logged by the engine)' : ' (rule code run offline)'}:</b> ${xflags}</div><div><b>rule_read_mode:</b> ${esc((xq || qa).rule_read_mode)} · <b>question_shape:</b> ${esc((xq || qa).question_shape)} · <b>statement_form:</b> ${esc((xq || qa).statement_form)} · <b>reader prompt route:</b> qa_shape=${esc((xq || qa).qa_shape)}, generic_qa_shape=${esc((xq || qa).generic_qa_shape)}</div>${xsplit}<div><b>Planner / read mode (run config):</b> ${esc(info.read_mode)}${info.read_cfg && info.read_cfg.list_mode ? ' · list_mode=' + esc(JSON.stringify(info.read_cfg.list_mode)) : ''}</div><div><b>Resolved perspective (asker / about / targets):</b> ${ptxt}</div><div><b>Entity check (I60):</b> ${r.xf ? '<span class="mut">no entity note in the trace (read.entity_check is off in this config)</span>' : '<span class="mut">not logged in this run; fix: --trace-full</span>'}</div>`, true); }
  // 2 gates
  { const [st, note] = cv('R2'); let b = '';
    if (!r.dec && r.xf) b += '<div class="mut">no decider task is configured in this run, so no decision exists to log</div>'; else if (r.dec) b += '<div><b>Decider decisions:</b> ' + r.dec.map(d => `${esc(d.task)} = <b>${esc(d.final || d.label)}</b> (${esc(d.adapter || '')}${d.confidence != null ? ', confidence ' + f1(d.confidence, 2) : ''}${d.used === false ? ', not used' : ''}, heuristic ${esc(d.heuristic)})`).join('; ') + '</div>'; else b += '<div class="mut">decider decisions: not logged in this run</div>';
    b += '<div><b>I74 named-entity bypass:</b> ' + (r.rb ? (r.rb.fired ? '<b>FIRED</b>' : 'not fired') + ' (' + esc(r.rb.kind || r.rb.mode || '') + (r.rb.names ? ': ' + esc(r.rb.names.join(', ')) : '') + ')' : '<span class="mut">' + (r.xf ? 'not applicable: the relevance gate is off in this config' : 'not logged in this run') + '</span>') + '</div>';
    b += `<div><b>Relevance gate outcome:</b> ${r.ce || (isLoc ? false : (r.ct === 0)) ? '<b class="r">context EMPTY: the gate closed the read</b>' : 'context returned (' + (r.ctx ? r.ctx.length : '?') + ' lines)'}</div>`;
    if (r.fl && r.fl.length) b += '<div><b>Gates that touched this question:</b> ' + r.fl.map(x => `<span class="tag s">${esc(x)}</span>`).join('') + '</div>';
    h += stepBox('2. Gates and deciders', st, note, b); }
  // 3-5 legs
  const nolog = '<div class="nl" style="padding:6px">No stage log for this run.</div>';
  { const [st, note] = cv('R3'); let b = nolog;
    if (T) { const cols = [['Vector leg (cosine)', T.v, 'v'], ['Lexical leg (BM25)', T.l, 'l']]; Object.entries(T.x || {}).forEach(([n, l]) => cols.push(['Extra leg: ' + n, l, 'x:' + n])); b = legTable(cols, q, r, cap) + `<div class="mut">Top ${cap} per leg shown; exact gold ranks come from the full 60-deep lists. ${(r.legs || []).length ? 'Extra legs fired: ' + esc(r.legs.join(', ')) : 'No extra leg fired.'}</div>`; }
    h += stepBox('3. Retrieval legs (hits with scores)', st, note, b, true); }
  { const [st, note] = cv('R4'); h += stepBox('4. Fusion (RRF ranks)', st, note, T ? legTable([['Fused (RRF), the rerank pool', T.f, 'f']], q, r, cap) : nolog); }
  { const [st, note] = cv('R5'); const pool = T ? T.p.slice().sort((a, b) => b[1] - a[1]).slice(0, cap) : [];
    h += stepBox('5. Rerank pool and scores', st, note, T ? `<div class="mut">Reranker: ${esc(T.pr || 'none')}. Scores are the engine's logged scale (one kind). Floor / cut config: ${esc(JSON.stringify(info.read_cfg || {}))}</div>` + legTable([['Reranker order (score)', pool, 'rr'], ['Final hits (top-k)', T.fin, 'fin']], q, r, cap) : nolog); }
  // 6 assembly
  { const [st, note] = cv('R6'); const fin = T ? T.fin.map(x => x[0]) : (r.fin || []); const ctxS = new Set(r.ctx || []); const cut = fin.filter(t => !ctxS.has(t));
    const tok = r.tok ?? r.ct; let b = `<div>final hits: <b>${fin.length || '?'}</b> · context lines: <b>${(r.ctx || []).length}</b> · context tokens: <b>${tok ?? '?'}</b> / budget <b>${info.budget ?? M.budgets[rid] ?? '?'}</b>${r.trunc ? ' · <b class="r">context truncated by the budget</b>' : ''}</div>`;
    b += `<div><b>Final hits cut before the reader:</b> ${cut.length ? cut.map(t => `<b>${esc(t)}</b>${goldSet(q).has(t) ? ' <b class="g">GOLD</b>' : ''}`).join(', ') + ' (reason not logged; floors in config: ' + esc(JSON.stringify((info.read_cfg || {}).assembly || {})) + ', rerank_floor=' + esc((info.read_cfg || {}).rerank_floor) + ')' : 'none (every final hit reached the context)'}</div>`;
    const offtxt = r.xf ? '<span class="mut">none in the trace (feature off in this config)</span>' : '<span class="mut">not logged in this run; fix: --trace-full</span>';
    b += `<div><b>Dedupe drops:</b> ${r.xf && r.xf.dedupe_dropped ? esc(JSON.stringify(r.xf.dedupe_dropped)) : offtxt} · <b>abstain_on_raw:</b> ${r.xf && r.xf.abstain_on_raw ? esc(JSON.stringify(r.xf.abstain_on_raw)) : offtxt} · <b>owner / entity notes:</b> ${offtxt}</div>`;
    if (r.xf && r.xf.cuts) b += `<div class="mut">The engine logs only the cuts of the rerank step below. A candidate that was in a leg but ranked outside the fused pool (a fusion cut, the largest retrieval loss class) has no cut entry: read it from the gold rank table in steps 3-4.</div>`;
    if (r.xf && r.xf.assembled) b += `<div><b>Assembled (engine):</b> ${esc(JSON.stringify(r.xf.assembled))}</div>`;
    if (r.xf && r.xf.cuts) b += `<div><b>Every candidate the read dropped, with the reason (engine sink):</b></div>` + (r.xf.cuts.length ? '<table><tr><th>turn</th><th>reason</th><th>detail</th></tr>' + r.xf.cuts.map(z => { const { id, turn, reason, ...rest } = z; return `<tr class="${goldSet(q).has(turn) ? 'sel' : ''}"><td><b>${esc(turn)}</b> ${goldSet(q).has(turn) ? '<b class="g">GOLD</b>' : ''}</td><td>${esc(reason)}</td><td><small>${esc(JSON.stringify(rest))}</small></td></tr>`; }).join('') + '</table>' : '<div class="mut">nothing was dropped</div>');
    h += stepBox('6. Assembly: floors, budget cuts, dedupe, owner notes', st, note, b); }
  // 7 window
  { const [st, note] = cv('R7'); const fin = T ? T.fin.map(x => x[0]) : (r.fin || []); const win = (r.ctx || []).filter(t => !fin.includes(t));
    const wx = r.xf && r.xf.window ? '<div><b>Window expansion (engine sink, logged):</b></div>' + (r.xf.window.length ? r.xf.window.map(w => `<div class="box">anchor <b>${esc(w.anchor)}</b> ${goldSet(q).has(w.anchor) ? '<b class="g">GOLD</b>' : ''} → added ${w.neighbours.map(n => `<b>${esc(n)}</b>${goldSet(q).has(n) ? ' <b class="g">GOLD</b>' : ''}`).join(', ') || 'none'}${Object.keys(w.skipped || {}).length ? '<br><small>skipped: ' + esc(JSON.stringify(w.skipped)) + '</small>' : ''}</div>`).join('') : '<div class="mut">no window turns were added</div>') : '';
    h += stepBox('7. Replay window expansion', st, note, wx + `<div class="mut">window before/after (config): ${esc((info.read_cfg || {}).replay_window_before)} / ${esc((info.read_cfg || {}).replay_window_after)}</div>` + (fin.length ? `<div class="ctx">${win.map(t => { const x = turnInfo(c, t); return `<div class="${goldSet(q).has(t) ? 'gold' : ''}"><b class="id">${esc(t)}</b> ${goldSet(q).has(t) ? '<b class="g">[GOLD]</b> ' : ''}<small>added around ${esc(anchorOf(t, fin) || '?')}</small> ${x ? esc(x[0]) : ''}</div>`; }).join('') || '<div class="mut">no neighbour lines were added</div>'}</div>` : '<div class="nl" style="padding:6px">hits unknown without a stage log</div>')); }
  // 8 rendered context
  { const [st, note] = cv('R8'); let extra = '';
    if (!isLoc && q.cc === 'diversity') { const grp = D.q.filter(z => z.b === 'opb' && z.it === q.it && z.cc === 'diversity' && z.k !== q.k); const mine = new Set(r.ctx || []); const js = grp.map(z => { const o = new Set((z.r[rid] || {}).ctx || []); let i = 0; mine.forEach(t => { if (o.has(t)) i++; }); const u = mine.size + o.size - i; return u ? i / u : 0; }); extra = `<div><b>Repetition group:</b> ${grp.length + 1} probes of ${esc(q.it)}; this probe's context overlaps the others by Jaccard mean <b>${js.length ? f1(js.reduce((a, b) => a + b, 0) / js.length, 2) : '-'}</b> (max ${js.length ? f1(Math.max(...js), 2) : '-'}); the official repetition score has no judge call.</div>`; }
    h += stepBox('8. Rendered context (memory lines the reader gets)', st, note, extra + ctxBlock(q, rid)); }
  // 9 prompt
  { const logged = r.tfl && r.tfl.reader && r.tfl.reader[0]; const rp = readerPrompt(q, rid, q.q); const st = logged ? 'L' : (rp ? 'R' : 'M'); let b = '';
    const p = logged ? {system: logged.system, user: logged.prompt} : rp;
    if (p) { b += (p.system ? '<b>System message</b>' + pre(p.system) : '') + '<b>User message</b>' + pre(p.user) + `<div class="mut">${p.user.length} chars ≈ ${Math.round(p.user.length / 4)} tokens by chars/4; the server counted <b>${r.fpt ?? r.pt ?? '?'}</b> prompt tokens${p.note ? ' · ' + esc(p.note) : ''}. Reader ${esc(info.reader_model)}, temperature ${esc(info.temperature)}, max_tokens ${esc(info.max_tokens)}.</div>`; } else b = '<div class="nl" style="padding:6px">The prompt template of this run is not known to the generator.</div>';
    h += stepBox('9. EXACT reader prompt', st, logged ? 'meta.trace_full (exact)' : 'template ' + (info.qa_prompt || '?') + ' + the logged records + question; exact copy needs --trace-full', b); }
  // 10 reader output + retry + post
  { const tf = r.tfl && r.tfl.reader; let b = '';
    if (tf && tf.length) b += tf.map((x, i) => `<b>Reader call ${i + 1} (raw reply)</b>${pre(x.reply)}`).join(''); else b += `<b>Reader reply (the stored answer; for non-extracting prompts it is the raw reply)</b>${pre(r.rraw || (r.fa != null ? r.fa : r.a))}`;
    if (r.rt || r.fa != null) { const instr = (info.retry || {}).instruction || ''; const rp = readerPrompt(q, rid, q.q + instr); b += `<div><b>Refusal retry fired</b>: first answer: ${esc(r.fa)}</div><b>Retry prompt (question + ${esc((info.retry || {}).mode || '')} instruction)${tf && tf[1] ? ' [logged]' : ' [reconstructed]'}</b>${rp ? pre(tf && tf[1] ? tf[1].prompt : rp.user) : ''}<div>Retry answer: ${esc(r.rta)} · accepted: <b>${r.ra}</b> · tokens first/retry: ${r.fpt ?? '?'} / ${r.rpt ?? '?'}</div>`; } else b += `<div class="mut">refusal retry: ${(info.retry || {}).on ? 'enabled in this run, did not fire for this question' : 'not enabled in this run'}</div>`;
    b += `<div><b>Post-steps (count-verify, date-repair, verify):</b> ${(info.post || []).length ? esc(info.post.join(', ')) + ' enabled; their model calls ' + (tf ? 'appear above' : 'are not separately logged') : 'not enabled in this run'}</div><div><b>Final answer:</b> ${esc(r.a)}</div>`;
    h += stepBox('10. Reader raw output, retry, post-steps', 'L', tf ? 'meta.trace_full' : 'answer and retry answers logged in the row; retry prompt reconstructed', b); }
  // 11 judge
  { const jp = judgePrompt(q, rid, r.fa != null && r.ra === false ? r.fa : r.a); const tfj = r.tfl && r.tfl.judge; const guarded = r.tfl && r.tfl.vmeta && r.tfl.vmeta.guard && !(tfj && tfj.length); const nojudge = !isLoc && q.cc === 'diversity'; const st = tfj && tfj.length ? 'L' : (guarded || nojudge ? 'L' : (jp && jp.user ? 'R' : 'M')); let b = '';
    if (tfj && tfj.length) b += tfj.map((x, i) => `<b>Judge call ${i + 1}: prompt</b>${x.system ? pre('[system] ' + x.system) : ''}${pre(x.prompt)}<b>raw reply</b>${pre(x.reply)}`).join('');
    else if (guarded) b += `<div><b>No judge call was made for this row.</b> The verdict came from a deterministic guard before the judge (verdict meta): <b>${esc(JSON.stringify(r.tfl.vmeta))}</b>; the judge prompt below is not applicable.</div>`;
    else { if (jp && jp.user) b += `<div><b>Judge prompt</b> (${esc(jp.pid)}${jp.route ? ', route ' + esc(jp.route) : ''}; source ${esc(jp.source)})</div>${jp.system ? pre('[system] ' + jp.system) : ''}${pre(jp.user)}`; else b += `<div class="mut">${jp && jp.note ? esc(jp.note) : 'no judge prompt available'}</div>`; b += `<div><b>Raw judge reply (logged${typeof r.jr === 'string' && r.jr.length >= 200 ? ', cut at 200 chars' : ''}):</b> ${typeof r.jr === 'string' ? pre(r.jr) : '<span class="mut">not logged for this row</span>'}</div>`; }
    b += `<div><b>Verdict:</b> score ${r.sc ?? r.s ?? ''} → ${r.ok ? '<b class="ok">correct / pass</b>' : '<b class="bad">wrong / fail</b>'} · judge: ${esc(info.judge_id)}${info.judge_guards ? ' (guards on)' : ''}</div>`;
    if (isLoc) b += `<div><b>Date check:</b> ${r.sd != null ? 'recorded ' + r.sd : (r.od ? 'offline recompute: same day, would flip to correct' : 'offline recompute: no flip')} · <b>Judge conventions:</b> ${r.sv != null ? 'recorded ' + r.sv : (r.ov ? 'offline recompute: credited (' + esc(r.cv || '') + ')' : 'offline recompute: no credit')} · <b>Errata flags:</b> ${(q.errs || []).map(e => esc(e.tag)).join(', ') || (q.errc ? 'candidate: ' + esc(q.errc) : 'none')}</div>`;
    h += stepBox('11. Judge: prompt, raw reply, verdict, date check, conventions, errata', st, tfj && tfj.length ? 'meta.trace_full' : guarded ? 'reads.jsonl verdict meta: deterministic guard, no judge call' : nojudge ? 'repetition probe: the official score is an embedding cosine over the probe group, there is no judge call' : 'prompt reconstructed from the suite template; reply as logged (cut at 200 chars); exact copy needs --trace-full', b); }
  // 12 final
  { h += stepBox('12. Final score', 'L', 'results.jsonl', `<div>${r.ok ? '<b class="ok">CORRECT</b>' : '<b class="bad">WRONG</b>'} · score ${r.sc ?? r.s ?? ''}${r.sd != null ? ' · date-checked ' + r.sd : ''} · answer ${esc(r.a)}</div><div class="mut">tokens: prompt ${r.pt ?? '?'}, completion ${r.cpt ?? '?'}, context ${r.tok ?? r.ct ?? '?'} · model calls ${r.mc ?? '?'} · latency ms retrieve/answer/judge ${r.lat ? r.lat.join(' / ') : '?'}</div>`); }
  return `<div class="timeline">${h}</div>`;
}

function detailLoc(q, nav, sel, runs, rid) {
  const c = conv(q);
  let h = nav + `<h2>${esc(q.k)} <small class="mut">LoCoMo ${esc(q.cat)}</small> <span class="${q.ok ? 'ok' : 'bad'}">${q.ok ? 'baseline correct' : 'baseline WRONG'}</span></h2><div>${sel}</div>`;
  h += (q.errs || []).map(e => `<div class="warn"><b>Errata: ${esc(e.tag)}</b>${e.borderline ? ' (borderline)' : ''}. ${esc(e.reason)} <small class="mut">${esc(e.source || '')}</small></div>`).join('') + (q.errc ? `<div class="warn"><b>Errata candidate (not yet in locomo_errata.json):</b> ${esc(q.errc)}</div>` : '');
  h += `<div class="sec"><h3>Question</h3><div class="box">${esc(q.q)}</div><h3>Gold answer</h3><div class="box">${esc(q.g)}</div>`;
  h += `<h3>${q.cc === 'cat5' ? 'Evidence of the distractor (the turn the wrong answer is built from; not gold)' : 'Evidence (gold) turns, as stored'}</h3><div class="ctx">` + ((q.cc === 'cat5' ? q.dv : q.ev) || []).map(t => { const x = turnInfo(c, t); return `<div class="${q.cc === 'cat5' ? 'dist' : 'gold'}"><b class="id">${esc(t)}</b> ${q.cc === 'cat5' ? '' : '<b class="g">[GOLD]</b> '}<small>${x ? esc(x[3]) + ', ' + esc(x[4] || '') : ''}</small> ${memBadge(c, t)}<br>${lineText(c, t)}</div>`; }).join('') + '</div></div>';
  h += `<div class="sec"><h3>Our answer, in the baseline and in each screen</h3>${answersTable(q, runs, rid)}</div>`;
  h += `<div class="sec"><h3>Forensics: where and why it broke</h3>`;
  if (q.st) h += `<div><b>Stage ${esc(q.st)}</b> (${esc(STAGE[q.st] || '')}) &mdash; ${esc(q.sub || '')} <span class="mut">[code ${esc(q.code)}]</span></div>`; else h += '<div class="g">Answered correctly in the baseline; no failure class.</div>';
  h += `<div style="margin:4px 0">${esc(narrate(q))}</div>`;
  if (rid !== M.loc_ref) h += `<div style="margin:4px 0"><b>In ${esc(rid)}:</b> ${esc(narrateRun(q, rid)) || '<span class="mut">no gold turns or no stage log</span>'}</div>`;
  if (q.fix) h += `<div><b>Generic fix:</b> ${esc(q.fix)}</div><div><b>Decision mechanism:</b> ${esc(q.mech || '')}</div><div><b>Built feature:</b> ${esc(q.feat || '')}</div><div><b>Runtime cost:</b> ${esc(q.cost || '')}</div>`;
  h += gapBlock(q) + '</div>';
  h += `<div class="sec"><h3>Gold-turn position in every leg, every run (where each gold turn dropped out)</h3><div class="mut">vector / lexical = rank in that leg (top 60); fused = rank after fusion; rerank = rank by reranker score within the pool; final = rank in the final list; in context = reached the reader.</div>${ranksTable(q, runs)}</div>`;
  h += `<div class="sec"><h3>Read pipeline, step by step: ${esc(rid)}</h3><div class="mut">Each step says whether the run logged it, whether the explorer reconstructed it offline from the logged data and the code, or whether it is missing (with the fix). The write side of the gold turns is in the memory view (button above).</div>${readTimeline(q, rid)}</div>`;
  return h;
}
function detailOpb(q, nav, sel, runs, rid) {
  let h = nav + `<h2>${esc(q.k)} <small class="mut">OP-Bench ${esc(q.cat)}</small> <span class="${q.ok ? 'ok' : 'bad'}">${q.ok ? 'baseline passes (>= 0.5)' : 'baseline below 0.5'}</span></h2><div>${sel}</div>`;
  h += `<div class="warn">OP-Bench has no licence: this view is local only.</div>`;
  h += `<div class="sec"><h3>Probe</h3><div class="box">${esc(q.q)}</div><div class="mut">persona: ${esc(q.persona)}; item ${esc(q.it)}${q.ho ? ' <b class="o">[held-out persona]</b>' : ' [dev persona]'}</div></div>`;
  h += `<div class="sec"><h3>Answers and judge scores: BASE (no memory), baseline, screens</h3><table><tr><th>run</th><th>judge score</th><th>pass</th><th>persona share of context</th><th>context lines / tokens</th><th>answer</th></tr>`;
  runs.forEach(r0 => { const r = q.r[r0]; h += `<tr class="${r0 === rid ? 'sel' : ''}"><td>${esc(r0)}${r0 === M.opb_base ? ' <small>(no memory)</small>' : ''}</td><td class="n">${f1(r.s, 2)}</td><td class="${r.ok ? 'ok' : 'bad'}">${r.ok ? '&#10003;' : '&#10007;'}</td><td class="n">${r.ps == null ? '-' : f1(100 * r.ps, 0) + '%'}</td><td class="n">${r.n ?? ''} / ${r.ct ?? ''}${(r.fl || []).length ? ' ' + r.fl.map(x => `<span class="tag s">${x}</span>`).join('') : ''}</td><td>${esc(r.a)}</td></tr>`; });
  h += '</table></div>';
  h += `<div class="sec"><h3>Forensics: failure class</h3>`;
  if (q.st) { h += `<div><b>${esc(q.sub)}</b> <span class="mut">[${esc(q.code)}]</span></div>`; const s = q.sig; if (s) h += `<div class="mut">signals: persona share ${s.persona_share}, leaked context tokens re-used in the answer ${s.leak_n}, second-person reference cues ${s.ref_cues}, best question/context word overlap ${s.best_overlap}, affirmation ${s.affirm}, role inversion ${s.role_inv}</div>`; h += `<div><b>Generic fix:</b> ${esc(q.fix || '')}</div><div><b>Decision mechanism:</b> ${esc(q.mech || '')}</div><div><b>Built feature:</b> ${esc(q.feat || '')}</div><div><b>Runtime cost:</b> ${esc(q.cost || '')}</div>`; }
  else if (q.ok) h += '<div class="g">Probe passes in the baseline (score &ge; 0.5): no failure class.</div>';
  else h += '<div class="mut">Below 0.5 in the baseline, no failure class assigned: the OP-Bench failure catalogue covers the dev personas only' + (q.ho ? ' (this is a held-out persona)' : '') + '.</div>';
  h += gapBlock(q) + '</div>';
  h += `<div class="sec"><h3>Read pipeline, step by step: ${esc(rid)}</h3><div class="mut">${(q.r[rid] || {}).tr ? 'This run kept a --trace-full stage log: legs, fusion, rerank, assembly, the exact assistant prompt, the raw reply and the judge prompt are logged.' : 'This run keeps no forensics stage log, so legs, fusion and rerank are not logged; the official assistant prompt, the answer and the OP-Bench judge are reconstructed from the official templates.'}</div>${readTimeline(q, rid)}</div>`;
  return h;
}

/* ------------------------------------------------------------------ write timeline (per stored record) */
function pickWT(c, t) { for (const r of Object.keys(D.wtrace || {})) { const x = ((D.wtrace[r] || {})[c] || {})[t]; if (x) return x; } return null; }
function allDerived(c) { let out = []; for (const r of Object.keys(D.derived || {})) out = out.concat((D.derived[r] || {})[c] || []); return out; }
function evOf(wt, kind) { return ((wt && wt.engine_events) || []).filter(e => e.kind === kind); }
function toggleW(conv, t, btn) {
  const tr = btn.closest('tr'); const nx = tr.nextElementSibling;
  if (nx && nx.classList.contains('wrow')) { nx.remove(); return; }
  const n = document.createElement('tr'); n.className = 'wrow'; n.innerHTML = `<td colspan="8">${writeSteps(conv, t)}</td>`; tr.after(n);
}
function writeSteps(c, t) {
  const rid = M.loc_ref, x = turnInfo(c, t), m = memRec(c, t) || {}, sg = (D.wsig[c] || {})[t] || {}, wt = pickWT(c, t), info = D.runinfo[rid] || {};
  const cv = k => covOf(rid, k); const bw = wt ? 'L' : null; let h = '';
  const ch = (name, v) => v ? `<span class="tag e">${esc(name)}</span>` : '';
  { const [st, note] = cv('W1'); h += stepBox('W1. Input turn', st, note, `<div><b>${esc(t)}</b> · ${esc(x[3])} · ${esc(x[4] || '')} · speaker <b>${esc(x[0])}</b> (role: ${x[0] === 'assistant' ? 'assistant' : 'human participant'})</div>${pre(x[1])}`, true); }
  { const [st, note] = cv('W2'); h += stepBox('W2. Validation', st, note, `<div>non-empty: <b>${sg.n === 0 ? 'no' : 'yes'}</b> · ${sg.n ?? x[1].length} characters · written: <b>${m.w === false ? 'NO' : 'yes'}</b> (logged)</div>`); }
  { const [st, note] = cv('W3'); h += stepBox('W3. Firewall decision and signals', st, note, `<div><b>Decision (logged):</b> ${m.q ? '<b class="r">QUARANTINED</b>' : 'passed'} · trust <b>${m.tr ?? '?'}</b> · status <b>${esc(m.st || '?')}</b>${wt && wt.stored ? ' · instruction_flag ' + esc(wt.stored.instruction_flag) : ''}</div><div><b>Deterministic screens (reconstructed with the engine's own rule code):</b> instruction-shaped ${sg.i ? '<b class="r">YES</b>' : 'no'} · extended patterns ${sg.ie ? '<b class="r">YES</b>' : 'no'} · semantic risk ${esc((sg.sr || []).join(', ') || 'none')}</div>${(() => { const f = evOf(wt, 'firewall')[0]; if (!f) return '<div class="mut">Embedding-outlier and MINJA prefix-repeat scores and the verdict reasons: not logged in this run (fix: rerun with --trace-full on this commit).</div>'; return `<div><b>Engine signals (logged):</b> role ${esc(f.role)} · trust tier ${esc(f.trust_tier)} · trust ${f.trust} · instruction screen ${esc(f.instruction_screen)} flag ${f.instruction_flag} · semantic risk ${esc(JSON.stringify(f.semantic_risk))}</div><div>embedding outlier: nearest similarity ${f.embedding_outlier.nearest_similarity == null ? '-' : f1(f.embedding_outlier.nearest_similarity, 3)} (threshold ${f.embedding_outlier.threshold}, ${f.embedding_outlier.n_neighbours}/${f.embedding_outlier.min_neighbours} neighbours, on=${f.embedding_outlier.on}) flagged <b>${f.embedding_outlier.flagged}</b> · MINJA prefix (${f.minja_bridge.prefix_chars} chars, ${f.minja_bridge.n_recent} recent, on=${f.minja_bridge.on}) flagged <b>${f.minja_bridge.flagged}</b> · query-anomaly z ${f.query_anomaly_z ?? '-'}</div><div><b>Decision:</b> quarantine ${f.quarantine}, anomalous ${f.anomalous}; reasons: ${esc((f.reasons || []).join(', ') || 'none')}</div>`; })()}`); }
  { const [st, note] = cv('W4'); h += stepBox('W4. Redaction / PII', st, note, `<div>stored text identical to source: <b>${m.ti === false ? 'NO (altered)' : m.ti ? 'yes (no redaction applied)' : '?'}</b></div>${m.stx ? '<div>stored: ' + esc(m.stx) + '</div>' : ''}<div>PII hits (reconstructed): ${esc((sg.pii || []).join(', ') || 'none')}</div>`); }
  { const st = wt ? 'L' : 'M'; const tags = wt && wt.stored ? wt.stored.tags : null; h += stepBox('W5. Tags (perspective spk/addr/sub/mod/pol/scope, sensitivity, participants, src, trust)', st, wt ? 'write_trace.jsonl (--trace-full)' : cv('W5')[1], tags ? `<div>tags: ${tags.map(z => `<span class="tag s">${esc(z)}</span>`).join('') || 'none'}</div><div>pii_tier ${esc(wt.stored.pii_tier)} · source ${esc(JSON.stringify(wt.stored.source))}</div>` : `<div><b>Reconstructed sensitivity grade of the text:</b> ${esc(sg.sg || 'none')} ${esc((sg.sc || []).join(', '))} <span class="mut">(applies only if sensitivity tagging is on)</span></div><div class="mut">record tags, participants / visibility and src provenance: not logged in this run; fix: --trace-full (write_trace.jsonl).</div>`); }
  { const [st, note] = cv('W6'); const e = info.embedding || {}; h += stepBox('W6. Embedding', st, note, `<div>model <b>${esc(e.model)}</b> · dim <b>${esc(e.dim)}</b> · device ${esc(e.device)} · dtype ${esc(e.dtype)} <span class="mut">(vectors are not dumped)</span></div>`); }
  { const [st, note] = cv('W7'); h += stepBox('W7. Vector + lexical index', st, note, `<div>record id <small>${esc(m.rid || '-')}</small> · type ${esc(m.mt || '?')} · group ${esc(m.g || '?')} · batch size ${esc(m.bs ?? '?')}</div>`); }
  { const [st, note] = cv('W8'); const ce = evOf(wt, 'conflict'); h += stepBox('W8. Dedup / conflict-ladder verdict (ADD / UPDATE / NOOP / INVALIDATE / CONTEST)', st, note, `<div>record status <b>${esc(m.st || '?')}</b></div>` + (ce.length ? ce.map(e => `<div><b>${esc(e.verdict)}</b> by rule <b>${esc(e.rule)}</b> · incumbent ${esc(e.incumbent || 'none')} · key ${esc(JSON.stringify(e.key))}${e.trust_incumbent != null ? ' · trust in/incumbent ' + e.trust_incoming + ' / ' + e.trust_incumbent : ''}</div>`).join('') : `<div class="mut">${wt ? 'no conflict event: an episodic turn does not pass the keyed conflict ladder' : 'not logged in this run; fix: --trace-full'}</div>`)); }
  { const has = !!m.wt; h += stepBox('W9. Timings (I73 write_timers)', has ? 'L' : 'M', has ? 'carried on the first record of its batch' : cv('W9')[1], has ? `<pre class="box">${esc(JSON.stringify(m.wt, null, 1))}</pre>` : '<div class="mut">no timer lines for this record</div>'); }
  { const d = allDerived(c).filter(z => (z.parent_turns || []).includes(t)); h += stepBox('W10. Derived records (sleep cycle) with this turn as a parent', cv('W10')[0], cv('W10')[1], d.length ? d.map(z => `<div class="box">${esc(z.content)}<br><small>${esc(z.stored && z.stored.memory_type)} · parents ${esc((z.parent_turns || []).join(', '))}</small></div>`).join('') : '<div class="mut">none</div>'); }
  return `<div class="timeline">${h}</div>`;
}

/* ------------------------------------------------------------------ memory (write side) */
function openMem(b, k, rid) {
  const q = D.q[QI[b + '|' + k]], r = q.r[rid] || {};
  S.mem = Object.assign(S.mem || {}, {conv: conv(q), qk: [b, k], run: rid, gold: new Set(q.ev || []), dist: new Set(q.dv || []), ctx: new Set(r.ctx || []), persona: q.persona || null, only: false});
  closeQ(); setTab('memory');
}
function memFilter(k, v) { S.mem[k] = v; const y = window.scrollY; render(); window.scrollTo(0, y); }
function viewMemory() {
  const m = S.mem = S.mem || {conv: M.dev_items[0]};
  m.gold = m.gold || new Set(); m.dist = m.dist || new Set(); m.ctx = m.ctx || new Set();
  const turns = Object.entries(D.tt[m.conv] || {}), rec = D.mem[m.conv] || {};
  const spk = [...new Set(turns.map(t => t[1][0]))], ses = [...new Set(turns.map(t => t[1][3]))];
  let written = 0, quar = 0; turns.forEach(([t]) => { const x = rec[t]; if (x && x.w !== false) written++; if (x && x.q) quar++; });
  let h = `<h2>Memory added (write side): conversation ${esc(m.conv)}</h2><div class="mut">Every turn of the conversation as the engine stored it (baseline stage log <code>ingest.jsonl</code>). ${turns.length} turns, ${written} written, ${quar} quarantined. ${Object.keys(M.ingest_diff || {}).length ? 'Write-side drift of screens against the baseline (turns whose written / quarantined / stored text / valid_from differ): ' + Object.entries(M.ingest_diff).map(([r, n]) => esc(r) + ' ' + n).join(', ') + '.' : ''} OP-Bench personas are the first speaker of these same conversations (the OP-Bench runs keep no ingest log).</div>`;
  h += `<div class="flt"><label>conversation<select onchange="memFilter('conv',this.value);S.mem.gold=new Set();S.mem.ctx=new Set();S.mem.dist=new Set();S.mem.qk=null;S.mem.persona=null;render()">${M.dev_items.map(c => `<option ${c === m.conv ? 'selected' : ''}>${c}</option>`).join('')}</select></label>
   <label>speaker<select onchange="memFilter('spk',this.value)"><option value="">all</option>${spk.map(s => `<option ${m.spk === s ? 'selected' : ''}>${esc(s)}</option>`).join('')}</select></label>
   <label>session<select onchange="memFilter('ses',this.value)"><option value="">all</option>${ses.map(s => `<option ${m.ses === s ? 'selected' : ''}>${esc(s)}</option>`).join('')}</select></label>
   <label>show<select onchange="memFilter('only',this.value==='1')"><option value="">all turns</option><option value="1" ${m.only ? 'selected' : ''}>highlighted only</option></select></label>
   <label>search<input type="text" size="24" value="${esc(m.text || '')}" onchange="memFilter('text',this.value)"></label>
   ${m.qk ? `<span class="tag s">highlighting ${esc(m.qk[1])} (${esc(m.run)}): ${m.gold.size} gold, ${m.ctx.size} in context <a href="#" onclick="openKey('${m.qk[0]}','${m.qk[1]}');return false">back to question</a> <a href="#" onclick="S.mem.qk=null;S.mem.gold=new Set();S.mem.ctx=new Set();S.mem.dist=new Set();S.mem.persona=null;render();return false">clear</a></span>` : '<span class="mut">open a question and press "open in memory view" to highlight its gold turns and the records it used</span>'}</div>
   <div class="legend mut"><span><i style="background:#c9f0d2"></i>gold evidence</span><span><i style="background:#ffe0b5"></i>distractor source</span><span><i style="background:#cfe0ff"></i>used in the context</span><span><i style="background:#eaf1ff"></i>persona's own turn (OP-Bench)</span></div>`;
  h += '<table><tr><th>turn</th><th>session / date</th><th>speaker</th><th>written</th><th>trust / type / group</th><th>valid_from</th><th>record id</th><th>stored text (full)</th></tr>';
  let shown = 0, first = null;
  turns.forEach(([t, x]) => {
    const isg = m.gold.has(t), isd = m.dist.has(t), isc = m.ctx.has(t), isp = m.persona && x[0] === m.persona;
    if (m.spk && x[0] !== m.spk) return; if (m.ses && x[3] !== m.ses) return;
    if (m.only && !(isg || isd || isc)) return;
    if (m.text && !(x[1] + ' ' + t).toLowerCase().includes(m.text.toLowerCase())) return;
    const r = rec[t] || {};
    const bg = isg ? '#c9f0d2' : isd ? '#ffe0b5' : isc ? '#cfe0ff' : isp ? '#eaf1ff' : '';
    if (!first && (isg || isc)) first = t;
    shown++;
    h += `<tr id="m_${esc(t.replace(':', '_'))}" style="${bg ? 'background:' + bg : ''}"><td><b>${esc(t)}</b>${isg ? ' <b class="g">GOLD</b>' : ''}${isc ? ' <small>used</small>' : ''}<br><button class="btn" onclick="toggleW('${m.conv}','${t}',this)">write pipeline</button></td><td><small>${esc(x[3])}<br>${esc(x[4] || '')}</small></td><td>${esc(x[0])}</td><td>${r.w === false ? '<b class="r">no</b>' : r.w ? 'yes' : '-'}${r.q ? ' <b class="r">quarantined</b>' : ''}</td><td><small>${r.tr ?? ''} ${esc(r.mt || '')} ${esc(r.g || '')} ${r.ex ? esc(JSON.stringify(r.ex)) : ''}</small></td><td><small>${esc((r.vf || '').replace('+00:00', ''))}</small></td><td><small class="mut">${esc(r.rid || '')}</small></td><td>${esc(r.stx || x[1])}${r.stx ? '<br><small class="o">stored text differs from source: ' + esc(x[1]) + '</small>' : ''}${x[2] ? '<br><small class="mut">reader-side: ' + esc(x[2]) + '</small>' : ''}</td></tr>`;
  });
  h += `</table><div class="mut">${shown} rows shown.</div>`;
  const dv = allDerived(m.conv);
  h += `<h3>Derived records (sleep cycle) of ${esc(m.conv)}</h3>` + (dv.length ? dv.map(z => `<div class="box">${esc(z.content)}<br><small>${esc(z.stored && z.stored.memory_type)} · parents ${esc((z.parent_turns || []).join(', '))} · tags ${esc(((z.stored || {}).tags || []).join(', '))}</small></div>`).join('') : `<div class="mut">none: ${(D.runinfo[M.loc_ref] || {}).build_sleep === false ? 'the sleep cycle is off in this run (build_sleep=false), so no fact, card, summary or graph edge was derived, and no derived record was skipped (I72 concerns the sleep-cycle deposit)' : 'not logged in this run; fix: --trace-full writes derived rows into write_trace.jsonl'}</div>`);
  S.memScroll = first ? 'm_' + first.replace(':', '_') : null;
  return h;
}

/* ------------------------------------------------------------------ sidebar tree */
let TREE = null;
function mkNode(label, test, parentTest) { const t = parentTest ? q => parentTest(q) && test(q) : test; const nd = {label, test: t, kids: [], leaf: false}; let n = 0, c = 0; D.q.forEach(q => { if (t(q)) { n++; if (q.ok) c++; } }); nd.n = n; nd.c = c; nd.w = n - c; return nd; }
function buildTree() {
  const roots = [];
  const L = mkNode(M.all_convs ? 'LoCoMo all 10 conversations' : 'LoCoMo dev', q => q.b === 'loc');
  ['single-hop', 'multi-hop', 'temporal', 'open-domain', 'adversarial'].forEach(cat => {
    const cn = mkNode(cat + (cat === 'adversarial' ? ' (cat 5)' : ''), q => q.cat === cat, L.test);
    M.dev_items.forEach(cv => { const k = mkNode(cv + (M.all_convs ? (M.split_dev.includes(cv) ? ' [dev]' : ' [held-out]') : ''), q => q.it === cv, cn.test); k.leaf = true; if (k.n) cn.kids.push(k); });
    L.kids.push(cn);
  });
  const O = mkNode(M.all_convs ? 'OP-Bench all 10 personas' : 'OP-Bench dev', q => q.b === 'opb');
  ['irrelevance_easy', 'irrelevance_hard', 'sycophancy', 'diversity'].forEach(task => {
    const tn = mkNode(task, q => q.cc === task, O.test);
    const subs = [...new Set(D.q.filter(q => q.b === 'opb' && q.cc === task).map(q => q.cat))].sort();
    const addPersonas = parent => { [...new Set(D.q.filter(q => q.b === 'opb' && parent.test(q)).map(q => q.it))].sort().forEach(p => { const k = mkNode(p + (M.all_convs ? (M.split_dev_personas.includes(p) ? ' [dev]' : ' [held-out]') : ''), q => q.it === p, parent.test); k.leaf = true; parent.kids.push(k); }); };
    if (subs.length === 1 && subs[0] === task) addPersonas(tn);
    else subs.forEach(sb => { const sn = mkNode(sb.replace(task + '/', ''), q => q.cat === sb, tn.test); addPersonas(sn); tn.kids.push(sn); });
    O.kids.push(tn);
  });
  roots.push(L, O);
  const ST = mkNode('By stage (LoCoMo failures, OP-Bench classes)', q => !!q.st);
  Object.keys(STAGE).forEach(s => { const sn = mkNode(STAGE[s], q => q.b === 'loc' && q.st === s, ST.test); if (!sn.n) return; [...new Set(D.q.filter(q => q.b === 'loc' && q.st === s).map(q => q.code))].sort().forEach(cd => { const k = mkNode(cd + ' ' + (D.q.find(q => q.b === 'loc' && q.code === cd) || {}).sub, q => q.code === cd, sn.test); sn.kids.push(k); }); ST.kids.push(sn); });
  const op = mkNode('OP-Bench failure class', q => q.b === 'opb' && !!q.st, ST.test);
  [...new Set(D.q.filter(q => q.b === 'opb' && q.st).map(q => q.code))].sort().forEach(cd => op.kids.push(mkNode(cd, q => q.code === cd, op.test)));
  ST.kids.push(op);
  roots.push(ST);
  const G = mkNode('By gap id', q => (q.gaps || []).length > 0);
  [...new Set(D.q.flatMap(q => q.gaps || []))].sort().forEach(g => G.kids.push(mkNode(g + ' ' + ((D.gaps[g] || {}).t || '').slice(0, 40), q => (q.gaps || []).includes(g), G.test)));
  roots.push(G);
  let id = 0; const num = (nd, p) => { nd.id = p; nd.kids.forEach((k, i) => num(k, p + '.' + i)); }; roots.forEach((r, i) => num(r, 't' + i));
  return roots;
}
const NODES = {}; function indexNodes(arr) { arr.forEach(n => { NODES[n.id] = n; indexNodes(n.kids); }); }
function treeHtml() {
  if (!TREE) { TREE = buildTree(); indexNodes(TREE); }
  const one = nd => {
    const lab = `<span class="tl ${S.node === nd.id ? 'on' : ''}" onclick="event.preventDefault();event.stopPropagation();selNode('${nd.id}')">${esc(nd.label)} <small class="mut">${nd.n} <b class="g">${nd.c}&#10003;</b> <b class="r">${nd.w}&#10007;</b></small></span>`;
    if (!nd.kids.length && !nd.leaf) return `<div class="tleaf">${lab}</div>`;
    const open = S.open[nd.id] || (S.node && S.node.startsWith(nd.id + '.')) ? 'open' : '';
    return `<details ${open} ontoggle="S.open['${nd.id}']=this.open;${nd.leaf ? `fillLeaf('${nd.id}',this)` : ''}"><summary>${lab}</summary>${nd.kids.map(one).join('')}${nd.leaf ? '<div class="leaves"></div>' : ''}</details>`;
  };
  return TREE.map(one).join('');
}
function selNode(id) { S.node = S.node === id ? null : id; S.shown = 200; const y = window.scrollY; render(); window.scrollTo(0, y); }
function fillLeaf(id, el) {
  const box = el.querySelector('.leaves'); if (!el.open || box.dataset.done) return; box.dataset.done = 1;
  const nd = NODES[id]; const qs = D.q.map((q, i) => [q, i]).filter(([q]) => nd.test(q));
  box.innerHTML = qs.map(([q, i]) => `<a href="#" class="${q.ok ? 'ok' : 'bad'}" title="${esc(q.q || '')}" onclick="leafOpen('${id}',${i});return false">${esc(q.b === 'loc' ? q.id : q.id.split(':').slice(-2).join(':'))}</a>`).join(' ');
}
function leafOpen(id, qi) { S.node = id; S.shown = 100000; render(); const n = S.list.indexOf(qi); if (n >= 0) openQ(n); }

/* ------------------------------------------------------------------ data gaps */
function viewGaps() {
  const runs = Object.keys(D.coverage), steps = Object.keys(D.coverage[runs[0]]);
  let cov = `<h2>Pipeline coverage per run (L logged, R reconstructed, M missing)</h2><div class="mut">Hover a cell for the note. Write steps W1-W10 are per stored record, read steps R1-R12 per question.</div><div style="overflow:auto"><table><tr><th>step</th>${runs.map(r => `<th><small>${esc(r)}</small></th>`).join('')}</tr>` + steps.map(st => `<tr><td>${esc(st)}</td>${runs.map(r => { const c = D.coverage[r][st] || ['M', '']; return `<td class="cov c${c[0]}" title="${esc(c[1])}">${c[0]}</td>`; }).join('')}</tr>`).join('') + '</table></div>';
  return cov + '<h2>Data gaps found while building this file</h2><ul>' + M.data_gaps.map(x => `<li>${esc(x)}</li>`).join('') + `</ul><h2>Notes</h2><ul><li>${esc(M.licence)}</li><li>${esc(M.gap_note)}</li><li>Failure stages for LoCoMo come from the hand-read catalogue of the baseline (154 wrong answers); for screens the explorer shows the outcome (fixed / broke / same) and the mechanical gold-turn funnel, not a re-read.</li><li>Running screens are listed with their progress; their finished rows already appear in the question strips, but no verdict is computed until the run writes its summary row. Re-run the generator to refresh.</li></ul>`;
}
render();
</script></body></html>
"""

if __name__ == "__main__":
    main()
