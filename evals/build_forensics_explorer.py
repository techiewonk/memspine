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

Dev only. The held-out conversations (conv-43/44/47/48/49/50) are dropped the moment a dataset is loaded, and the
script aborts if any result or forensics row names one.

Inputs: baseline ``xb-loc-dev`` / ``xb-opb-dev`` (+ ``--forensics`` dirs), no-memory ``opb-base-dev``, every screen,
``analysis/catalogue/locomo_dev_failures.jsonl`` (stage codes), ``runs/_analysis/opbench_dev_failures*.jsonl``
(OP-Bench classes), ``analysis/locomo_errata.json``, ``analysis/GAP_REGISTER.md`` (gap-row text) and
``analysis/FAILURE_FORENSICS_BASELINE_2026-10-10.md`` (lever table, mirrored in ``LEVERS``).
"""

from __future__ import annotations

import argparse
import collections
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


def clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def run_dir(run: str) -> Path | None:
    for p in sorted(RUNS.glob(f"{run}--*")):
        if p.is_dir() and not p.name.endswith("--forensics"):
            return p
    return None


def check_heldout(rows: list[dict], what: str) -> None:
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
    tt: dict[str, dict] = {}
    qs: dict[str, dict] = {}
    for item in ds.items():
        if item.item_id not in dev:
            continue
        tt[item.item_id] = {
            t.turn_id: [t.speaker, t.text, None, t.session_id, t.timestamp] for t in item.history
        }
        for q in item.queries:
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
        if item.item_id not in dev:
            continue
        for q in item.queries:
            meta[q.query_id] = dict(
                item=item.item_id, persona=item.meta.get("persona"), qm=dict(q.meta)
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
        if not p.is_dir() or "--" in n or n.startswith(("xb-", "opb-base", "_")):
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


def load_fx(
    run: str, gold_by_key: dict[str, list[str]], tt: dict, ann_seen: dict
) -> dict[str, dict]:
    p = RUNS / f"{run}--forensics" / "forensics.jsonl"
    out = {}
    for x in jl(p):
        check_heldout([x], run + " forensics")
        key = f"{x['item']}:{x['query_id']}"
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
    for x in jl(RUNS / f"{run}--forensics" / "ingest.jsonl"):
        check_heldout([x], run + " ingest")
        rec = clean(
            dict(
                w=x.get("written"), q=x.get("quarantined"), tr=x.get("trust"),
                vf=x.get("valid_from"), mt=x.get("memory_type"), g=x.get("group_id"),
                rid=x.get("record_id"), st=x.get("status"), ti=x.get("text_identical"),
                stx=None if x.get("text_identical") else x.get("stored_text"),
                bs=x.get("batch_size"),
                ex={k: v for k, v in x.items() if k not in INGEST_KNOWN} or None,
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
    for x in jl(ANALYSIS / "catalogue" / "locomo_dev_failures.jsonl"):
        cat_rows[x["query_id"]] = x
    opb_cat = {x["query_id"]: x for x in jl(OUT_DIR / "opbench_dev_failures_catalogue.jsonl")}
    opb_sig = {x["query_id"]: x for x in jl(OUT_DIR / "opbench_dev_failures.jsonl")}

    # ---- baseline + screens
    n_loc_exp = sum(1 for k in loc_q if k.startswith(("conv-26:", "conv-30:")))
    ref = load_run(LOC_REF, len(loc_q))
    if ref["status"] != "complete" or ref["n"] != len(loc_q):
        raise SystemExit(f"baseline {LOC_REF} incomplete: {ref['n']} of {len(loc_q)}")
    opb_ref = load_run(OPB_REF, len(opb_meta))
    opb_base = load_run(OPB_BASE, len(opb_meta))

    screens = discover_screens(args.extra_screen)
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
    opb_ids = [OPB_BASE, OPB_REF] + [sruns[s]["opb"]["id"] for s in screens if sruns[s]["opb"]]
    loc_runs = {LOC_REF: ref}
    loc_runs.update({sruns[s]["loc"]["id"]: sruns[s]["loc"] for s in screens if sruns[s]["loc"]})
    opb_runs = {OPB_BASE: opb_base, OPB_REF: opb_ref}
    opb_runs.update({sruns[s]["opb"]["id"]: sruns[s]["opb"] for s in screens if sruns[s]["opb"]})

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
        if e["cmp_loc"] and e["loc"]["status"] == "complete":
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
                )
            )
        opb_c[rid] = rows
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
                    r=rr,
                )
            )
        )

    for qid, m in opb_meta.items():
        row = opb_c[OPB_REF].get(qid)
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
                rr[rid] = clean(dict(rw, fl=gate_flags[rid].get(qid) or None))
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
                    o=o,
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
    for lv in LEVERS:
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
            100 * (base_loc["correct"] + sum(lv["q15"] for lv in LEVERS)) / base_loc["n"], 2
        ),
        levers=lev_out,
        doc="FAILURE_FORENSICS_BASELINE_2026-10-10.md section 2; expected gains are capture-rate assumptions, measured where a screen has finished",
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
    args = ap.parse_args()
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
   <div class="card"><b>${f1(100 * bl.correct / bl.n)}%</b>LoCoMo dev cat 1-5<br><small>${bl.correct}/${bl.n}, ${bl.wrong} wrong</small></div>
   <div class="card"><b>${f1(100 * bl.c14_correct / bl.c14_n)}%</b>LoCoMo cat 1-4<br><small>${bl.c14_correct}/${bl.c14_n} (target 89.6)</small></div>
   <div class="card"><b>${f1(bo.subs.official_overall)}</b>OP-Bench overall<br><small>${bo.n} probes, ${bo.low} below 0.5</small></div>
   <div class="card"><b>${f1((bo.base || {}).official_overall)}</b>no-memory BASE<br><small>${bo.base_low} below 0.5</small></div>
   <div class="card"><b>${D.screens.length}</b>screens<br><small>${runs} running / partial</small></div>
   <div class="card"><b>${bl.errata_n}</b>errata-flagged dev q<br><small>${bl.errata_wrong} of them wrong</small></div></div>`;
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

/* retrieval trace: every leg side by side, gold marked, exact gold position per leg */
function traceBlock(q, rid) {
  const r = q.r[rid];
  if (!r || !r.tr) return `<div class="nl" style="padding:6px">No stage log for ${esc(rid)}${q.b === 'opb' ? ' (OP-Bench runs keep no forensics rows: only the retrieved ids below)' : ' (this run predates the forensics logging)'}.</div>`;
  const T = r.tr, c = conv(q), gold = goldSet(q), dist = new Set(q.dv || []), cap = M.leg_cap;
  const pool = T.p.slice().sort((a, b) => b[1] - a[1]).slice(0, cap);
  const gi = {v: 1, l: 2, f: 4, rr: 5, fin: 7};
  const cols = [['Vector leg (cosine)', T.v, 'v'], ['Lexical leg (BM25)', T.l, 'l']];
  Object.entries(T.x || {}).forEach(([n, l]) => cols.push(['Extra leg: ' + n, l, 'x:' + n]));
  cols.push(['Fused (RRF) = rerank pool', T.f, 'f'], ['Reranker order (score)' + (T.pr ? ' ' + T.pr : ''), pool, 'rr'], ['Final hits (top-k)', T.fin, 'fin']);
  const rows = Math.max(...cols.map(x => x[1].length));
  let h = `<div class="mut">Top ${cap} per leg (exact gold ranks below each header, from the full 60-deep lists). Green = gold evidence${dist.size ? ', orange = the turn the cat-5 distractor answer was built from' : ''}.</div><div style="overflow:auto"><table class="trace"><tr>` + cols.map(x => `<th>${esc(x[0])}<br><small class="mut">${x[1].length} shown</small></th>`).join('') + '</tr>';
  if (r.g && r.g.length) h += '<tr>' + cols.map(x => { const k = x[2]; const p = r.g.map(g => { const v = k.startsWith('x:') ? (g[3] || {})[k.slice(2)] : g[gi[k]]; return `${g[0]} ${v == null ? '<span class="r">-</span>' : '#' + v}`; }).join('<br>'); return `<td class="gp"><b class="g">GOLD</b><br>${p}</td>`; }).join('') + '</tr>';
  for (let i = 0; i < rows; i++) {
    h += '<tr>' + cols.map(x => {
      const e = x[1][i]; if (!e) return '<td></td>';
      const isg = gold.has(e[0]), isd = dist.has(e[0]);
      return `<td class="${isg ? 'gold' : isd ? 'dist' : ''}"><small class="mut">#${i + 1}</small> <b>${esc(e[0])}</b> ${isg ? '<b class="g">GOLD</b> ' : ''}${isd ? '<b class="o">DIST</b> ' : ''}<small>${f1(e[1], 3)}</small><br><small class="mut">${snippet(c, e[0])}</small></td>`;
    }).join('') + '</tr>';
  }
  h += '</table></div>';
  const info = [];
  if (r.dec) info.push('decisions: ' + r.dec.map(d => `${d.task} = ${d.final || d.label} (${d.adapter || ''}${d.confidence != null ? ', conf ' + f1(d.confidence, 2) : ''}${d.used === false ? ', not used' : ''})`).join('; '));
  if (r.rb) info.push('relevance bypass: ' + (r.rb.fired ? 'FIRED' : 'not fired') + ' (' + (r.rb.kind || r.rb.mode || '') + (r.rb.names ? ': ' + r.rb.names.join(', ') : '') + ')');
  if (r.legs) info.push('extra legs fired: ' + r.legs.join(', '));
  if (r.ce) info.push('<b class="r">context came back EMPTY (a gate closed it)</b>');
  if (!info.length && !(r.dec || r.rb)) info.push('<span class="mut">gate / decider decisions: not logged in this run</span>');
  return h + `<div style="margin-top:4px">${info.join('<br>')}</div>`;
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
  h += `<div class="sec"><h3>Retrieval trace: ${esc(rid)}</h3>${traceBlock(q, rid)}</div>`;
  h += `<div class="sec"><h3>Final context sent to the reader: ${esc(rid)}</h3>${ctxBlock(q, rid)}</div>`;
  const fl = runs.map(r => (q.r[r].fl || []).length ? `${esc(r)}: ` + q.r[r].fl.map(x => `<span class="tag s">${x}</span>`).join('') : '').filter(Boolean);
  h += `<div class="sec"><h3>Gates that touched this question</h3>${fl.join('<br>') || '<span class="mut">none</span>'}</div>`;
  return h;
}
function detailOpb(q, nav, sel, runs, rid) {
  let h = nav + `<h2>${esc(q.k)} <small class="mut">OP-Bench ${esc(q.cat)}</small> <span class="${q.ok ? 'ok' : 'bad'}">${q.ok ? 'baseline passes (>= 0.5)' : 'baseline below 0.5'}</span></h2><div>${sel}</div>`;
  h += `<div class="warn">OP-Bench has no licence: this view is local only.</div>`;
  h += `<div class="sec"><h3>Probe</h3><div class="box">${esc(q.q)}</div><div class="mut">persona: ${esc(q.persona)}; item ${esc(q.it)}</div></div>`;
  h += `<div class="sec"><h3>Answers and judge scores: BASE (no memory), baseline, screens</h3><table><tr><th>run</th><th>judge score</th><th>pass</th><th>persona share of context</th><th>context lines / tokens</th><th>answer</th></tr>`;
  runs.forEach(r0 => { const r = q.r[r0]; h += `<tr class="${r0 === rid ? 'sel' : ''}"><td>${esc(r0)}${r0 === M.opb_base ? ' <small>(no memory)</small>' : ''}</td><td class="n">${f1(r.s, 2)}</td><td class="${r.ok ? 'ok' : 'bad'}">${r.ok ? '&#10003;' : '&#10007;'}</td><td class="n">${r.ps == null ? '-' : f1(100 * r.ps, 0) + '%'}</td><td class="n">${r.n ?? ''} / ${r.ct ?? ''}${(r.fl || []).length ? ' ' + r.fl.map(x => `<span class="tag s">${x}</span>`).join('') : ''}</td><td>${esc(r.a)}</td></tr>`; });
  h += '</table></div>';
  h += `<div class="sec"><h3>Forensics: failure class</h3>`;
  if (q.st) { h += `<div><b>${esc(q.sub)}</b> <span class="mut">[${esc(q.code)}]</span></div>`; const s = q.sig; if (s) h += `<div class="mut">signals: persona share ${s.persona_share}, leaked context tokens re-used in the answer ${s.leak_n}, second-person reference cues ${s.ref_cues}, best question/context word overlap ${s.best_overlap}, affirmation ${s.affirm}, role inversion ${s.role_inv}</div>`; h += `<div><b>Generic fix:</b> ${esc(q.fix || '')}</div><div><b>Decision mechanism:</b> ${esc(q.mech || '')}</div><div><b>Built feature:</b> ${esc(q.feat || '')}</div><div><b>Runtime cost:</b> ${esc(q.cost || '')}</div>`; }
  else h += '<div class="g">Probe passes in the baseline (score &ge; 0.5): no failure class.</div>';
  h += gapBlock(q) + '</div>';
  h += `<div class="sec"><h3>Retrieval trace: ${esc(rid)}</h3>${traceBlock(q, rid)}</div>`;
  h += `<div class="sec"><h3>Records injected into the reader context: ${esc(rid)} (persona's own turns highlighted)</h3>${ctxBlock(q, rid)}</div>`;
  return h;
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
    h += `<tr id="m_${esc(t.replace(':', '_'))}" style="${bg ? 'background:' + bg : ''}"><td><b>${esc(t)}</b>${isg ? ' <b class="g">GOLD</b>' : ''}${isc ? ' <small>used</small>' : ''}</td><td><small>${esc(x[3])}<br>${esc(x[4] || '')}</small></td><td>${esc(x[0])}</td><td>${r.w === false ? '<b class="r">no</b>' : r.w ? 'yes' : '-'}${r.q ? ' <b class="r">quarantined</b>' : ''}</td><td><small>${r.tr ?? ''} ${esc(r.mt || '')} ${esc(r.g || '')} ${r.ex ? esc(JSON.stringify(r.ex)) : ''}</small></td><td><small>${esc((r.vf || '').replace('+00:00', ''))}</small></td><td><small class="mut">${esc(r.rid || '')}</small></td><td>${esc(r.stx || x[1])}${r.stx ? '<br><small class="o">stored text differs from source: ' + esc(x[1]) + '</small>' : ''}${x[2] ? '<br><small class="mut">reader-side: ' + esc(x[2]) + '</small>' : ''}</td></tr>`;
  });
  h += `</table><div class="mut">${shown} rows shown.</div>`;
  S.memScroll = first ? 'm_' + first.replace(':', '_') : null;
  return h;
}

/* ------------------------------------------------------------------ sidebar tree */
let TREE = null;
function mkNode(label, test, parentTest) { const t = parentTest ? q => parentTest(q) && test(q) : test; const nd = {label, test: t, kids: [], leaf: false}; let n = 0, c = 0; D.q.forEach(q => { if (t(q)) { n++; if (q.ok) c++; } }); nd.n = n; nd.c = c; nd.w = n - c; return nd; }
function buildTree() {
  const roots = [];
  const L = mkNode('LoCoMo dev', q => q.b === 'loc');
  ['single-hop', 'multi-hop', 'temporal', 'open-domain', 'adversarial'].forEach(cat => {
    const cn = mkNode(cat + (cat === 'adversarial' ? ' (cat 5)' : ''), q => q.cat === cat, L.test);
    M.dev_items.forEach(cv => { const k = mkNode(cv, q => q.it === cv, cn.test); k.leaf = true; if (k.n) cn.kids.push(k); });
    L.kids.push(cn);
  });
  const O = mkNode('OP-Bench dev', q => q.b === 'opb');
  ['irrelevance_easy', 'irrelevance_hard', 'sycophancy', 'diversity'].forEach(task => {
    const tn = mkNode(task, q => q.cc === task, O.test);
    const subs = [...new Set(D.q.filter(q => q.b === 'opb' && q.cc === task).map(q => q.cat))].sort();
    const addPersonas = parent => { [...new Set(D.q.filter(q => q.b === 'opb' && parent.test(q)).map(q => q.it))].sort().forEach(p => { const k = mkNode(p, q => q.it === p, parent.test); k.leaf = true; parent.kids.push(k); }); };
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
  return '<h2>Data gaps found while building this file</h2><ul>' + M.data_gaps.map(x => `<li>${esc(x)}</li>`).join('') + `</ul><h2>Notes</h2><ul><li>${esc(M.licence)}</li><li>${esc(M.gap_note)}</li><li>Failure stages for LoCoMo come from the hand-read catalogue of the baseline (154 wrong answers); for screens the explorer shows the outcome (fixed / broke / same) and the mechanical gold-turn funnel, not a re-read.</li><li>Running screens are listed with their progress; their finished rows already appear in the question strips, but no verdict is computed until the run writes its summary row. Re-run the generator to refresh.</li></ul>`;
}
render();
</script></body></html>
"""

if __name__ == "__main__":
    main()
