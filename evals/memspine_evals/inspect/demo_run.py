"""A synthetic run directory built from the invented demo conversation (no engine, no model, no benchmark text).

``make_demo_run(root, arm)`` writes the same files a real run leaves behind (ingest, forensics with
``trace_full``, results, trace, write_trace, reads, wiretap events, store snapshots) with scripted outcomes, so the
assembler, the HTML builder and the published demo page can be exercised and shown without a GPU.

arm ``ref``  : verbatim turns, graph edges added in sleep; records/links/sleep are *not* snapshotted (the assembler
               falls back to ingest rows and tap events).
arm ``v2``   : window extraction with envelopes through stages 100..500, derived facts, a supersede, store snapshots.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .assemble import DEMO_PATH, load_dataset

STAGES = ["compile", "100", "200", "250", "300", "400", "500", "deposit"]


def _plan(ds: dict) -> dict:
    """Windows of four turns per session, two facts per window (turns at offsets 0 and 2), and scripted
    envelope events: one fact dropped as a duplicate, one rewritten (pronoun), one with a resolved time, one superseded."""
    turns = ds["turns"]
    sessions: dict[str, list[str]] = {}
    for t in turns:
        sessions.setdefault(t["session"], []).append(t["turn_id"])
    windows, facts = [], {}
    occ = 0
    for sname, tids in sessions.items():
        sn = re.sub(r"\D", "", sname) or sname
        for k, i in enumerate(range(0, len(tids), 4), 1):
            chunk = tids[i:i + 4]
            wid = f"win-{sn}-{k}"
            windows.append((wid, chunk[0], chunk[-1]))
            for off in (0, 2):
                if off < len(chunk):
                    occ += 1
                    tt = next(t for t in turns if t["turn_id"] == chunk[off])
                    facts[occ] = (wid, f"{tt['speaker']}: {tt['text']}", [chunk[off]])
    by_src = {f[2][0]: o for o, f in facts.items()}
    dropped = {len(facts) - 1: "300"}
    rewrite = by_src.get("D1:5", 3)
    timeocc = by_src.get("D1:9", 5)
    old, new = by_src.get("D1:3", 2), by_src.get("D3:5", len(facts))
    overrides = {(rewrite, "250"): ("rewritten", "pronoun_resolved", ["subject"]),
                 (timeocc, "200"): ("rewritten", "relative_time:resolved", ["time"]),
                 (old, "500"): ("kept", f"superseded_by:{new}", []),
                 (len(facts) - 1, "300"): ("dropped", f"duplicate_of:{new}", [])}
    integrate = {new: {"op": "supersede", "rung": "entity_attribute", "target": old}}
    gold = {g for q in ds["questions"].values() for g in q["gold_turns"]} | {f[2][0] for f in facts.values()}
    last_session = list(sessions.values())[1] if len(sessions) > 1 else list(sessions.values())[0]
    skipped = min((t for t in last_session if t not in gold),
                  key=lambda t: len(next(x for x in turns if x["turn_id"] == t)["text"]), default=None)
    return {"windows": windows, "facts": facts, "dropped": dropped, "overrides": overrides, "integrate": integrate,
            "rewrite": rewrite, "timeocc": timeocc, "old": old, "new": new, "skipped": skipped}


SCENARIOS = {
    "ref": {0: "ok", 1: "recall", 2: "reader", 3: "fusion", 4: "ok", 5: "rerank", 6: "assembly", 7: "ok"},
    "v2": {0: "ok", 1: "ok", 2: "ok", 3: "rescue", 4: "ok", 5: "ok", 6: "ok", 7: "reader"},
}


def _num(t: str) -> int:
    return int(re.sub(r"\D", "", t.split(":")[1]))


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def make_demo_run(root: str | Path, arm: str = "ref", run_id: str | None = None) -> Path:
    """Write the scripted run under ``root``; returns the ``<run_id>--memspine`` directory."""
    root = Path(root)
    run_id = run_id or f"demo-{arm}"
    ds = load_dataset("demo")["demo-1"]
    item = "demo-1"
    turns = ds["turns"]
    tids = [t["turn_id"] for t in turns]
    rid = {t: f"rec-{i + 1}" for i, t in enumerate(tids)}
    by_turn = {t["turn_id"]: t for t in turns}
    v2 = arm == "v2"
    pl = _plan(ds)
    WINDOWS, FACTS, OVERRIDES, DROPPED_AT, INTEGRATE = pl["windows"], pl["facts"], pl["overrides"], pl["dropped"], pl["integrate"]
    SESSION_DAY = {t["session"]: t["ts"] for t in turns}
    skipped = {pl["skipped"]} if v2 and pl["skipped"] else set()

    ingest, wtrace, deposit = [], [], []
    for t in turns:
        w = t["turn_id"] not in skipped
        ingest.append({"run_id": run_id, "item": item, "turn": t["turn_id"], "session": t["session"], "speaker": t["speaker"],
                       "source_timestamp": t["ts"], "source_text": t["text"], "written": w,
                       "record_id": rid[t["turn_id"]] if w else None, "stored_text": f"{t['speaker']}: {t['text']}" if w else None,
                       "text_identical": True if w else None, "valid_from": SESSION_DAY[t["session"]], "memory_type": "episodic",
                       "group_id": item, "batch_size": 1, "quarantined": False, "trust": 0.8, "status": "active"})
        wtrace.append({"run_id": run_id, "item": item, "turn": t["turn_id"], "record_id": rid[t["turn_id"]], "written": w,
                       "stored": {"memory_type": "episodic", "tags": ["verbatim"], "group_id": item, "status": "active",
                                  "trust": 0.8, "quarantined": False, "valid_from": SESSION_DAY[t["session"]],
                                  "source": {"role": "user", "channel": "chat", "principal": None, "parents": []}},
                       "engine_events": [{"kind": "firewall", "record_id": rid[t["turn_id"]], "trust": 0.8, "quarantine": False,
                                          "reasons": []}]})
    win_of = {}
    for wid, a, b in WINDOWS:
        for t in tids:
            if _num(a) <= _num(t) <= _num(b) and t.split(":")[0] == a.split(":")[0]:
                win_of[t] = wid
    win_last = {wid: b for wid, _a, b in WINDOWS}
    win_last_of_session = {by_turn[_b]["session"]: _b for _w, _a, _b in WINDOWS}
    fct_of_win: dict[str, list[int]] = {}
    for occ, (wid, _t, _s) in FACTS.items():
        if occ not in DROPPED_AT:
            fct_of_win.setdefault(wid, []).append(occ)
    for i, t in enumerate(turns):
        tid = t["turn_id"]
        derived = []
        if v2 and tid == win_last.get(win_of.get(tid)):
            derived = [f"fct-{o}" for o in fct_of_win.get(win_of[tid], [])]
        deposit.append({"kind": "deposit", "item_id": item, "turn_id": tid, "t": i,
                        "delta_t": {"n_records": (1 if tid not in skipped else 0) + len(derived),
                                    "record_ids": ([rid[tid]] if tid not in skipped else []) + derived, "model_calls": 1 if derived else 0,
                                    "buffered": 0}, "latency_ms": 12.5})

    # ---- tap events
    tap: list[dict] = []
    if v2:
        for wid, a, b in WINDOWS:
            members = [t for t in tids if win_of.get(t) == wid]
            tap.append({"event": "write.window.members", "run_id": run_id, "item": item, "window_id": wid,
                        "session": by_turn[a]["session"], "member_turns": members,
                        "member_record_ids": [rid[m] for m in members if m not in skipped],
                        "cut_reason": "session_end" if b == win_last_of_session[by_turn[b]["session"]] else "size", "prepass": {"verdict": "extract", "hints": []}})
            occs = [o for o, f in FACTS.items() if f[0] == wid]
            reply = json.dumps([{"occ": o, "fact": FACTS[o][1]} for o in occs])
            tap.append({"event": "write.extract.io", "run_id": run_id, "item": item, "window_id": wid,
                        "prompt": {"id": "extract.v2", "hash": f"h{abs(hash(wid)) % 10**6}", "context_lines": 2, "known": 3},
                        "prompt_text": "\n".join(f"{by_turn[m]['speaker']}: {by_turn[m]['text']}" for m in members),
                        "reply_text": reply, "model": "stub-llm", "cached": False, "tokens": {"prompt": 180, "completion": 60}, "ms": 420,
                        "sections": {"facts": len(occs), "edges": 0, "episode": 1}})
        for occ, (wid, _text, _src) in FACTS.items():
            for stage in STAGES:
                if occ in DROPPED_AT and STAGES.index(stage) > STAGES.index(DROPPED_AT[occ]):
                    break
                st, reason, ch = OVERRIDES.get((occ, stage), ("kept", None, []))
                if stage == "deposit":
                    st = "kept"
                e = {"event": "write.envelope.trace", "run_id": run_id, "item": item, "occ": occ, "write_id": f"w-{occ}",
                     "window_id": wid, "stage": stage, "status": st, "reason": reason, "before_hash": f"b{occ}{stage}",
                     "after_hash": f"a{occ}{stage}", "fields_changed": ch}
                if stage == "200":
                    e["grounding"] = {"label": "stated" if occ != pl["rewrite"] else "inferred", "rules": ["verbatim_span"]}
                if stage == "250":
                    e["resolve"] = {"entity": "Noor" if occ == pl["rewrite"] else None, "verdict": "resolved" if occ == pl["rewrite"] else "none"}
                if stage == "500" and occ in INTEGRATE:
                    e["integrate"] = INTEGRATE[occ]
                if stage == "100" and occ == pl["timeocc"]:
                    e["time"] = {"event": SESSION_DAY[by_turn[FACTS[occ][2][0]]["session"]], "precision": "day", "basis": "session_date"}
                if stage == "deposit":
                    e["record_id"] = f"fct-{occ}"
                tap.append(e)
    for t in tids:
        if t in skipped:
            continue
        tap.append({"event": "store.index.record", "run_id": run_id, "item": item, "rid": rid[t], "turn_id": t, "vector": True,
                    "lexical": True, "graph_node": not v2, "index_text_len": len(by_turn[t]["text"]),
                    "action": "written", "edges": [{"kind": "mentions", "src": rid[t], "dst": f"ent:{n}", "rel": "mentions"}
                                                   for n in ("Noor", "Nono", "Tomasz", "Ilse", "Bram", "Pepper") if n in by_turn[t]["text"]]})
    if not v2:
        for s in ("session_1", "session_2", "session_3"):
            ids = [rid[t] for t in tids if by_turn[t]["session"] == s]
            tap.append({"event": "job.stage.item", "run_id": run_id, "item": item, "stage": "extract_graph", "cycle": 1,
                        "session_key": s, "input_ids": ids, "output_ids": [], "marker": f"graph:{s}", "calls": 0, "tokens": 0,
                        "status": "ok"})

    # ---- questions
    res_rows: list[dict] = [{"kind": "manifest", "run_id": run_id, "system": {"system_id": f"demo-{arm}", "config_hash": "c0ffee"},
                              "reader": {"model": "stub-reader"}, "judge": {"model": "stub-judge"},
                              "labels": {"arms": [arm]}, "code": {"memspine_git": "demo"}, "dataset": {"revision_id": "demo-1"},
                              "created_at": "2026-10-14T00:00:00Z"}]
    fx_rows, reads, cycles = [], [], []
    n = len(tids)
    for qi, (qid, q) in enumerate(ds["questions"].items()):
        scen = SCENARIOS[arm][qi]
        gold = q["gold_turns"]
        tg = gold[-1] if gold else None
        dist = [tids[(qi * 3 + 5 * j + 2) % n] for j in range(6)]
        dist = [d for d in dict.fromkeys(dist) if d not in gold][:5]
        order = []
        for k in range(max(len(dist), len(gold))):
            if k < len(gold):
                order.append(gold[k])
            if k < len(dist):
                order.append(dist[k])
        legs_c = [x for x in order if not (scen == "recall" and x == tg)]
        fused = [x for x in legs_c if not (scen in ("fusion", "rescue") and x == tg)]
        pool = fused[:8]
        final = [x for x in pool if not (scen == "rerank" and x == tg)][:5]
        if scen == "rescue" and tg:
            prev = f"{tg.split(':')[0]}:{_num(tg) - 1}"
            if prev in tids and prev not in final:
                final.append(prev)
        ctx = [x for x in final if not (scen == "assembly" and x == tg)]
        if scen == "rescue" and tg:
            ctx.append(tg)
        sc = lambda lst: [{"r": i + 1, "turn": x, "score": round(1.0 - 0.07 * i, 3)} for i, x in enumerate(lst)]  # noqa: E731
        wrong = scen in ("recall", "reader", "fusion", "rerank", "assembly") and q["category"] != "cat5"
        if q["category"] == "cat5":
            ans = "A dog." if scen == "reader" else "I cannot find that in the conversation."
            score = 0.0 if scen == "reader" else 1.0
        elif wrong:
            ans = {"recall": "I do not know.", "reader": "Not stated.", "fusion": "Unclear.", "rerank": "Unknown.", "assembly": "Unsure."}[scen]
            score = 0.0
        else:
            ans = str(q["gold"])
            score = 1.0
        ctx_text = "\n".join(f"[{by_turn[x]['ts']}] {by_turn[x]['speaker']}: {by_turn[x]['text']}" for x in ctx)
        res_rows.append({"kind": "result", "run_id": run_id, "item_id": item, "query_id": qid, "question": q["question"],
                         "gold": q["gold"] if q["category"] != "cat5" else "abstain", "answer": ans, "score": score, "status": "completed",
                         "type_label": q["category"], "context_tokens": len(ctx_text) // 4, "retrieved_ids": ctx,
                         "meta": {"judge_raw": "CORRECT" if score else "WRONG", "reranked": True}})
        flags = {"is_set_question": qi == 1, "is_temporal": qi == 2, "is_intent_list": False, "is_count": False}
        tf = {"query_analysis": {"flags": flags, "rule_read_mode": "list" if qi == 1 else "fact", "question_shape": "wh",
                                  "split_intents": ["who", "who"] if qi == 1 else [], "qa_shape": "list" if qi == 1 else "fact"},
              "perspective": {"asker": None, "about": "Bram" if qi in (0, 6) else "Ilse", "targets": [["Bram"]], "mode": "single",
                              "scope": "item"},
              "records": [{"turn": x, "record_id": rid[x], "tags": ["verbatim"], "valid_from": SESSION_DAY[by_turn[x]["session"]],
                           "trust": 0.8, "memory_type": "episodic"} for x in ctx],
              "cuts": ([{"id": rid[tg], "reason": "budget", "keep": 4, "turn": tg}] if scen == "assembly" and tg else []),
              "window": [{"anchor": x, "neighbours": [f"{x.split(':')[0]}:{_num(x) + 1}"], "skipped": {}} for x in final[:2]]
              + ([{"anchor": prev, "neighbours": [tg], "skipped": {}}] if scen == "rescue" and tg else []),
              "assembled": {"abstained": q["category"] == "cat5" and scen != "reader", "tokens_used": len(ctx_text) // 4,
                            "boundary_index": len(ctx), "n_records": len(ctx)}}
        if qi == 2:
            tf["date_prior"] = {"on": True, "mode": "session_day", "protected": [tg]}
        fx_rows.append({"run_id": run_id, "query_id": qid, "item": item, "namespace": item, "query": q["question"],
                        "vector": sc(legs_c[::2] or legs_c), "lexical": sc(legs_c[1::2] or legs_c), "extra_legs": {}, "fused": sc(fused),
                        "pool": sc(pool), "reranker": "stub", "rerank_scores": sc(pool), "final": sc(final),
                        "context_records": [{"turn": x, "text": f"{by_turn[x]['speaker']}: {by_turn[x]['text']}"} for x in ctx],
                        "context_tokens": len(ctx_text) // 4, "trace_full": tf})
        reads.append({"run_id": run_id, "item_id": item, "query_id": qid, "question": q["question"], "gold": q["gold"],
                      "context_text": ctx_text, "context_tokens": len(ctx_text) // 4, "answer": ans, "reader_raw": ans,
                      "reader_calls": [{"kind": "answer", "step": 1, "model": "stub-reader",
                                        "prompt": f"Context:\n{ctx_text}\n\nQuestion: {q['question']}", "reply": ans, "finish": "stop"}],
                      "judge_calls": [], "verdict": {"score": score, "scale": "binary", "raw": "CORRECT" if score else "WRONG", "meta": {}}})
        cycles.append({"kind": "cycle", "item_id": item, "query_id": qid, "t": n + qi, "E_t": [{"turn_id": x, "score": 1.0} for x in ctx],
                       "model_calls": 1})
        if qi in (0, 5):
            tap.append({"event": "read.planner.io", "run_id": run_id, "item": item, "query_id": qid,
                        "prompt": f"Plan: {q['question']}", "reply": '{"route":"fact"}', "plan": {"route": "fact"}})
        for k, x in enumerate(pool):
            tap.append({"event": "read.rerank.components", "run_id": run_id, "item": item, "query_id": qid, "rid": rid[x],
                        "rel": round(0.9 - 0.05 * k, 3), "rec": round(0.5 + 0.01 * k, 3), "imp": 0.4, "trust": 0.8})
        tap.append({"event": "compose.headers", "run_id": run_id, "item": item, "query_id": qid,
                    "headers": [{"rid": rid[ctx[0]], "kind": "session_date", "source_ids": [rid[ctx[0]]]}] if ctx else []})
    res_rows.append({"kind": "summary", "run_id": run_id, "score_mean": sum(r["score"] for r in res_rows if r.get("kind") == "result") / 8})

    mem = root / f"{run_id}--memspine"
    fxd = root / f"{run_id}--forensics"
    trd = root / f"{run_id}--trace"
    _write(mem / "results.jsonl", res_rows)
    _write(mem / "trace.jsonl", deposit + cycles)
    _write(fxd / "ingest.jsonl", ingest)
    _write(fxd / "forensics.jsonl", fx_rows)
    _write(trd / "write_trace.jsonl", wtrace)
    _write(trd / "reads.jsonl", reads)
    _write(fxd / "tap" / "events.jsonl", tap)

    # ---- TI-2 style snapshots (v2 only)
    if v2:
        recs, links = [], []
        for t in tids:
            if t in skipped:
                continue
            recs.append({"item": item, "record_id": rid[t], "phase": "post_ingest", "memory_type": "episodic", "layer": "L0",
                         "channel": "chat", "content": f"{by_turn[t]['speaker']}: {by_turn[t]['text']}", "tags": ["verbatim"],
                         "status": "active", "valid_from": SESSION_DAY[by_turn[t]["session"]], "valid_to": None, "evolve_to": None,
                         "trust": 0.8, "parents": [], "parent_turns": [t], "envelope": None})
        for occ, (wid, text, src) in FACTS.items():
            if occ in DROPPED_AT:
                continue
            parents = [rid[s] for s in src]
            recs.append({"item": item, "record_id": f"fct-{occ}", "phase": "post_ingest", "memory_type": "semantic", "layer": "L1",
                         "channel": "extract", "content": text, "tags": ["fact"], "entity": text.split()[0], "attribute": None,
                         "value": None, "status": "active", "valid_from": SESSION_DAY[by_turn[src[0]]["session"]], "valid_to": None,
                         "evolve_to": None, "trust": 0.7, "parents": parents, "parent_turns": src,
                         "envelope": {"occ": occ, "window_id": wid}})
            for p in parents:
                links.append({"item": item, "kind": "lineage", "src": p, "dst": f"fct-{occ}", "rel": "derived_from", "props": {},
                              "created_by": "deposit", "phase": "post_ingest"})
        old, new = pl["old"], pl["new"]
        o_wid, o_text, o_src = FACTS[old]
        recs.append({"item": item, "record_id": f"fct-{old}", "phase": "post_sleep", "memory_type": "semantic", "layer": "L1",
                     "channel": "extract", "content": o_text, "tags": ["fact"], "status": "superseded",
                     "valid_from": SESSION_DAY[by_turn[o_src[0]]["session"]], "valid_to": SESSION_DAY[by_turn[FACTS[new][2][0]]["session"]],
                     "evolve_to": f"fct-{new}", "trust": 0.7, "parents": [rid[o_src[0]]], "parent_turns": o_src,
                     "envelope": {"occ": old, "window_id": o_wid}})
        links.append({"item": item, "kind": "typed", "src": f"fct-{old}", "dst": f"fct-{new}", "rel": "supersede", "props": {},
                      "created_by": "integrate", "phase": "post_ingest"})
        for a, b in zip(tids, tids[1:], strict=False):
            if a not in skipped and b not in skipped and a.split(":")[0] == b.split(":")[0]:
                links.append({"item": item, "kind": "reply", "src": rid[b], "dst": rid[a], "rel": "replies_to", "props": {},
                              "created_by": "ingest", "phase": "post_ingest"})
        for occ, (_w, text, _s) in FACTS.items():
            for name in ("Noor", "Nono", "Tomasz", "Ilse", "Bram", "Pepper", "Utrecht"):
                if name in text and occ not in DROPPED_AT:
                    links.append({"item": item, "kind": "mentions", "src": f"fct-{occ}", "dst": f"ent:{name}", "rel": "mentions",
                                  "props": {}, "created_by": "sleep.extract_graph", "phase": "post_sleep"})
        sleep = [{"item": item, "stage": st, "cycle": 1, "session_key": s, "input_ids": [f"fct-{o}" for o in (1, 2)],
                  "output_ids": [f"fct-{o}" for o in (1,)], "marker": f"{st}:{s}", "calls": 1, "tokens": 90, "status": "ok"}
                 for s in ("session_1", "session_2", "session_3") for st in ("consolidate", "reflect")]
        sleep_ids = {r["record_id"] for r in recs if r["phase"] == "post_sleep"}
        full = [r for r in recs if r["phase"] == "post_ingest"]
        recs = full + [r for r in recs if r["phase"] == "post_sleep"] + [dict(r, phase="post_sleep") for r in full if r["record_id"] not in sleep_ids]
        names = sorted({lk["dst"] for lk in links if lk["dst"].startswith("ent:")})
        _write(fxd / "inspect" / "graph_nodes.jsonl", [{"item": item, "node_id": n, "kind": "entity", "name": n[4:], "phase": "post_sleep"} for n in names])
        _write(fxd / "inspect" / "records.jsonl", recs)
        _write(fxd / "inspect" / "links.jsonl", links)
        _write(fxd / "inspect" / "sleep.jsonl", sleep)
        _write(fxd / "inspect" / "checks.jsonl", [
            {"check_id": "turn_written_once", "scope": "turn", "id": "D1:1", "item": item, "level": "error", "status": "pass", "ok": True, "detail": ""},
            {"check_id": "turn_indexed", "scope": "turn", "id": pl["skipped"], "item": item, "level": "error", "status": "fail", "ok": False,
             "detail": "turn skipped by the prepass, not indexed"},
            {"check_id": "envelope_terminal", "scope": "envelope", "id": "13", "item": item, "level": "warn", "status": "pass", "ok": True,
             "detail": "dropped: duplicate_of:10"},
            {"check_id": "no_parent_cycles", "scope": "record", "id": "fct-" + str(pl["new"]), "item": item, "level": "error", "status": "pass", "ok": True, "detail": ""}])
    return mem


if __name__ == "__main__":  # pragma: no cover
    print(DEMO_PATH)
