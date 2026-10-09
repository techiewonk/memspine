"""Per-question forensic report: injection -> retrieval stages -> context -> answer -> verdict.

    python evals/forensics_report.py --run runs/<run>--memspine [--forensics runs/<fx-dir>] \
        --data data/locomo10.json --out <dir> [--only-wrong-md]

Inputs:
  results.jsonl   (the harness)  verdict, answer, gold, retrieved turn ids per question
  summary.json    (the harness)  run manifest: engine config, models
  forensics.jsonl (optional, MEMSPINE_FORENSICS_DIR) ranked ids at every search stage per question
  ingest.jsonl    (optional, MEMSPINE_FORENSICS_DIR) source turn vs stored record per turn written

Outputs in ``--out`` (JSON first; the schemas are in evals/schemas/ and every row is validated):
  per_question.jsonl  forensic_question/v1, one line per question
  run_summary.json    forensic_run/v1: config, models, accuracy, funnel, gaps, cascades, injection audit
  per_question.md, gap_summary.md, injection_audit.md   readable versions
  (evals/build_html_report.py turns the JSON into an HTML view per run and an index over runs)

Gap attribution. For each gold evidence turn, the earliest stage that lost it: ingest (not written),
recall (in no leg), fusion (in a leg, not in the fused top-k), gate, rerank (in the reranker pool, not in
the final list), assembly (in the final list, not in the context). A turn that the search lost but the
neighbour expansion still put into the context is a rescue, not a loss. A question's primary gap is its
weakest link; ``reader`` when every gold turn reached the context and the answer was still wrong.
Runs without a stage log get results-level attribution only (retrieval_miss / partial / read_fail).
"""

from __future__ import annotations

import argparse
import ast
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from pathlib import Path

HERE = Path(__file__).parent
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != HERE.resolve()]
sys.path.append(str(HERE))

from failure_buckets import _session_date, bucket_failure  # noqa: E402
from memspine_evals.datasets import LoCoMoDataset  # noqa: E402

CAT = {"cat1": "multi-hop", "cat2": "temporal", "cat3": "open-domain", "cat4": "single-hop", "cat5": "adversarial"}
CATS = ["single-hop", "multi-hop", "temporal", "open-domain"]
STAGE_ORDER = ["ingest", "recall", "fusion", "gate", "rerank", "assembly", "reader"]
HYPOTHESES = {
    "ingest": "Write side: turn dropped/merged, text altered, or filed under a wrong event date. Fix at deposit "
    "(session stamp -> valid_from, speaker prefix, image captions, relative-date resolution, firewall quarantine).",
    "recall": "No leg (vector top-30, BM25 top-30, extra legs) surfaced the gold turn. Options: query rewrite / "
    "planner probes, entity-expand and cohesion legs, session-digest leg, larger fetch window, contextual "
    "enrichment of turns at write time, fact extraction, a better or second embedder, word2vec+BM25 (future).",
    "fusion": "Gold was in a leg but fell outside the fused top-k (10). Options: candidate_pool > 1 so more "
    "candidates survive to rerank/assembly, leg weights, RRF k, min-max fusion.",
    "gate": "Gold survived fusion but a visibility/trust/tag gate removed it. Check gates and header-hide rules.",
    "rerank": "Gold was in the reranker pool but demoted out of the final list. Options: rerank_blend with the "
    "retrieval prior, confidence gate, date prefix, session-neighbour context, larger keep; check the 0.3 "
    "relative floor applied to min-max-normalised rerank scores.",
    "assembly": "Gold ranked but was cut by budget, abstain threshold or relative floor. Options: budget, "
    "relative_floor, theta_abstain, dedupe / compression.",
    "reader": "All gold evidence was in context and the answer was wrong: prompt (refusal instruction, unexplained "
    "[= date] tags), date arithmetic, granularity, list completeness, inference, or judge disagreement / gold error.",
}


def gold_turns(raw) -> list[str]:
    out: list[str] = []
    for e in raw:
        out += [p.strip() for p in str(e).replace(";", ",").split(",") if p.strip()]
    return out


def rank_of(stage: list[dict], turn: str) -> int | None:
    return next((r["r"] for r in stage if r["turn"] == turn), None)


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def as_list(v) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str) and v.startswith("["):
        try:
            return [str(x) for x in ast.literal_eval(v)]
        except (ValueError, SyntaxError):
            return []
    return []


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def session_day(stamp: str | None) -> date | None:
    return _session_date(stamp) if stamp else None


def build(args) -> None:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    run_dir = Path(args.run)
    run_id = run_dir.name.removesuffix("--memspine")

    ds = LoCoMoDataset(args.data, revision_id="auto", categories=(1, 2, 3, 4))
    turn_info: dict[tuple[str, str], dict] = {}
    gold_by_q: dict[tuple[str, str], list[str]] = {}
    for item in ds.items():
        for t in item.history:
            turn_info[(item.item_id, t.turn_id)] = {"text": f"{t.speaker}: {t.text}", "ts": t.timestamp}
        for q in item.queries:
            gold_by_q[(item.item_id, q.query_id)] = gold_turns(list(q.gold_turn_ids))

    results = [r for r in load_jsonl(run_dir / "results.jsonl") if r.get("kind") == "result"]
    manifest = {}
    if (run_dir / "summary.json").exists():
        manifest = json.loads((run_dir / "summary.json").read_text(encoding="utf-8")).get("manifest", {})
    fxdir = Path(args.forensics) if args.forensics else None
    fx = load_jsonl(fxdir / "forensics.jsonl") if fxdir else []
    ingest = load_jsonl(fxdir / "ingest.jsonl") if fxdir else []
    ing = {(r.get("item"), r["turn"]): r for r in ingest}
    fx_index: dict[tuple, list[dict]] = defaultdict(list)
    for row in fx:
        fx_index[(row.get("item"), row["query"])].append(row)
    seen: Counter = Counter()
    fx_by_q: dict[tuple[str, str], dict] = {}
    for r in results:
        key = (r["item_id"], r["question"])
        n = seen[key]
        seen[key] += 1
        if n < len(fx_index.get(key, [])):
            fx_by_q[(r["item_id"], r["query_id"])] = fx_index[key][n]

    rows: list[dict] = []
    for r in results:
        meta = r.get("meta") or {}
        mode = "retrieval" if meta.get("retrieval_only") else "qa"
        key = (r["item_id"], r["query_id"])
        gold_ids = gold_by_q.get(key, [])
        f = fx_by_q.get(key)
        retrieved = [c["turn"] for c in f["context_records"]] if f else as_list(r.get("retrieved_ids"))
        in_ctx = set(retrieved)
        answer = r.get("answer")
        score = num(r.get("score"))
        if mode == "qa":
            correct = score is not None and score >= 1.0 and r.get("status") == "completed" and bool((answer or "").strip())
        else:
            correct = bool(gold_ids) and all(g in in_ctx for g in gold_ids)
        entry = {
            "schema_version": "forensic_question/v1", "run_id": run_id, "mode": mode,
            "item": r["item_id"], "qid": r["query_id"], "category": CAT.get(r.get("type_label"), "other"),
            "question": r["question"], "gold_answer": r.get("gold"), "answer": answer if mode == "qa" else None,
            "status": r.get("status"), "judge_score": score if mode == "qa" else None,
            "judge_raw": meta.get("judge_raw"), "correct": correct, "has_stage_log": f is not None,
            "context_tokens": num(r.get("context_tokens")), "latency_retrieve_ms": num(r.get("latency_retrieve_ms")),
            "latency_answer_ms": num(r.get("latency_answer_ms")), "retrieved_turns": retrieved,
            "gold_turns": [], "flags": [], "primary_gap": None, "reader_bucket": None,
            "stages": None, "top_non_gold": None, "context_text": None,
        }
        losses: list[str] = []
        for g in gold_ids:
            info = turn_info.get((r["item_id"], g), {})
            gt = {"turn": g, "text": info.get("text"), "source_timestamp": info.get("ts"),
                  "in_context": g in in_ctx, "ingest": None, "ranks": None, "lost_at": None, "rescued_from": None}
            loss = None
            i = ing.get((r["item_id"], g))
            if i is not None:
                gt["ingest"] = {"written": bool(i["written"]), "text_identical": i.get("text_identical"),
                                "valid_from": i.get("valid_from"), "record_id": i.get("record_id")}
                if not i["written"]:
                    loss = "ingest"
                    entry["flags"].append(f"ingest:not_written:{g}")
                elif i.get("text_identical") is False:
                    entry["flags"].append(f"ingest:text_altered:{g}")
                sd = session_day(info.get("ts"))
                if i.get("valid_from") and sd and i["valid_from"][:10] != sd.isoformat():
                    entry["flags"].append(f"ingest:date_mismatch:{g}")
            if f is not None:
                rk = {"vector": rank_of(f["vector"], g), "lexical": rank_of(f["lexical"], g),
                      "extra": {n: rank_of(h, g) for n, h in f["extra_legs"].items()},
                      "fused": rank_of(f["fused"], g), "pool": rank_of(f["pool"], g),
                      "rerank": rank_of(f["rerank_scores"], g) if f["rerank_scores"] else None,
                      "rerank_order": None, "rerank_score": None,
                      "final": rank_of(f["final"], g), "in_context": g in in_ctx}
                if f["rerank_scores"]:
                    srt = sorted(f["rerank_scores"], key=lambda x: -x["score"])
                    rk["rerank_order"] = next((k + 1 for k, x in enumerate(srt) if x["turn"] == g), None)
                    rk["rerank_score"] = next((x["score"] for x in f["rerank_scores"] if x["turn"] == g), None)
                gt["ranks"] = rk
                if loss is None:
                    any_leg = rk["vector"] or rk["lexical"] or any(v for v in rk["extra"].values())
                    if not any_leg:
                        loss = "recall"
                    elif rk["fused"] is None:
                        loss = "fusion"
                    elif rk["pool"] is None:
                        loss = "gate"
                    elif f["rerank_scores"] and rk["final"] is None:
                        loss = "rerank"
                    elif rk["final"] is None:
                        loss = "fusion"
                    elif not rk["in_context"]:
                        loss = "assembly"
                if loss and loss != "ingest" and rk["in_context"]:
                    entry["flags"].append(f"rescued_by_expansion:{g}:was_lost_at_{loss}")
                    gt["rescued_from"] = loss
                    loss = None
            elif g not in in_ctx and loss is None:
                loss = "recall"  # results-level only: the stage that lost it is unknown, counted as retrieval
            gt["lost_at"] = loss
            if loss:
                losses.append(loss)
            entry["gold_turns"].append(gt)
        n_in = sum(g["in_context"] for g in entry["gold_turns"])
        if not gold_ids:
            entry["outcome_class"] = "correct" if correct else "no_gold"
        elif correct:
            entry["outcome_class"] = "correct"
        elif n_in == 0:
            entry["outcome_class"] = "retrieval_miss"
        elif n_in < len(gold_ids):
            entry["outcome_class"] = "partial"
        else:
            entry["outcome_class"] = "read_fail"
        if losses:
            entry["flags"] += [f"lost:{s}" for s in losses]
            if not correct:
                entry["primary_gap"] = min(losses, key=STAGE_ORDER.index)
        elif not correct and gold_ids:
            entry["primary_gap"] = "reader"
            if mode == "qa":
                bucket, detail = bucket_failure(r["question"], str(r.get("gold")), answer or "")
                entry["reader_bucket"] = {"bucket": bucket, "detail": detail}
        if mode == "qa" and not correct and (answer or "").strip() and str(r.get("gold", "")).strip().lower() in (answer or "").lower():
            entry["flags"].append("judge:answer_contains_gold")
        if mode == "qa" and r.get("status") == "completed" and not (answer or "").strip() and (score or 0) >= 1:
            entry["flags"].append("judge:credited_empty_answer")
        if f is not None:
            entry["stages"] = {k: f[k] for k in ("vector", "lexical", "extra_legs", "fused", "pool", "reranker",
                                                  "rerank_scores", "final")}
            entry["top_non_gold"] = [
                {"turn": x["turn"], "rank": x["r"], "score": x["score"],
                 "text": (turn_info.get((r["item_id"], x["turn"])) or {}).get("text")}
                for x in f["final"] if x["turn"] not in gold_ids][:5]
            entry["context_text"] = [c["text"] for c in f["context_records"]]
        rows.append(entry)

    summary = summarise(run_id, rows, manifest, ingest, turn_info)
    validate(rows, summary)
    (out / "per_question.jsonl").write_text("\n".join(json.dumps(e) for e in rows), encoding="utf-8")
    (out / "run_summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    write_markdown(out, rows, args.only_wrong_md)
    write_gap_md(out, summary)
    print(f"{run_id}: {len(rows)} questions, accuracy {summary['accuracy']:.1%}, stage log={summary['has_stage_log']} -> {out}")


def summarise(run_id: str, rows: list[dict], manifest: dict, ingest: list[dict], turn_info: dict) -> dict:
    sysconf = ((manifest.get("system") or {}).get("config") or {})
    cfg = sysconf.get("config") or {}
    reader = (manifest.get("reader") or {})
    judge = (manifest.get("judge") or {})
    s: dict = {"schema_version": "forensic_run/v1", "run_id": run_id,
               "mode": rows[0]["mode"] if rows else "qa", "has_stage_log": any(r["has_stage_log"] for r in rows),
               "generated_at": datetime.now(UTC).isoformat(timespec="seconds"), "config": cfg or None,
               "models": {"embedder": (cfg.get("embedding") or {}).get("model", "BAAI/bge-small-en-v1.5 (default)"),
                          "reranker": (cfg.get("read") or {}).get("rerank_model") or (cfg.get("read") or {}).get("rerank", "off"),
                          "reader": reader.get("model") or reader.get("reader_id"),
                          "judge": judge.get("model") or judge.get("judge_id")},
               "wall_clock_s": None, "n_questions": len(rows),
               "accuracy": (sum(r["correct"] for r in rows) / len(rows)) if rows else 0.0}
    s["by_category"] = {c: {"n": len(sub), "accuracy": sum(r["correct"] for r in sub) / len(sub)}
                        for c in CATS if (sub := [r for r in rows if r["category"] == c])}
    s["outcome_classes"] = dict(Counter(r["outcome_class"] for r in rows))
    wrong = [r for r in rows if not r["correct"]]
    s["primary_gaps"] = {g: sum(1 for r in wrong if r["primary_gap"] == g) for g in STAGE_ORDER}
    s["primary_gaps_by_category"] = {c: {g: sum(1 for r in wrong if r["category"] == c and r["primary_gap"] == g)
                                         for g in STAGE_ORDER} for c in CATS}
    staged = [r for r in rows if r["has_stage_log"] and r["gold_turns"]]
    if staged:
        tests = {"any_leg": lambda k: k["vector"] or k["lexical"] or any(k["extra"].values()),
                 "fused": lambda k: k["fused"], "pool": lambda k: k["pool"], "final": lambda k: k["final"],
                 "in_context": lambda k: k["in_context"]}
        s["funnel"] = {}
        for c in CATS + ["ALL"]:
            sub = [r for r in staged if c == "ALL" or r["category"] == c]
            if sub:
                s["funnel"][c] = {"n": len(sub), **{name: sum(all(fn(g["ranks"]) for g in r["gold_turns"]) for r in sub) / len(sub)
                                                    for name, fn in tests.items()},
                                  "correct": sum(r["correct"] for r in sub) / len(sub)}
    else:
        s["funnel"] = None
    s["oracle"] = {}
    for c in CATS + ["ALL"]:
        sub = [r for r in rows if (c == "ALL" or r["category"] == c) and r["gold_turns"]]
        full = [r for r in sub if all(g["in_context"] for g in r["gold_turns"])]
        rest = [r for r in sub if r not in full]
        if sub:
            s["oracle"][c] = {"n_all_gold_in_context": len(full),
                              "acc_all_gold_in_context": (sum(r["correct"] for r in full) / len(full)) if full else None,
                              "n_missing_gold": len(rest),
                              "acc_missing_gold": (sum(r["correct"] for r in rest) / len(rest)) if rest else None}
    combos = Counter(tuple(sorted({":".join(f.split(":")[:2]) for f in r["flags"]})) for r in wrong if len(r["flags"]) > 1)
    s["cascades"] = [{"flags": list(k), "count": v} for k, v in combos.most_common(25)]
    s["reader_buckets"] = dict(Counter(r["reader_bucket"]["bucket"] for r in wrong if r["reader_bucket"]))
    toks = [r["context_tokens"] for r in rows if r["context_tokens"]]
    lat = sorted(r["latency_answer_ms"] for r in rows if r["latency_answer_ms"])
    s["context_tokens_mean"] = statistics.mean(toks) if toks else None
    s["latency_answer_p50_ms"] = lat[len(lat) // 2] if lat else None
    s["latency_answer_p95_ms"] = lat[min(len(lat) - 1, int(.95 * len(lat)))] if lat else None
    if ingest:
        mism = sum(1 for r in ingest if r.get("valid_from") and (sd := session_day((turn_info.get((r.get("item"), r["turn"])) or {}).get("ts")))
                   and r["valid_from"][:10] != sd.isoformat())
        s["injection"] = {"turns": len(ingest), "written": sum(r["written"] for r in ingest),
                          "text_altered": sum(1 for r in ingest if r["written"] and r.get("text_identical") is False),
                          "no_date": sum(1 for r in ingest if r["written"] and not r.get("valid_from")),
                          "date_mismatch": mism,
                          "memory_types": dict(Counter(r.get("memory_type") for r in ingest)),
                          "batch_sizes": dict(Counter(str(r.get("batch_size")) for r in ingest))}
    else:
        s["injection"] = None
    s["fix_hypotheses"] = HYPOTHESES
    return s


def validate(rows: list[dict], summary: dict) -> None:
    try:
        import jsonschema
    except ImportError:
        print("jsonschema not installed: schema validation skipped")
        return
    qs = json.loads((HERE / "schemas" / "forensic_question.schema.json").read_text(encoding="utf-8"))
    rs = json.loads((HERE / "schemas" / "forensic_run.schema.json").read_text(encoding="utf-8"))
    v = jsonschema.Draft202012Validator(qs)
    for e in rows:
        errs = list(v.iter_errors(e))
        if errs:
            raise SystemExit(f"schema error in {e['item']}/{e['qid']}: {errs[0].message} at {list(errs[0].path)}")
    jsonschema.validate(summary, rs)


def write_markdown(out: Path, rows: list[dict], only_wrong: bool) -> None:
    L = ["# Per-question forensic log\n"]
    for e in rows:
        if only_wrong and e["correct"]:
            continue
        verdict = "CORRECT" if e["correct"] else f"WRONG - {e['outcome_class']} - primary gap: {e['primary_gap']}"
        L += [f"## {e['item']} / {e['qid']} - {e['category']} - {verdict}", f"- **Q:** {e['question']}",
              f"- **Gold:** {e['gold_answer']}  |  **Answer:** {e['answer']!r}  |  judge={e['judge_score']} status={e['status']}"]
        if e["reader_bucket"]:
            L.append(f"- **Reader failure type:** {e['reader_bucket']['bucket']} ({e['reader_bucket']['detail']})")
        if e["flags"]:
            L.append(f"- **All flags (cascade):** {', '.join(e['flags'])}")
        for g in e["gold_turns"]:
            L.append(f"- **Gold evidence {g['turn']}** (session {g['source_timestamp']}), in context={g['in_context']}: {g['text']}")
            if g["ingest"]:
                L.append(f"  - injection: written={g['ingest']['written']} text_identical={g['ingest']['text_identical']} valid_from={g['ingest']['valid_from']}")
            if g["ranks"]:
                k = g["ranks"]
                L.append(f"  - ranks: vector={k['vector']} bm25={k['lexical']} extra={k['extra']} fused={k['fused']} pool={k['pool']} "
                         f"rerank_order={k['rerank_order']} (score {k['rerank_score']}) final={k['final']} -> lost_at {g['lost_at']}"
                         + (f" (rescued by expansion from {g['rescued_from']})" if g["rescued_from"] else ""))
        if e["top_non_gold"] and not e["correct"]:
            L.append("- **What ranked instead:**")
            L += [f"  - #{x['rank']} {x['turn']}: {(x['text'] or '')[:160]}" for x in e["top_non_gold"][:3]]
        L.append("")
    (out / "per_question.md").write_text("\n".join(L), encoding="utf-8")


def write_gap_md(out: Path, s: dict) -> None:
    pc = lambda x: "n/a" if x is None else f"{100 * x:.1f}%"  # noqa: E731
    L = [f"# Gap summary - {s['run_id']}\n", f"- mode {s['mode']}, stage log {s['has_stage_log']}, "
         f"{s['n_questions']} questions, accuracy {pc(s['accuracy'])}", f"- models: {s['models']}\n",
         "| Category | n | accuracy |", "|---|---|---|"]
    L += [f"| {c} | {v['n']} | {pc(v['accuracy'])} |" for c, v in s["by_category"].items()]
    L += ["\n## Outcome classes", *[f"- {k}: {v}" for k, v in s["outcome_classes"].items()],
          "\n## Primary gap (wrong questions)", *[f"- {k}: {v}" for k, v in s["primary_gaps"].items()]]
    if s["funnel"]:
        L += ["\n## Funnel (all gold survives)", "| Category | n | any leg | fused | pool | final | in context | correct |",
              "|---|---|---|---|---|---|---|---|"]
        L += [f"| {c} | {v['n']} | {pc(v['any_leg'])} | {pc(v['fused'])} | {pc(v['pool'])} | {pc(v['final'])} | "
              f"{pc(v['in_context'])} | {pc(v['correct'])} |" for c, v in s["funnel"].items()]
    L += ["\n## Oracle", *[f"- {c}: acc with all gold in context {pc(v['acc_all_gold_in_context'])} (n={v['n_all_gold_in_context']}), "
                            f"without {pc(v['acc_missing_gold'])} (n={v['n_missing_gold']})" for c, v in s["oracle"].items()]]
    L += ["\n## Cascades", *[f"- {c['count']} x {' + '.join(c['flags'])}" for c in s["cascades"]]]
    L += ["\n## Reader failure types", *[f"- {k}: {v}" for k, v in s["reader_buckets"].items()]]
    if s["injection"]:
        L += ["\n## Injection audit", *[f"- {k}: {v}" for k, v in s["injection"].items()]]
    L += ["\n## Fix hypotheses", *[f"- **{k}**: {v}" for k, v in s["fix_hypotheses"].items()]]
    (out / "gap_summary.md").write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--forensics", default=None)
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only-wrong-md", action="store_true")
    build(ap.parse_args())
