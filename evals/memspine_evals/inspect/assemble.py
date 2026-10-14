"""TI-3: the inspect bundle assembler (docs/v2/TRACE_INSPECTOR_PLAN.md section 1).

    python -m memspine_evals.inspect.assemble <run> [--compare <run2>] --data <dataset|demo> --out <dir>
                                              [--no-content]

Joins, for one run, what the harness and the engine already wrote into one *inspect bundle*:

  turns.jsonl  windows.jsonl  envelopes.jsonl  records.jsonl  links.jsonl  sleep.jsonl
  questions.jsonl  checks.jsonl  manifest.json                    (+ compare.json with --compare)

Inputs, all optional except ``results.jsonl`` (missing ones just leave their columns empty):

  <run>--forensics/ingest.jsonl (+ window_stats)     one row per dataset turn written
  <run>--forensics/forensics.jsonl[.gz]              ranked ids per stage per question, ``trace_full``
  <run>--trace/reads.jsonl[.gz], write_trace.jsonl   reader prompts and replies, stored-record signals
  <run>--memspine/results.jsonl, trace.jsonl         verdicts; per-turn deposit deltas
  <forensics>/tap/*.jsonl[.gz]                       TI-1 wiretap events (see ``TAP_EVENTS``)
  <forensics>/inspect/{records,links,sleep}.jsonl    TI-2 store snapshots (same schema as the bundle)
  the dataset (LoCoMo-shaped json, or ``demo``)      turn texts and gold evidence turns

``<run>`` is a run id (looked up under ``evals/runs``), or the path of any of its directories.
Text fields are only written with content on (the default); the output directory must be gitignored
when it holds real benchmark text (``evals/runs/_analysis/inspect/``).
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
EVALS = HERE.parent.parent
DEMO_PATH = HERE / "demo_conversation.json"
RUNS = EVALS / "runs"

STAGE_ORDER = ["ingest", "recall", "fusion", "gate", "rerank", "assembly", "reader"]
CAT = {"cat1": "multi-hop", "cat2": "temporal", "cat3": "open-domain", "cat4": "single-hop", "cat5": "adversarial"}

#: TI-1 tap events this assembler understands, with the fields it reads (extra fields are kept).
TAP_EVENTS = {
    "write.window.members": "window_id, item, session, member_turns|members, member_record_ids|rids, cut_reason, prepass",
    "write.extract.io": "window_id, prompt{id,hash,context_lines,known}|prompt_id, prompt_hash, prompt_text, reply_text, "
    "model, cached, tokens, ms, sections, items",
    "write.envelope.trace": "occ, write_id, window_id, stage, status, reason, before_hash, after_hash, fields_changed, "
    "time, grounding, resolve, integrate, record_id",
    "store.index.record": "rid|record_id, turn_id, vector, lexical, graph_node, index_text(_hash), edges",
    "job.stage.item": "stage, cycle, session_key, input_ids, output_ids, marker, calls, tokens, status",
    "compose.headers": "query_id, headers[{rid,kind,source_ids}] | rid, kind, source_ids",
    "read.rerank.components": "query_id, rid|turn, rel, rec, imp, trust | items[...]",
    "read.planner.io": "query_id, prompt, reply, plan",
}

TEXT_KEYS = {
    "text", "index_text", "content", "prompt_text", "reply_text", "context_text", "raw", "question", "gold",
    "answer", "source_text", "stored_text", "reply", "context_lines", "known", "value",
}


# ---------------------------------------------------------------------------------------------- io
def _open(path: Path):
    return gzip.open(path, "rt", encoding="utf-8") if path.suffix == ".gz" else open(path, encoding="utf-8")


def load_jsonl(path: Path | None) -> list[dict]:
    if path is None or not path.exists():
        return []
    out: list[dict] = []
    with _open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                out.append(row)
    return out


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows), encoding="utf-8")


def _first(dirs: list[Path], names: list[str]) -> Path | None:
    for d in dirs:
        for n in names:
            if (d / n).exists():
                return d / n
    return None


def resolve_run(run: str | Path) -> dict[str, Any]:
    """Run id or any of its directories -> ``{run_id, dirs}`` (every existing sibling directory)."""
    p = Path(run)
    if not p.exists() and (RUNS / str(run)).exists():
        p = RUNS / str(run)
    if not p.exists():
        raise FileNotFoundError(f"run not found: {run}")
    name = p.name
    base = re.sub(r"--(memspine|forensics|trace)$", "", name)
    parent = p.parent
    cands = [parent / f"{base}--{s}" for s in ("memspine", "forensics", "trace")] + [parent / base, p]
    dirs: list[Path] = []
    for c in cands:
        if c.is_dir() and c not in dirs:
            dirs.append(c)
    return {"run_id": base, "dirs": dirs, "given": p}


# ----------------------------------------------------------------------------------------- dataset
def load_dataset(dataset: Any) -> dict[str, dict]:
    """item_id -> {"turns": [...], "questions": {query_id: {...}}} from a LoCoMo-shaped json (or ``demo``)."""
    if dataset is None:
        return {}
    if isinstance(dataset, str | Path):
        path = DEMO_PATH if str(dataset) == "demo" else Path(dataset)
        raw = json.loads(path.read_text(encoding="utf-8"))
    else:
        raw = dataset
    if isinstance(raw, dict):
        raw = [raw]
    out: dict[str, dict] = {}
    for index, sample in enumerate(raw):
        if "sessions" in sample:  # the demo fixture shape: sessions[{session_id, date, turns}], questions[...]
            item = str(sample.get("conversation_id", index))
            turns = [{"item": item, "session": s["session_id"], "turn_id": str(t["turn_id"]), "speaker": str(t.get("speaker", "unknown")),
                      "ts": t.get("ts") or s.get("date"), "text": str(t.get("text", "")),
                      **({"custom": True} if t.get("custom") else {})} for s in sample["sessions"] for t in s["turns"]]
            qs = {str(q.get("query_id", i)): {"question": q.get("question", ""), "gold": q.get("gold"), "category": q.get("category"),
                                              "gold_turns": [str(g) for g in q.get("gold_turns") or []], "distractor_turns": [],
                                              **({"custom": True} if q.get("custom") else {})}
                  for i, q in enumerate(sample.get("questions") or [])}
            out[item] = {"turns": turns, "questions": qs}
            continue
        conv = sample.get("conversation", {})
        item = str(sample.get("sample_id", index))
        turns: list[dict] = []
        sessions = sorted(
            (int(m.group(1)), k) for k in conv if (m := re.match(r"^session_(\d+)$", k))
        )
        for number, key in sessions:
            for pos, t in enumerate(conv.get(key) or []):
                text = str(t.get("text", ""))
                if t.get("blip_caption"):
                    text = f"{text} [image: {t['blip_caption']}]".strip()
                turns.append({
                    "item": item, "session": f"session_{number}", "turn_id": str(t.get("dia_id") or f"{key}:{pos}"),
                    "speaker": str(t.get("speaker", "unknown")), "ts": conv.get(f"{key}_date_time"), "text": text,
                })
        questions: dict[str, dict] = {}
        for qi, qa in enumerate(sample.get("qa") or []):
            cat = qa.get("category")
            ev = [str(e) for e in qa.get("evidence") or []]
            gold_turns: list[str] = []
            if cat != 5:
                for e in ev:
                    for tok in re.split(r"[;,\s]+", e.strip()):
                        m = re.match(r"^D:?(\d+):(\d+)$", tok, re.I)
                        if m and (t := f"D{int(m.group(1))}:{int(m.group(2))}") not in gold_turns:
                            gold_turns.append(t)
            questions[f"{index}-{qi}"] = {
                "question": str(qa.get("question", "")), "gold": qa.get("answer"), "category": f"cat{cat}",
                "gold_turns": gold_turns, "distractor_turns": ev if cat == 5 else [],
            }
        out[item] = {"turns": turns, "questions": questions}
    return out


# ------------------------------------------------------------------------------------------ events
def _norm_event(r: dict) -> dict:
    """One tap row -> flat dict with ``_event`` (the event name); data/payload/fields merged to top level."""
    flat = {k: v for k, v in r.items() if k not in ("data", "payload", "fields")}
    for k in ("data", "payload", "fields"):
        if isinstance(r.get(k), dict):
            flat.update(r[k])
    name = None
    for k in ("event", "name", "type", "tap", "kind"):
        if isinstance(r.get(k), str) and r[k]:
            name = r[k]
            break
    if (name is None or "." not in name) and isinstance(r.get("stage"), str) and "." in r["stage"]:
        name = r["stage"]
    flat["_event"] = name or ""
    return flat


def _flatten_trace(row: dict) -> list[dict]:
    """A tap ``jsonl`` sink line is a whole request trace (``events`` and nested ``children``);
    expand it to one flat row per event, children after the parent's own events."""
    out = [{"stage": e.get("stage"), "fields": e.get("fields") or {}} for e in row.get("events") or []]
    for child in row.get("children") or []:
        out += _flatten_trace(child)
    return out


def load_tap(dirs: list[Path]) -> list[dict]:
    events: list[dict] = []
    seen: set[Path] = set()
    for d in dirs:
        tap = d / "tap"
        if not tap.is_dir():
            continue
        for p in sorted(tap.rglob("*.jsonl*")):
            if p in seen or p.suffix not in (".jsonl", ".gz"):
                continue
            seen.add(p)
            for r in load_jsonl(p):
                if isinstance(r.get("events"), list):  # the sink's own format: one trace per line
                    events += [_norm_event(e) for e in _flatten_trace(r)]
                else:
                    events.append(_norm_event(r))
    for i, e in enumerate(events):
        e["_seq"] = i
    return events


def _aslist(v: Any) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _tid(e: dict) -> str | None:
    return e.get("turn_id") or e.get("turn")


# --------------------------------------------------------------------------------- gold path logic
def rank_of(stage: list[dict] | None, turn: str) -> int | None:
    return next((r.get("r") for r in (stage or []) if r.get("turn") == turn), None)


#: lost_at -> the last stage output that still held the turn (TI-6 contract: leg/fused/pool/rerank/packed/none)
LAST_REACHED = {None: "packed", "ingest": "none", "recall": "none", "fusion": "leg", "gate": "fused",
                "rerank": "pool", "assembly": "rerank"}


def _run_started_at(created: Any, paths: list[Path | None]) -> str | None:
    """ISO-8601 UTC start of the run: the manifest's ``created_at``, else the earliest input file mtime."""
    from datetime import UTC, datetime

    if isinstance(created, str) and created:
        try:
            d = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=UTC)
            return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            pass
    times = [p.stat().st_mtime for p in paths if p is not None and p.exists()]
    return datetime.fromtimestamp(min(times), UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if times else None


def gold_path(
    gold_turns: list[str], f: dict | None, ing: dict[str, dict], in_ctx: set[str]
) -> tuple[list[dict], str | None]:
    """Per gold turn the stage that lost it (first-losing-stage logic of ``evals/forensics_report.py``) and
    the last stage it still reached. Returns ``(rows, primary_loss)``; ``primary_loss`` is the earliest
    loss over the question's gold turns, or None."""
    rows: list[dict] = []
    losses: list[str] = []
    for g in gold_turns:
        loss = None
        i = ing.get(g)
        if i is not None and not i.get("written", True):
            loss = "ingest"
        ranks = None
        rescued = None
        if f is not None:
            rk = {
                "vector": rank_of(f.get("vector"), g), "lexical": rank_of(f.get("lexical"), g),
                "extra": {n: rank_of(h, g) for n, h in (f.get("extra_legs") or {}).items()},
                "fused": rank_of(f.get("fused"), g), "pool": rank_of(f.get("pool"), g),
                "rerank": rank_of(f.get("rerank_scores"), g) if f.get("rerank_scores") else None,
                "final": rank_of(f.get("final"), g), "in_context": g in in_ctx,
            }
            ranks = rk
            if loss is None:
                any_leg = rk["vector"] or rk["lexical"] or any(v for v in rk["extra"].values())
                if not any_leg:
                    loss = "recall"
                elif rk["fused"] is None:
                    loss = "fusion"
                elif rk["pool"] is None:
                    loss = "gate"
                elif f.get("rerank_scores") and rk["final"] is None:
                    loss = "rerank"
                elif rk["final"] is None:
                    loss = "fusion"
                elif not rk["in_context"]:
                    loss = "assembly"
            if loss and loss != "ingest" and rk["in_context"]:
                rescued, loss = loss, None
        elif g not in in_ctx and loss is None:
            loss = "recall"  # results-level only: the stage is unknown, counted as retrieval
        last = LAST_REACHED[loss]
        rows.append({"turn": g, "in_context": g in in_ctx, "lost_at": loss, "last_stage_reached": last,
                     "rescued_from": rescued, "ranks": ranks})
        if loss:
            losses.append(loss)
    return rows, (min(losses, key=STAGE_ORDER.index) if losses else None)


def _join_forensics(results: list[dict], fx: list[dict]) -> dict[tuple, dict]:
    by_qid: dict[tuple, list[dict]] = defaultdict(list)
    by_text: dict[tuple, list[dict]] = defaultdict(list)
    for row in fx:
        if row.get("query_id") is not None:
            by_qid[(row.get("item"), row["query_id"])].append(row)
        else:
            by_text[(row.get("item"), row.get("query"))].append(row)
    seen: dict[tuple, int] = defaultdict(int)
    joined: dict[tuple, dict] = {}
    for r in results:
        qkey = (r["item_id"], r["query_id"])
        cands = by_qid.get(qkey)
        if cands:
            same = [c for c in cands if c.get("run_id") == r.get("run_id")]
            joined[qkey] = (same or cands)[-1]
            continue
        key = (r["item_id"], r.get("question"))
        n = seen[key]
        seen[key] += 1
        if n < len(by_text.get(key, [])):
            joined[qkey] = by_text[key][n]
    return joined


# ----------------------------------------------------------------------------------------- builders
def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _build_one(run: str | Path, dataset: Any = None) -> dict[str, Any]:
    info = resolve_run(run)
    dirs: list[Path] = info["dirs"]
    run_id = info["run_id"]
    ds = load_dataset(dataset)

    results_path = _first(dirs, ["results.jsonl"])
    results_all = load_jsonl(results_path)
    results = [r for r in results_all if r.get("kind") == "result"]
    run_ids = {r.get("run_id") for r in results}
    man_row = next((r for r in results_all if r.get("kind") not in ("result", "summary", "cost") and "system" in r), {})
    summary_row = next((r for r in results_all if r.get("kind") == "summary"), {})
    ingest = [r for r in load_jsonl(_first(dirs, ["ingest.jsonl"])) if r.get("run_id") is None or not run_ids or r.get("run_id") in run_ids]
    fx = load_jsonl(_first(dirs, ["forensics.jsonl", "forensics.jsonl.gz"]))
    reads = load_jsonl(_first(dirs, ["reads.jsonl", "reads.jsonl.gz"]))
    wtrace = load_jsonl(_first(dirs, ["write_trace.jsonl"]))
    trace = load_jsonl(_first(dirs, ["trace.jsonl"]))
    tap = load_tap(dirs)
    insp = [d / "inspect" for d in dirs if (d / "inspect").is_dir()]
    snap = {n: load_jsonl(_first(insp, [f"{n}.jsonl", f"{n}.jsonl.gz"])) for n in ("records", "links", "sleep", "graph_nodes")}
    checks = load_jsonl(_first(insp + dirs, ["checks.jsonl"]))
    fx_by_q = _join_forensics(results, fx)

    ing_by = {(r.get("item"), r["turn"]): r for r in ingest if "turn" in r}  # window_stats rows have none
    wt_by = {(r.get("item"), r["turn"]): r for r in wtrace if "turn" in r}
    dep_by = {(r.get("item_id"), r.get("turn_id")): r for r in trace if r.get("kind") == "deposit"}
    reads_by = {(r.get("item_id"), r.get("query_id")): r for r in reads}

    ing_by_item: dict[Any, dict] = defaultdict(dict)
    for (it_, tn_), r_ in ing_by.items():
        ing_by_item[it_][tn_] = r_
    rid_turn: dict[str, tuple] = {}
    turn_rid: dict[tuple, str] = {}
    for (item, turn), r in ing_by.items():
        if r.get("record_id"):
            turn_rid[(item, turn)] = r["record_id"]
            rid_turn[r["record_id"]] = (item, turn)
    for (item, turn), d in dep_by.items():
        for rid in (d.get("delta_t") or {}).get("record_ids") or []:
            rid_turn.setdefault(rid, (item, turn))
            turn_rid.setdefault((item, turn), rid)

    ev = defaultdict(list)
    for e in tap:
        ev[e["_event"]].append(e)

    # ---- windows + envelopes (tap)
    windows: dict[str, dict] = {}
    for e in ev["write.window.members"]:
        wid = e.get("window_id")
        if wid is None:
            continue
        w = windows.setdefault(wid, {"window_id": wid})
        w["item"] = e.get("item") or w.get("item")
        w["session"] = e.get("session") or w.get("session")
        rids = _aslist(e.get("member_record_ids") or e.get("rids"))
        turns = _aslist(e.get("member_turns") or e.get("members"))
        if not turns and rids:
            turns = [rid_turn[r][1] for r in rids if r in rid_turn]
        w["member_turns"] = [str(t) for t in turns]
        w["member_record_ids"] = rids
        w["cut_reason"] = e.get("cut_reason")
        pre = e.get("prepass")  # the engine emits the verdict as a plain string
        w["prepass"] = {"verdict": pre} if isinstance(pre, str) else pre
        for extra in ("status", "reason"):  # additive: planned | skipped | failed | cancelled
            if e.get(extra) is not None:
                w[extra] = e[extra]
    for e in ev["write.extract.io"]:
        wid = e.get("window_id")
        if wid is None:
            continue
        w = windows.setdefault(wid, {"window_id": wid})
        pr = e.get("prompt") if isinstance(e.get("prompt"), dict) else {}
        w["item"] = w.get("item") or e.get("item")
        w["prompt"] = {
            "id": pr.get("id") or e.get("prompt_id"), "hash": pr.get("hash") or e.get("prompt_hash"),
            "context_lines": pr.get("context_lines", e.get("context_lines")), "known": pr.get("known", e.get("known")),
        }
        call = dict(e.get("call") or {})
        for k_src, k_dst in (("model", "model"), ("cached", "cached"), ("tokens", "tokens"), ("ms", "ms"),
                              ("prompt_text", "prompt_text"), ("reply_text", "reply_text")):
            if k_src in e and k_dst not in call:
                call[k_dst] = e[k_src]
        w["call"] = call
        sec = e.get("sections")
        if isinstance(sec, list):  # the engine lists the requested sections; counts ride in parsed_counts
            counts = e.get("parsed_counts") if isinstance(e.get("parsed_counts"), dict) else {}
            sec = {k: int(counts.get(k) or 0) for k in ("facts", "edges", "episode")}
        w["sections"] = sec
        if e.get("items") is not None:
            w["items"] = e["items"]
    envelopes: list[dict] = []
    occ_rid: dict[Any, str] = {}
    for e in ev["write.envelope.trace"]:
        row = {k: v for k, v in e.items() if not k.startswith("_")}
        row.setdefault("run_id", run_id)
        row["seq"] = e["_seq"]
        envelopes.append(row)
        if e.get("record_id") and e.get("occ") is not None:
            occ_rid[e["occ"]] = e["record_id"]
    for row in envelopes:
        if not row.get("record_id") and row.get("occ") in occ_rid:
            row["record_id"] = occ_rid[row["occ"]]
    env_by_win: dict[str, list[dict]] = defaultdict(list)
    for row in envelopes:
        env_by_win[row.get("window_id")].append(row)
    for wid, w in windows.items():
        w["run_id"] = run_id
        w.setdefault("member_turns", [])
        w.setdefault("member_record_ids", [])
        if "items" not in w:
            rows = env_by_win.get(wid, [])
            w["items"] = {
                "valid": len({r.get("occ") for r in rows if r.get("status") in ("kept", "added")}),
                "repaired": len({r.get("occ") for r in rows if r.get("status") == "rewritten"}),
                "dead": [{"occ": r.get("occ"), "stage": r.get("stage"), "reason": r.get("reason")}
                         for r in rows if r.get("status") == "dropped"],
            }
    turn_windows: dict[tuple, list[str]] = defaultdict(list)
    for wid, w in windows.items():
        for t in w["member_turns"]:
            turn_windows[(w.get("item"), t)].append(wid)

    index_by_rid = {(e.get("rid") or e.get("record_id")): e for e in ev["store.index.record"]}
    index_by_turn = {(e.get("item"), _tid(e)): e for e in ev["store.index.record"] if _tid(e)}

    # ---- turns
    if ds:
        run_items = {r.get("item") for r in ingest} | {r["item_id"] for r in results}
        if not run_items:  # nothing asked yet and no ingest rows: the dataset items
            run_items = set(ds)
        base_turns = [t for it, d in ds.items() if it in run_items for t in d["turns"]]
    else:
        base_turns = [{"item": r.get("item"), "session": r.get("session"), "turn_id": r["turn"],
                       "speaker": r.get("speaker"), "ts": r.get("source_timestamp"), "text": r.get("source_text")}
                      for r in ingest]
    turns: list[dict] = []
    for t in base_turns:
        key = (t["item"], t["turn_id"])
        ing = ing_by.get(key) or {}
        wt = wt_by.get(key) or {}
        stored = wt.get("stored") or {}
        src = stored.get("source") or {}
        dep = dep_by.get(key) or {}
        derived = [r for r in (dep.get("delta_t") or {}).get("record_ids") or [] if r != ing.get("record_id")]
        reasons: list[str] = []
        for ee in wt.get("engine_events") or []:
            reasons += [str(x) for x in ee.get("reasons") or []]
        idx = index_by_turn.get(key) or index_by_rid.get(ing.get("record_id"))
        written = ing.get("written")
        action = "skipped" if written is False else ("derived" if derived else ("written" if written else None))
        row = {
            "run_id": run_id, "item": t["item"], "session": t.get("session") or ing.get("session"),
            "turn_id": t["turn_id"], "speaker": t.get("speaker"), "ts": t.get("ts"), "text": t.get("text"),
            "record_id": ing.get("record_id"), "derived_record_ids": derived, "index_text": ing.get("stored_text"),
            "text_identical": ing.get("text_identical"),
            "stamp": {"source_at": ing.get("source_timestamp") or t.get("ts"), "tz": stored.get("tz"),
                      "spk": stored.get("spk") or t.get("speaker"), "addr": stored.get("addr"),
                      "participants": stored.get("participants"), "src_class": src.get("role") or stored.get("src_class"),
                      "valid_from": ing.get("valid_from") or stored.get("valid_from"),
                      "recorded_at": stored.get("recorded_at")},
            "role": {"derive_use": stored.get("derive_use"), "trust_cap": stored.get("trust_cap"),
                     "echo": stored.get("echo"), "role": src.get("role"), "channel": src.get("channel")},
            "firewall": {"trust": ing.get("trust", stored.get("trust")),
                         "quarantined": ing.get("quarantined", stored.get("quarantined")), "reasons": reasons},
            "route": wt.get("route") or ing.get("memory_type") or stored.get("memory_type"),
            "deposit_action": (idx or {}).get("action") or action,
            "indexed": ({"vector": idx.get("vector"), "lexical": idx.get("lexical"), "graph_node": idx.get("graph_node"),
                         "source": "tap"} if idx else
                        {"vector": written, "lexical": None, "graph_node": None, "source": "inferred"}),
            "window_ids": turn_windows.get(key, []),
            "deposit": dep.get("delta_t"), "deposit_latency_ms": dep.get("latency_ms"),
        }
        if t.get("custom"):
            row["custom"] = True  # written by the step mode `say`, not part of the dataset
        turns.append(row)

    # ---- records
    records: list[dict]
    if snap["records"]:
        records = [dict(r, run_id=r.get("run_id", run_id)) for r in snap["records"]]
    else:
        records = []
        for (item, turn), ing in ing_by.items():
            if not ing.get("record_id"):
                continue
            st = (wt_by.get((item, turn)) or {}).get("stored") or {}
            src = st.get("source") or {}
            records.append({
                "run_id": run_id, "item": item, "record_id": ing["record_id"], "phase": "post_ingest",
                "memory_type": ing.get("memory_type") or st.get("memory_type"), "layer": st.get("layer"),
                "channel": src.get("channel"), "content": ing.get("stored_text"), "tags": st.get("tags") or [],
                "entity": st.get("entity"), "attribute": st.get("attribute"), "value": st.get("value"),
                "status": ing.get("status") or st.get("status"), "valid_from": ing.get("valid_from"),
                "valid_to": st.get("valid_to"), "evolve_to": st.get("evolve_to"),
                "trust": ing.get("trust", st.get("trust")), "parents": src.get("parents") or [],
                "parent_turns": [turn], "envelope": None,
            })
    # ---- links
    links: list[dict]
    if snap["links"]:
        links = [dict(r, run_id=r.get("run_id", run_id)) for r in snap["links"]]
    else:
        links = []
        for r in records:
            for p in r.get("parents") or []:
                links.append({"run_id": run_id, "item": r.get("item"), "kind": "lineage", "src": p, "dst": r["record_id"],
                              "rel": "derived_from", "props": {}, "created_by": "ingest", "phase": r.get("phase")})
            if r.get("evolve_to"):
                links.append({"run_id": run_id, "item": r.get("item"), "kind": "typed", "src": r["record_id"],
                              "dst": r["evolve_to"], "rel": "supersede", "props": {}, "created_by": "ingest",
                              "phase": r.get("phase")})
        for e in index_by_rid.values():
            for ed in _aslist(e.get("edges")):
                if isinstance(ed, dict):
                    links.append({"run_id": run_id, "item": e.get("item"), "kind": ed.get("kind", "graph"),
                                  "src": ed.get("src"), "dst": ed.get("dst"), "rel": ed.get("rel"),
                                  "props": ed.get("props", {}), "created_by": "index", "phase": "post_ingest"})
    # ---- sleep
    if snap["sleep"]:
        sleep = [dict(r, run_id=r.get("run_id", run_id)) for r in snap["sleep"]]
    else:
        sleep = [{"run_id": run_id, "item": e.get("item"), "stage": e.get("stage"), "cycle": e.get("cycle"),
                  "session_key": e.get("session_key"), "input_ids": _aslist(e.get("input_ids")),
                  "output_ids": _aslist(e.get("output_ids")), "marker": e.get("marker"), "calls": e.get("calls"),
                  "tokens": e.get("tokens"), "status": e.get("status")} for e in ev["job.stage.item"]]

    # ---- questions
    hdr_by_q: dict[Any, list[dict]] = defaultdict(list)
    for e in ev["compose.headers"]:
        items = e.get("headers")
        for h in (items if isinstance(items, list) else [e]):
            hdr_by_q[e.get("query_id")].append({"rid": h.get("rid"), "kind": h.get("kind"),
                                                 "source_ids": _aslist(h.get("source_ids"))})
    comp_by_q: dict[Any, dict] = defaultdict(dict)
    for e in ev["read.rerank.components"]:
        for c in (e.get("items") if isinstance(e.get("items"), list) else [e]):
            k = c.get("rid") or c.get("turn")
            comp_by_q[e.get("query_id")][k] = {x: c.get(x) for x in ("rel", "rec", "imp", "trust") if x in c}
    plan_io = {e.get("query_id"): e for e in ev["read.planner.io"]}

    questions: list[dict] = []
    for r in results:
        item, qid = r["item_id"], r["query_id"]
        dq = (ds.get(item) or {}).get("questions", {}).get(qid) or {}
        f = fx_by_q.get((item, qid))
        rd = reads_by.get((item, qid)) or {}
        tf = (f or {}).get("trace_full") or {}
        meta = r.get("meta") or {}
        mode = "retrieval" if meta.get("retrieval_only") else "qa"
        gold_turns = list(dq.get("gold_turns") or [])
        ctx_recs = (f or {}).get("context_records") or []
        retrieved = [c["turn"] for c in ctx_recs] if f else list(r.get("retrieved_ids") or [])
        in_ctx = set(retrieved)
        score = _num(r.get("score"))
        if mode == "qa":
            correct = bool(score is not None and score >= 1.0 and r.get("status") == "completed"
                           and (r.get("answer") or "").strip())
        else:
            correct = bool(gold_turns) and all(g in in_ctx for g in gold_turns)
        ing_item = ing_by_item.get(item, {})
        gp, loss = gold_path(gold_turns, f, ing_item, in_ctx)
        n_in = sum(g["in_context"] for g in gp)
        first_losing = loss
        if not correct and gold_turns and loss is None:
            first_losing = "reader" if n_in == len(gold_turns) else "recall"

        def rid_of(turn: str, _item: str = item) -> str | None:
            return turn_rid.get((_item, turn))

        def rows_of(stage: list[dict] | None, _item: str = item) -> list[dict]:
            return [{"rid": turn_rid.get((_item, x["turn"])), "turn": x["turn"], "rank": x.get("r"),
                     "score": x.get("score")} for x in stage or []]

        legs: dict[str, list[dict]] = {}
        if f:
            legs["vector"] = rows_of(f.get("vector"))
            legs["lexical"] = rows_of(f.get("lexical"))
            for n, h in (f.get("extra_legs") or {}).items():
                legs[n] = rows_of(h)
        comps = comp_by_q.get(qid, {})
        rerank = []
        for x in (f or {}).get("rerank_scores") or []:
            rid = rid_of(x["turn"])
            rerank.append({"rid": rid, "turn": x["turn"], "raw": x.get("score"), "rank": x.get("r"),
                           "composite": comps.get(rid) or comps.get(x["turn"])})
        qa_f = tf.get("query_analysis") or {}
        flags = qa_f.get("flags") or {}
        pio = plan_io.get(qid) or {}
        final_turns = {x["turn"] for x in (f or {}).get("final") or []}
        packed = [{"rid": rid_of(c["turn"]), "turn": c["turn"], "tokens": c.get("tokens"),
                   "reason": "final" if c["turn"] in final_turns else "window_neighbour"} for c in ctx_recs]
        window_added = sorted({n for w in tf.get("window") or [] for n in w.get("neighbours") or []} - final_turns)
        calls = rd.get("reader_calls") or []
        verdict = rd.get("verdict") or {}
        funnel = []
        stage_sets = [
            ("recall", {x["turn"] for h in legs.values() for x in h}),
            ("fusion", {x["turn"] for x in (f or {}).get("fused") or []}),
            ("pool", {x["turn"] for x in (f or {}).get("pool") or []}),
            ("final", final_turns), ("context", in_ctx),
        ]
        if f:
            for name, s in stage_sets:
                funnel.append({"stage": name, "n": len(s), "gold_present": sum(g in s for g in gold_turns),
                               "gold_total": len(gold_turns)})
        context_text = rd.get("context_text") or ("\n".join(c.get("text", "") for c in ctx_recs) if ctx_recs else None)
        questions.append({
            "run_id": run_id, "item": item, "query_id": qid, "category": CAT.get(r.get("type_label"), r.get("type_label") or dq.get("category")),
            "question": r.get("question") or dq.get("question"), "gold": r.get("gold", dq.get("gold")),
            "gold_turns": gold_turns, "mode": mode, "has_stage_log": f is not None,
            **({"custom": True} if dq.get("custom") or r.get("custom") else {}),
            "plan": {"contract": tf.get("contract") or qa_f.get("qa_shape"), "tier": tf.get("tier"),
                     "route": qa_f.get("rule_read_mode") or tf.get("route"),
                     "list_mode": bool(flags.get("is_set_question") or flags.get("is_intent_list")),
                     "decompose": qa_f.get("split_intents"), "flags": flags, "perspective": tf.get("perspective"),
                     "planner_io": ({"prompt": pio.get("prompt"), "reply": pio.get("reply"), "plan": pio.get("plan")}
                                    if pio else None)},
            "legs": legs, "fused": rows_of((f or {}).get("fused")),
            "date_prior": tf.get("date_prior"), "protected": tf.get("protected"),
            "cuts": [{"rid": c.get("id"), "turn": c.get("turn"), "reason": c.get("reason"), "stage": c.get("stage", "assembly"),
                      "keep": c.get("keep")} for c in tf.get("cuts") or []],
            "pool": rows_of((f or {}).get("pool")), "final": rows_of((f or {}).get("final")),
            "reranker": (f or {}).get("reranker"), "rerank": rerank,
            "headers": hdr_by_q.get(qid, []), "packed": packed, "window_added": window_added,
            "window": tf.get("window"), "assembled": tf.get("assembled"),
            "gate": {k: f[k] for k in ("decisions", "relevance_calibration", "relevance_bypass") if f and k in f} or None,
            "deep": tf.get("deep") or tf.get("obligations"),
            "context_text": context_text, "context_tokens": r.get("context_tokens"),
            "reader": {"prompt": (calls[0].get("prompt") if calls else None), "raw": rd.get("reader_raw"),
                       "calls": calls},
            "retry": next((c for c in calls if "retry" in str(c.get("kind", ""))), None) or tf.get("retry"),
            "answer": r.get("answer"), "status": r.get("status"),
            "judge": {"score": score, "raw": verdict.get("raw") or meta.get("judge_raw"),
                      "conventions": (verdict.get("meta") or {}) or meta.get("conventions")},
            "correct": correct, "first_losing_stage": first_losing, "gold_path": gp, "funnel": funnel,
        })

    manifest = {
        "run_id": run_id, "dirs": [d.name for d in dirs],
        "system_id": (man_row.get("system") or {}).get("system_id"),
        "config_hash": (man_row.get("system") or {}).get("config_hash"),
        "arm": (man_row.get("labels") or {}).get("arms") or (man_row.get("labels") or {}).get("experiment"),
        "commit": (man_row.get("code") or {}).get("memspine_git"),
        "models": {"reader": ((man_row.get("reader") or {}).get("model")), "judge": ((man_row.get("judge") or {}).get("model")),
                   "engine": (man_row.get("labels") or {}).get("engine_llm_models")},
        "dataset_revision": (man_row.get("dataset") or {}).get("revision_id"),
        "created_at": man_row.get("created_at"),
        "run_started_at": _run_started_at(man_row.get("created_at"), [results_path, _first(dirs, ["ingest.jsonl"])]),
        "score_mean": summary_row.get("score_mean"),
        "tap_events": {k: len(v) for k, v in ev.items()},
        "inputs": {"ingest": len(ingest), "forensics": len(fx), "reads": len(reads), "write_trace": len(wtrace),
                   "results": len(results), "trace": len(trace), "tap": len(tap),
                   "snapshots": {k: len(v) for k, v in snap.items()}},
    }
    return {"turns": turns, "windows": list(windows.values()), "envelopes": envelopes, "records": records,
            "links": links, "sleep": sleep, "questions": questions, "checks": checks, "manifest": manifest,
            "graph_nodes": [dict(r, run_id=r.get("run_id", run_id)) for r in snap["graph_nodes"]]}


def _strip_text(o: Any) -> Any:
    if isinstance(o, dict):
        return {k: (None if (k in TEXT_KEYS and isinstance(v, str | list)) else _strip_text(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [_strip_text(x) for x in o]
    return o


FILES = ["turns", "windows", "envelopes", "records", "links", "sleep", "questions", "checks"]


def write_bundle(bundle: dict[str, Any], out_dir: Path, content: bool = True) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    man = dict(bundle["manifest"])
    man["content"] = content
    man["counts"] = {n: len(bundle[n]) for n in FILES}
    extra = ["graph_nodes"] if bundle.get("graph_nodes") else []
    man["counts"].update({n: len(bundle[n]) for n in extra})
    man["files"] = [f"{n}.jsonl" for n in FILES + extra] + ["manifest.json"]
    for n in FILES + extra:
        rows = bundle[n] if content else _strip_text(bundle[n])
        write_jsonl(out_dir / f"{n}.jsonl", rows)
    (out_dir / "manifest.json").write_text(json.dumps(man, indent=1, default=str), encoding="utf-8")
    return man


# --------------------------------------------------------------------------------------- compare
def _turn_sig(t: dict) -> dict:
    return {"record_id": t.get("record_id"), "derived": len(t.get("derived_record_ids") or []),
            "deposit_action": t.get("deposit_action"), "windows": len(t.get("window_ids") or []),
            "quarantined": (t.get("firewall") or {}).get("quarantined"), "text_identical": t.get("text_identical")}


def _q_sig(q: dict) -> dict:
    surv = {f["stage"]: f["gold_present"] for f in q.get("funnel") or []}
    return {"correct": q.get("correct"), "first_losing_stage": q.get("first_losing_stage"), "survival": surv,
            "answer": q.get("answer"), "judge_score": (q.get("judge") or {}).get("score")}


def compare_bundles(a: dict, b: dict) -> dict:
    """Align two bundles by (item, turn_id) and (item, query_id)."""
    ta = {(t["item"], t["turn_id"]): t for t in a["turns"]}
    tb = {(t["item"], t["turn_id"]): t for t in b["turns"]}
    turns = []
    for k in sorted(set(ta) | set(tb), key=lambda k: (str(k[0]), [int(x) for x in re.findall(r"\d+", str(k[1]))])):
        sa = _turn_sig(ta[k]) if k in ta else None
        sb = _turn_sig(tb[k]) if k in tb else None
        diff = [f for f in ("derived", "deposit_action", "windows", "quarantined") if sa and sb and sa[f] != sb[f]]
        turns.append({"item": k[0], "turn_id": k[1], "a": sa, "b": sb, "differs": diff or (["missing"] if not (sa and sb) else [])})
    qa = {(q["item"], q["query_id"]): q for q in a["questions"]}
    qb = {(q["item"], q["query_id"]): q for q in b["questions"]}
    questions = []
    for k in sorted(set(qa) | set(qb), key=lambda k: (str(k[0]), [int(x) for x in re.findall(r"\d+", str(k[1]))])):
        sa = _q_sig(qa[k]) if k in qa else None
        sb = _q_sig(qb[k]) if k in qb else None
        if sa and sb:
            delta = ("fixed" if sb["correct"] and not sa["correct"] else "regressed" if sa["correct"] and not sb["correct"]
                     else "both_right" if sa["correct"] else "both_wrong")
        else:
            delta = "missing"
        q = qa.get(k) or qb.get(k)
        questions.append({"item": k[0], "query_id": k[1], "category": q.get("category"), "question": q.get("question"),
                          "gold_turns": q.get("gold_turns"), "a": sa, "b": sb, "delta": delta,
                          "stage_changed": bool(sa and sb and sa["first_losing_stage"] != sb["first_losing_stage"])})
    counts: dict[str, int] = defaultdict(int)
    for q in questions:
        counts[q["delta"]] += 1
    return {"a": a["manifest"]["run_id"], "b": b["manifest"]["run_id"], "turns": turns, "questions": questions,
            "summary": {"questions": dict(counts), "turns_differing": sum(1 for t in turns if t["differs"]),
                        "turns": len(turns)}}


# -------------------------------------------------------------------------------------------- API
def assemble(run_dir: str | Path, out_dir: str | Path, dataset: Any = None, *, content: bool = True,
             compare_with: str | Path | None = None) -> dict:
    """Build the inspect bundle of ``run_dir`` into ``out_dir``; returns the manifest. With ``compare_with``
    the second run's bundle goes to ``out_dir/compare_B`` and ``out_dir/compare.json`` aligns the two."""
    out = Path(out_dir)
    a = _build_one(run_dir, dataset)
    man = write_bundle(a, out, content)
    if compare_with is not None:
        b = _build_one(compare_with, dataset)
        write_bundle(b, out / "compare_B", content)
        cmp = compare_bundles(a, b)
        if not content:
            cmp = _strip_text(cmp)
        (out / "compare.json").write_text(json.dumps(cmp, ensure_ascii=False, default=str), encoding="utf-8")
        man["compare"] = {"b": b["manifest"]["run_id"], **cmp["summary"]}
        (out / "manifest.json").write_text(json.dumps(man, indent=1, default=str), encoding="utf-8")
    return man


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m memspine_evals.inspect.assemble", description=__doc__.split("\n")[0])
    ap.add_argument("run", help="run id or a run directory")
    ap.add_argument("--compare", help="a second run to align against (by turn_id / query_id)")
    ap.add_argument("--data", help="LoCoMo-shaped dataset json, or 'demo'")
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-content", action="store_true", help="drop all text fields from the bundle")
    args = ap.parse_args(argv)
    man = assemble(args.run, args.out, args.data, content=not args.no_content, compare_with=args.compare)
    print(f"{man['run_id']}: {man['counts']} -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
