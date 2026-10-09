"""Per-question forensic report: injection -> retrieval stages -> context -> answer -> verdict.

    python evals/forensics_report.py --run runs/<run>--memspine --forensics runs/<fx-dir> \
        --data data/locomo10.json --out runs/<fx-dir>/report [--only-wrong]

Inputs (all written by a run with ``MEMSPINE_FORENSICS_DIR=<fx-dir>``):
  results.jsonl   verdict, answer, gold per question (the harness)
  forensics.jsonl one line per question: ranked ids at every search stage
  ingest.jsonl    one line per turn written: source text vs stored record, event time

Outputs in ``--out``:
  per_question.jsonl   every field, machine-readable
  per_question.md      one readable block per question (the log)
  gap_summary.md       funnel by category, primary gaps, cascades, oracle ceilings, fix hypotheses
  injection_audit.md   write-side findings over every turn

A question's **primary gap** is the earliest pipeline stage that lost a gold evidence turn (weakest link
over all its gold turns). **All flags** are kept too, so cascades (e.g. a stored-date error AND a
fusion cut) stay visible.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != Path(__file__).parent.resolve()]
sys.path.insert(0, str(Path(__file__).parent))
sys.path.append(str(Path(__file__).parent))

from failure_buckets import bucket_failure, parse_interval  # noqa: E402,F401
from memspine_evals.datasets import LoCoMoDataset  # noqa: E402

CAT = {"cat1": "multi-hop", "cat2": "temporal", "cat3": "open-domain", "cat4": "single-hop"}
STAGE_ORDER = ["ingest", "recall", "fusion", "gate", "rerank", "assembly", "reader"]
HYPOTHESES = {
    "ingest": "Write side: turn dropped/merged, text altered, or filed under a wrong event date. Fix at deposit "
    "(session stamp -> valid_from, speaker prefix, image captions, relative-date resolution).",
    "recall": "No leg (vector top-30, BM25 top-30, extra legs) surfaced the gold turn. Options: query rewrite / "
    "planner probes (#35), entity-expand and cohesion legs, session-digest leg, larger fetch window, a better or "
    "second embedder, fact extraction at write time, word2vec+BM25 leg (future plan).",
    "fusion": "Gold was in a leg but fell outside the fused top-k. Options: leg weights (N16), RRF k, min-max fusion "
    "(N52), widen the fused pool (candidate_pool > 1) so rerank sees more than top-10.",
    "gate": "Gold survived fusion but was removed by a visibility/trust/tag gate. Check gates and header-hide rules.",
    "rerank": "Gold was in the reranker pool but the reranker demoted it out of the final cut. Options: blend with "
    "retrieval prior (rerank_blend), confidence gate, date prefix, session-neighbour context, bigger keep.",
    "assembly": "Gold ranked high but was cut by the token budget or relative floor. Options: budget, "
    "relative_floor, dedupe / compression.",
    "reader": "All gold evidence was in the context and the answer was still wrong: reader or composition gap "
    "(date arithmetic, granularity, refusal, list completeness) or a judge disagreement.",
}


def gold_turns(raw: list[str]) -> list[str]:
    out: list[str] = []
    for e in raw:
        out += [p.strip() for p in str(e).replace(";", ",").split(",") if p.strip()]
    return out


def rank_of(stage: list[dict], turn: str) -> int | None:
    for r in stage:
        if r["turn"] == turn:
            return r["r"]
    return None


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--forensics", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only-wrong", action="store_true")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    ds = LoCoMoDataset(args.data, revision_id="auto", categories=(1, 2, 3, 4))
    turn_info: dict[tuple[str, str], dict] = {}
    gold_by_q: dict[tuple[str, str], tuple[list[str], str]] = {}
    for item in ds.items():
        for t in item.history:
            turn_info[(item.item_id, t.turn_id)] = {
                "text": f"{t.speaker}: {t.text}", "ts": t.timestamp, "session": t.session_id}
        for q in item.queries:
            gold_by_q[(item.item_id, q.query_id)] = (gold_turns(list(q.gold_turn_ids)), q.text)

    results = [r for r in load_jsonl(Path(args.run) / "results.jsonl") if r.get("kind") == "result"]
    fx = load_jsonl(Path(args.forensics) / "forensics.jsonl")
    ingest = load_jsonl(Path(args.forensics) / "ingest.jsonl")
    ing: dict[tuple[str, str], dict] = {(r["item"], r["turn"]): r for r in ingest}
    fx_by_q: dict[tuple[str, str], dict] = {}
    # forensics rows are in query order per item; join by (item, question text, occurrence)
    seen: Counter = Counter()
    fx_index: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in fx:
        fx_index[(row["item"], row["query"])].append(row)
    for r in results:
        key = (r["item_id"], r["question"])
        n = seen[key]
        seen[key] += 1
        if n < len(fx_index.get(key, [])):
            fx_by_q[(r["item_id"], r["query_id"])] = fx_index[key][n]

    rows: list[dict] = []
    for r in results:
        key = (r["item_id"], r["query_id"])
        gold_ids, _ = gold_by_q.get(key, ([], ""))
        f = fx_by_q.get(key)
        correct = r["score"] >= 1.0 and r["status"] == "completed" and bool((r.get("answer") or "").strip())
        entry = {
            "item": r["item_id"], "qid": r["query_id"], "category": CAT.get(r["type_label"], r["type_label"]),
            "question": r["question"], "gold_answer": r["gold"], "answer": r["answer"], "status": r["status"],
            "judge_score": r["score"], "correct": correct, "gold_turns": [],
            "flags": [], "primary_gap": None, "context_tokens": r.get("context_tokens"),
            "latency_retrieve_ms": r.get("latency_retrieve_ms"), "latency_answer_ms": r.get("latency_answer_ms"),
        }
        in_context = {c["turn"] for c in (f or {}).get("context_records", [])}
        stage_losses: list[str] = []
        for g in gold_ids:
            info = turn_info.get((r["item_id"], g), {})
            i = ing.get((r["item_id"], g))
            gt = {"turn": g, "text": info.get("text"), "source_timestamp": info.get("ts")}
            loss: str | None = None
            if i is not None:
                gt["ingest"] = {"written": i["written"], "text_identical": i["text_identical"],
                                "valid_from": i["valid_from"], "record_id": i["record_id"]}
                if not i["written"]:
                    loss = "ingest"
                    entry["flags"].append(f"ingest:not_written:{g}")
                elif i["text_identical"] is False:
                    entry["flags"].append(f"ingest:text_altered:{g}")
                if i["valid_from"] and info.get("ts"):
                    sd = _session_day(info["ts"])
                    if sd and i["valid_from"][:10] != sd.isoformat():
                        entry["flags"].append(f"ingest:date_mismatch:{g}")
                        loss = loss or None
            if f is not None:
                ranks = {
                    "vector": rank_of(f["vector"], g), "lexical": rank_of(f["lexical"], g),
                    "fused": rank_of(f["fused"], g), "pool": rank_of(f["pool"], g),
                    "rerank": rank_of(f["rerank_scores"], g) if f["rerank_scores"] else None,
                    "final": rank_of(f["final"], g), "in_context": g in in_context,
                }
                ranks["extra"] = {n: rank_of(h, g) for n, h in f["extra_legs"].items()}
                if f["rerank_scores"]:
                    srt = sorted(f["rerank_scores"], key=lambda x: -x["score"])
                    ranks["rerank_order"] = next((k + 1 for k, x in enumerate(srt) if x["turn"] == g), None)
                    ranks["rerank_score"] = next((x["score"] for x in f["rerank_scores"] if x["turn"] == g), None)
                gt["ranks"] = ranks
                any_leg = (ranks["vector"] or ranks["lexical"] or any(v for v in ranks["extra"].values()))
                if loss is None:
                    if not any_leg:
                        loss = "recall"
                    elif ranks["fused"] is None:
                        loss = "fusion"
                    elif ranks["pool"] is None:
                        loss = "gate"
                    elif f["rerank_scores"] and ranks["final"] is None:
                        loss = "rerank"
                    elif ranks["final"] is None:
                        loss = "fusion"
                    elif not ranks["in_context"]:
                        loss = "assembly"
            if loss and loss not in ("ingest",) and gt.get("ranks", {}).get("in_context"):
                # The search stages dropped it, but a later step (session/neighbour expansion in replay
                # mode) put the turn into the context anyway: not a loss, a rescue worth knowing about.
                entry["flags"].append(f"rescued_by_expansion:{g}:was_lost_at_{loss}")
                gt["rescued_from"] = loss
                loss = None
            gt["lost_at"] = loss
            if loss:
                stage_losses.append(loss)
            entry["gold_turns"].append(gt)
        if stage_losses:
            entry["primary_gap"] = min(stage_losses, key=STAGE_ORDER.index)
            entry["flags"] += [f"lost:{s}" for s in stage_losses]
        elif not correct:
            entry["primary_gap"] = "reader"
            bucket, detail = bucket_failure(r["question"], str(r["gold"]), r.get("answer") or "")
            entry["reader_bucket"] = {"bucket": bucket, "detail": detail}
        if not correct and (r.get("answer") or "").strip() and str(r["gold"]).strip().lower() in (r["answer"] or "").lower():
            entry["flags"].append("judge:answer_contains_gold")
        if f is not None:
            entry["stages"] = {k: f[k] for k in ("vector", "lexical", "extra_legs", "fused", "pool", "reranker",
                                                  "rerank_scores", "final")}
            outrank = [x for x in f["final"] if x["turn"] not in gold_ids][:5]
            entry["top_non_gold"] = [
                {"turn": x["turn"], "rank": x["r"], "score": x["score"],
                 "text": (turn_info.get((r["item_id"], x["turn"])) or {}).get("text")} for x in outrank]
            entry["context_text"] = [c["text"] for c in f["context_records"]]
        rows.append(entry)

    (out / "per_question.jsonl").write_text("\n".join(json.dumps(e) for e in rows), encoding="utf-8")
    write_markdown(out, rows, args.only_wrong)
    write_summary(out, rows)
    write_injection(out, ingest, turn_info)
    print(f"{len(rows)} questions, {sum(not e['correct'] for e in rows)} wrong -> {out}")


def _session_day(stamp: str) -> date | None:
    from failure_buckets import _session_date

    return _session_date(stamp)


def write_markdown(out: Path, rows: list[dict], only_wrong: bool) -> None:
    L = ["# Per-question forensic log\n"]
    for e in rows:
        if only_wrong and e["correct"]:
            continue
        L.append(f"## {e['item']} / {e['qid']} - {e['category']} - {'CORRECT' if e['correct'] else 'WRONG'}"
                 f"{'' if e['correct'] else ' - primary gap: ' + str(e['primary_gap'])}")
        L.append(f"- **Q:** {e['question']}")
        L.append(f"- **Gold:** {e['gold_answer']}  |  **Answer:** {e['answer']!r}  |  judge={e['judge_score']} status={e['status']}")
        if e.get("reader_bucket"):
            L.append(f"- **Reader failure type:** {e['reader_bucket']['bucket']} ({e['reader_bucket']['detail']})")
        if e["flags"]:
            L.append(f"- **All flags (cascade):** {', '.join(e['flags'])}")
        for g in e["gold_turns"]:
            L.append(f"- **Gold evidence {g['turn']}** (source ts {g['source_timestamp']}): {g['text']}")
            ing = g.get("ingest")
            if ing:
                L.append(f"  - injection: written={ing['written']} text_identical={ing['text_identical']} valid_from={ing['valid_from']}")
            rk = g.get("ranks")
            if rk:
                L.append(f"  - ranks: vector={rk['vector']} bm25={rk['lexical']} extra={rk['extra']} fused={rk['fused']} "
                         f"pool={rk['pool']} rerank_order={rk.get('rerank_order')} (score {rk.get('rerank_score')}) "
                         f"final={rk['final']} in_context={rk['in_context']}  -> lost_at: {g['lost_at']}{' (rescued by expansion from ' + g['rescued_from'] + ')' if g.get('rescued_from') else ''}")
        if e.get("top_non_gold") and not e["correct"]:
            L.append("- **What ranked instead (final top non-gold):**")
            for x in e["top_non_gold"][:3]:
                L.append(f"  - #{x['rank']} {x['turn']}: {(x['text'] or '')[:160]}")
        L.append("")
    (out / "per_question.md").write_text("\n".join(L), encoding="utf-8")


def pct(a: int, b: int) -> str:
    return f"{100 * a / b:.1f}%" if b else "n/a"


def write_summary(out: Path, rows: list[dict]) -> None:
    L = ["# Gap summary\n"]
    cats = ["single-hop", "multi-hop", "temporal", "open-domain"]
    L.append("## Accuracy and primary gap by category (wrong questions only counted under their primary gap)\n")
    L.append("| Category | n | correct | " + " | ".join(STAGE_ORDER) + " |")
    L.append("|---|---|---|" + "---|" * len(STAGE_ORDER))
    for c in cats + ["ALL"]:
        sub = [e for e in rows if c == "ALL" or e["category"] == c]
        if not sub:
            continue
        cnt = Counter(e["primary_gap"] for e in sub if not e["correct"])
        L.append(f"| {c} | {len(sub)} | {pct(sum(e['correct'] for e in sub), len(sub))} | "
                 + " | ".join(str(cnt.get(s, 0)) for s in STAGE_ORDER) + " |")
    L.append("\n## Retrieval funnel: share of questions whose gold evidence survives each stage (all gold turns)\n")
    L.append("| Category | any leg | fused top-k | pool | final | in context | answered right |")
    L.append("|---|---|---|---|---|---|---|")
    for c in cats + ["ALL"]:
        sub = [e for e in rows if (c == "ALL" or e["category"] == c) and e["gold_turns"] and all("ranks" in g for g in e["gold_turns"])]
        if not sub:
            continue

        def surv(fn):
            return sum(all(fn(g["ranks"]) for g in e["gold_turns"]) for e in sub)

        L.append(f"| {c} ({len(sub)}) | " + " | ".join(pct(surv(fn), len(sub)) for fn in (
            lambda r: r["vector"] or r["lexical"] or any(r["extra"].values()),
            lambda r: r["fused"], lambda r: r["pool"], lambda r: r["final"], lambda r: r["in_context"])
        ) + f" | {pct(sum(e['correct'] for e in sub), len(sub))} |")
    L.append("\n## Oracle ceilings (how much each gap could possibly buy)\n")
    for c in cats + ["ALL"]:
        sub = [e for e in rows if (c == "ALL" or e["category"] == c) and e["gold_turns"]]
        full = [e for e in sub if all(g.get("ranks", {}).get("in_context") for g in e["gold_turns"])]
        if not sub:
            continue
        acc_full = sum(e["correct"] for e in full) / len(full) if full else 0
        L.append(f"- **{c}**: accuracy when all gold is in context = {100 * acc_full:.1f}% (n={len(full)}); "
                 f"when not = {pct(sum(e['correct'] for e in sub if e not in full), len(sub) - len(full))} (n={len(sub) - len(full)}). "
                 f"Perfect retrieval would lift accuracy to about {100 * (acc_full * 1):.1f}% for this category.")
    L.append("\n## Cascades: wrong questions with more than one flag\n")
    wrong = [e for e in rows if not e["correct"]]
    multi = Counter(tuple(sorted({f.split(':')[0] + ':' + f.split(':')[1] for f in e['flags']})) for e in wrong if len(e["flags"]) > 1)
    for combo, n in multi.most_common(15):
        L.append(f"- {n} x {' + '.join(combo)}")
    L.append("\n## Reader failure types (gold fully in context, answer wrong)\n")
    bk = Counter(e["reader_bucket"]["bucket"] for e in wrong if e.get("reader_bucket"))
    for b, n in bk.most_common():
        L.append(f"- {b}: {n}")
    L.append("\n## Fix hypotheses per gap\n")
    for s in STAGE_ORDER:
        n = sum(1 for e in wrong if e["primary_gap"] == s)
        L.append(f"- **{s}** ({n} wrong questions): {HYPOTHESES[s]}")
    (out / "gap_summary.md").write_text("\n".join(L), encoding="utf-8")


def write_injection(out: Path, ingest: list[dict], turn_info: dict) -> None:
    n = len(ingest)
    L = ["# Injection audit\n", f"- turns logged: {n}"]
    L.append(f"- written as a record: {sum(r['written'] for r in ingest)} ({pct(sum(r['written'] for r in ingest), n)})")
    altered = [r for r in ingest if r["written"] and r["text_identical"] is False]
    L.append(f"- stored text differs from source: {len(altered)}")
    nodate = [r for r in ingest if r["written"] and not r["valid_from"]]
    L.append(f"- records without an event date (valid_from): {len(nodate)}")
    mism = []
    for r in ingest:
        info = turn_info.get((r["item"], r["turn"]))
        if r["valid_from"] and info and info.get("ts"):
            sd = _session_day(info["ts"])
            if sd and r["valid_from"][:10] != sd.isoformat():
                mism.append(r)
    L.append(f"- stored date differs from the session date: {len(mism)}")
    L.append(f"- memory types: {dict(Counter(r['memory_type'] for r in ingest))}")
    L.append(f"- batch sizes: {dict(Counter(r['batch_size'] for r in ingest))}")
    lens = sorted(len(r['source_text']) for r in ingest)
    if lens:
        L.append(f"- source text length chars: min {lens[0]}, median {lens[len(lens) // 2]}, max {lens[-1]}")
    for title, rs in (("Altered text examples", altered), ("Date mismatch examples", mism)):
        L.append(f"\n## {title}")
        for r in rs[:8]:
            L.append(f"- {r['item']} {r['turn']}: source={r['source_text'][:100]!r} stored={str(r['stored_text'])[:100]!r} valid_from={r['valid_from']}")
    (out / "injection_audit.md").write_text("\n".join(L), encoding="utf-8")


main()
