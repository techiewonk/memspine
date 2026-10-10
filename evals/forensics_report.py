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

from failure_buckets import _list_items, _session_date, _words, bucket_failure  # noqa: E402
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


def list_recall(question: str, gold: str | None, answer: str | None) -> tuple[int, int] | None:
    """Gap A3: (items found, items) for a short-list gold, else None.

    An item counts as found when every content word of it appears in the answer, matching on
    a shared prefix in either direction ("win"/"winning", "painting"/"paintings"). Date golds
    ("25 May, 2022") are not lists.
    """
    from failure_buckets import parse_interval

    if parse_interval(str(gold or "")) is not None:  # "25 May, 2022" is a date, not a list
        return None
    items = _list_items(question or "", str(gold or ""))
    if not items:
        return None
    words = _words(answer or "")

    def present(w: str) -> bool:
        stem = w[: max(3, min(5, len(w)))]
        return any(a.startswith(stem) or w.startswith(a[:5]) and len(a) >= 3 for a in words)

    found = sum(1 for item in items if _words(item) and all(present(w) for w in _words(item)))
    return found, len(items)


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


def join_forensics(results: list[dict], fx: list[dict]) -> dict[tuple[str, str], dict]:
    """Pair each result row with its forensics row, keyed ``(item_id, query_id)``.

    D7 [INJ-6]: rows written with a ``query_id`` (and ``run_id``) join on it, preferring the row
    of the same run. Older logs have neither; those pair by question text in order of
    appearance (duplicate questions pair first-to-first), the legacy join.
    """
    by_qid: dict[tuple, list[dict]] = defaultdict(list)
    by_text: dict[tuple, list[dict]] = defaultdict(list)
    for row in fx:
        if row.get("query_id") is not None:
            by_qid[(row.get("item"), row["query_id"])].append(row)
        else:
            by_text[(row.get("item"), row["query"])].append(row)
    seen: Counter = Counter()
    joined: dict[tuple[str, str], dict] = {}
    for r in results:
        qkey = (r["item_id"], r["query_id"])
        candidates = by_qid.get(qkey)
        if candidates:
            same_run = [c for c in candidates if c.get("run_id") == r.get("run_id")]
            joined[qkey] = (same_run or candidates)[-1]
            continue
        key = (r["item_id"], r["question"])
        n = seen[key]
        seen[key] += 1
        if n < len(by_text.get(key, [])):
            joined[qkey] = by_text[key][n]
    return joined


def build(args) -> None:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    run_dir = Path(args.run)
    run_id = run_dir.name.removesuffix("--memspine")

    ds = LoCoMoDataset(args.data, revision_id="auto", categories=(1, 2, 3, 4, 5))  # cat 5 is reported apart (A13)
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
    run_ids = {r.get("run_id") for r in results}
    ing = {
        (r.get("item"), r["turn"]): r
        for r in ingest
        if r.get("run_id") is None or r.get("run_id") in run_ids  # D7: other runs' rows out
    }
    fx_by_q = join_forensics(results, fx)

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
            "list_recall": None,
            "gold_turns": [], "flags": [], "primary_gap": None, "reader_bucket": None,
            "stages": None, "top_non_gold": None, "context_text": None,
        }
        lr = list_recall(r["question"], r.get("gold"), answer) if mode == "qa" else None
        if lr is not None:
            entry["list_recall"] = {"found": lr[0], "items": lr[1], "recall": lr[0] / lr[1]}
            if correct and lr[0] < lr[1]:
                entry["flags"].append("judge:credited_partial_list")
            if not correct and lr[0] == lr[1]:
                entry["flags"].append("judge:rejected_complete_list")
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
                for extra in ("quarantined", "trust", "status"):  # F2 [INJ-2]
                    if i.get(extra) is not None:
                        gt["ingest"][extra] = i[extra]
                if i.get("quarantined"):
                    entry["flags"].append(f"ingest:quarantined:{g}")
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

    no_errata = getattr(args, "no_errata", False)
    errata = {} if no_errata else load_errata(getattr(args, "errata", DEFAULT_ERRATA))
    verdict = {(e["item"], e["qid"]): e["correct"] for e in rows}
    fx_joined = [dict(f, **({"_correct": verdict[k]} if k in verdict else {})) for k, f in fx_by_q.items()]
    summary = summarise(run_id, rows, manifest, ingest, turn_info, errata, fx_joined)
    validate(rows, summary)
    (out / "per_question.jsonl").write_text("\n".join(json.dumps(e) for e in rows), encoding="utf-8")
    (out / "run_summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    write_markdown(out, rows, args.only_wrong_md)
    write_gap_md(out, summary)
    print(f"{run_id}: {summary['n_questions']} questions (+{summary['n_adversarial']} adversarial), accuracy {summary['accuracy']:.1%}, stage log={summary['has_stage_log']} -> {out}")
    if summary["errata"]:
        e = summary["errata"]["strict"]
        print(f"  excluding errata: {e['n']} questions ({e['n_excluded']} excluded), "
              f"accuracy {e['accuracy']:.1%}")


#: A12: the non-adversarial LoCoMo question set (categories 1-4) every headline is over.
QA_SET_SIZE = 1540
ADVERSARIAL = "adversarial"


def adversarial_block(adv: list[dict]) -> dict | None:
    """A13: the cat-5 (abstention) rows, apart from the headline. Accuracy is the judge's
    verdict on whether the answer abstained; a retrieval-only run has no answer to judge, so
    its accuracy is None (cat 5 has no gold evidence to retrieve)."""
    if not adv:
        return None
    qa = adv[0]["mode"] == "qa"
    return {
        "label": ADVERSARIAL,
        "n": len(adv),
        "accuracy": (sum(r["correct"] for r in adv) / len(adv)) if qa else None,
        "note": "LoCoMo category 5 (unanswerable; graded as abstention); excluded from the "
        f"{QA_SET_SIZE}-question headline",
    }


def recall_at_10_hits(rows: list[dict]) -> dict | None:
    """A12: share of questions whose gold turns are all in the engine's final top-10 search
    hits, before neighbour expansion (the stage log's ``final`` list). None without a stage log."""
    staged = [r for r in rows if r["has_stage_log"] and r["gold_turns"]]
    if not staged:
        return None
    ok = sum(
        all(g["ranks"]["final"] is not None and g["ranks"]["final"] <= 10 for g in r["gold_turns"])
        for r in staged
    )
    return {"n": len(staged), "value": ok / len(staged)}


#: A5: errata tags that make the gold answer unusable for grading (the question is dropped from the
#: "excluding errata" headline). ``evidence_label_error`` keeps a right answer: not dropped.
ERRATA_EXCLUDE_TAGS = ("gold_error", "needs_image")
DEFAULT_ERRATA = HERE / "analysis" / "locomo_errata.json"


def load_errata(path: Path | str | None = DEFAULT_ERRATA) -> dict[tuple[str, str], dict]:
    """(item, qid) -> {"tag", "borderline"} for the entries that invalidate grading; {} if no file.
    I14: delegates to the generic ``memspine_evals.errata`` (any benchmark; a file may carry its
    own ``exclude_tags``, else ``ERRATA_EXCLUDE_TAGS`` applies here)."""
    from memspine_evals.errata import load_errata as _load

    return _load(path, exclude_tags=ERRATA_EXCLUDE_TAGS)


def errata_block(rows: list[dict], errata: dict[tuple[str, str], dict]) -> dict:
    """A5: accuracy over the categories 1-4 rows with and without the errata questions.
    ``strict`` drops confirmed errata only; ``incl_borderline`` also drops the borderline ones."""
    def acc(sub: list[dict]) -> float | None:
        return (sum(r["correct"] for r in sub) / len(sub)) if sub else None

    def variant(drop_borderline: bool) -> dict:
        def hit(r: dict) -> bool:
            e = errata.get((r["item"], r["qid"]))
            return e is not None and (drop_borderline or not e["borderline"])

        kept = [r for r in rows if not hit(r)]

        def by_cat(kept: list[dict]) -> dict:
            out = {}
            for c in CATS:
                total = sum(1 for r in rows if r["category"] == c)
                sub = [r for r in kept if r["category"] == c]
                if total:
                    out[c] = {"n": len(sub), "n_excluded": total - len(sub), "accuracy": acc(sub)}
            return out

        return {
            "n": len(kept), "n_excluded": len(rows) - len(kept), "accuracy": acc(kept),
            "by_category": by_cat(kept),
        }

    return {"tags_excluded": list(ERRATA_EXCLUDE_TAGS), "n_errata_entries": len(errata),
            "overall_accuracy": acc(rows), "n": len(rows),
            "strict": variant(False), "incl_borderline": variant(True)}


def _prf(tp: int, fp: int, fn: int) -> dict:
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    f1 = 2 * p * r / (p + r) if p and r else (0.0 if p is not None and r is not None else None)
    return {"tp": tp, "fp": fp, "fn": fn, "precision": p, "recall": r, "f1": f1}


def abstention_block(all_rows: list[dict]) -> dict | None:
    """I24: abstention precision / recall / F1, from the saved answers (no judge).

    The positive class is "the system refused" (``refusal.is_refusal`` on the answer: empty,
    declines, or denies the premise). A refusal is *right* on LoCoMo category 5 (gold is the
    refusal) and *wrong* on categories 1-4 (the gold is an answer). Recall = share of cat-5
    questions refused; precision = share of refusals that were on cat-5 questions; the
    answer-when-answerable rate is the share of cat-1-4 questions answered (1 - false refusal
    rate). None for retrieval-only runs (no answers) or runs without a cat-5 row.
    """
    from memspine_evals.refusal import is_refusal

    qa = [r for r in all_rows if r["mode"] == "qa" and r.get("status") == "completed"
          and r.get("answer") is not None]
    if not qa or not any(r["category"] == ADVERSARIAL for r in qa):
        return None
    tp = fp = fn = tn = 0
    for r in qa:
        refused = is_refusal(r["answer"])
        should = r["category"] == ADVERSARIAL
        tp += refused and should
        fp += refused and not should
        fn += (not refused) and should
        tn += (not refused) and not should
    out = _prf(tp, fp, fn)
    out.update(
        n=len(qa), n_should_refuse=tp + fn, n_answerable=fp + tn,
        answer_rate_when_answerable=tn / (fp + tn) if fp + tn else None,
        false_refusal_rate=fp / (fp + tn) if fp + tn else None,
        note="positive = refused (is_refusal on the answer text); cat 5 should refuse, cats 1-4 should answer",
    )
    return out


def by_category_all(all_rows: list[dict]) -> dict:
    """I24: per-category table including cat 5 (adversarial), judge accuracy and refusal rate."""
    from memspine_evals.refusal import is_refusal

    out: dict[str, dict] = {}
    for c in CATS + [ADVERSARIAL]:
        sub = [r for r in all_rows if r["category"] == c]
        if not sub:
            continue
        answered = [r for r in sub if r["mode"] == "qa" and r.get("answer") is not None]
        out[c] = {
            "n": len(sub), "accuracy": sum(r["correct"] for r in sub) / len(sub),
            "refusal_rate": (sum(is_refusal(r["answer"]) for r in answered) / len(answered)) if answered else None,
        }
    return out


def memory_used_block(all_rows: list[dict]) -> dict | None:
    """I24: share of queries whose context was non-empty (any retrieved turn or context tokens)."""
    if not all_rows:
        return None

    def used(r: dict) -> bool:
        return bool(r.get("retrieved_turns")) or bool(r.get("context_tokens"))

    cats = sorted({r["category"] for r in all_rows})
    return {"n": len(all_rows), "rate": sum(used(r) for r in all_rows) / len(all_rows),
            "by_category": {c: sum(used(r) for r in all_rows if r["category"] == c)
                            / sum(1 for r in all_rows if r["category"] == c) for c in cats}}


def trigger_block(fx_rows: list[dict]) -> dict | None:
    """I24: how often each optional retrieval trigger fired, from the forensics rows.

    list mode = the ``speaker_vote*`` leg is present (only added when the question matched
    ``read.list_trigger``); session leg / temporal / bridge / cohesion legs = their extra leg has
    hits; bridge gate = the recorded ``bridge_gate`` decisions (always / cue / weak / skipped;
    absent in logs written before 2026-10-10); decider = the recorded ``decider`` values, if any.
    A trigger absent from every row is reported as ``logged: false`` rather than a rate of 0.
    """
    if not fx_rows:
        return None
    n = len(fx_rows)
    legs = Counter()
    for f in fx_rows:
        for name, hits in (f.get("extra_legs") or {}).items():
            if hits:
                legs[name] += 1
    list_n = sum(1 for f in fx_rows if any(k.startswith("speaker_vote") and v for k, v in (f.get("extra_legs") or {}).items()))
    gates = Counter(str(f["bridge_gate"]) for f in fx_rows if f.get("bridge_gate") is not None)
    bridge_fired = sum(1 for f in fx_rows if (f.get("extra_legs") or {}).get("bridge"))
    deciders = Counter(json.dumps(f["decider"], sort_keys=True) if not isinstance(f["decider"], str)
                       else f["decider"] for f in fx_rows if f.get("decider") is not None)
    reranked = sum(1 for f in fx_rows if f.get("rerank_scores"))

    def row(count: int) -> dict:
        return {"fired": count, "rate": count / n}

    out: dict = {
        "n": n,
        "list_mode": row(list_n),
        "bridge_hop": {**row(bridge_fired), "gate_logged": bool(gates),
                       "gate_decisions": dict(gates)},
        "rerank": row(reranked),
        "legs": {k: row(v) for k, v in sorted(legs.items())},
        "decider": {"logged": bool(deciders), "decisions": dict(deciders)},
    }
    if any("_correct" in f for f in fx_rows):
        def acc(flag) -> dict:
            on = [bool(f["_correct"]) for f in fx_rows if "_correct" in f and flag(f)]
            off = [bool(f["_correct"]) for f in fx_rows if "_correct" in f and not flag(f)]
            return {"accuracy_fired": (sum(on) / len(on)) if on else None, "n_fired": len(on),
                    "accuracy_not_fired": (sum(off) / len(off)) if off else None, "n_not_fired": len(off)}
        out["list_mode"].update(acc(lambda f: any(k.startswith("speaker_vote") and v for k, v in (f.get("extra_legs") or {}).items())))
        out["bridge_hop"].update(acc(lambda f: bool((f.get("extra_legs") or {}).get("bridge"))))
    return out


def summarise(run_id: str, rows: list[dict], manifest: dict, ingest: list[dict], turn_info: dict,
              errata: dict[tuple[str, str], dict] | None = None, fx_rows: list[dict] | None = None) -> dict:
    all_rows = rows
    rows = [r for r in all_rows if r["category"] != ADVERSARIAL]  # headline: categories 1-4 only
    adv = [r for r in all_rows if r["category"] == ADVERSARIAL]
    sysconf = ((manifest.get("system") or {}).get("config") or {})
    cfg = sysconf.get("config") or {}
    reader = (manifest.get("reader") or {})
    judge = (manifest.get("judge") or {})
    s: dict = {"schema_version": "forensic_run/v1", "run_id": run_id,
               "mode": (rows or all_rows)[0]["mode"] if all_rows else "qa", "has_stage_log": any(r["has_stage_log"] for r in rows),
               "generated_at": datetime.now(UTC).isoformat(timespec="seconds"), "config": cfg or None,
               "models": {"embedder": (cfg.get("embedding") or {}).get("model", "BAAI/bge-small-en-v1.5 (default)"),
                          "reranker": (cfg.get("read") or {}).get("rerank_model") or (cfg.get("read") or {}).get("rerank", "off"),
                          "reader": reader.get("model") or reader.get("reader_id"),
                          "judge": judge.get("model") or judge.get("judge_id")},
               "wall_clock_s": None, "n_questions": len(rows),
               "accuracy": (sum(r["correct"] for r in rows) / len(rows)) if rows else 0.0}
    s["errata"] = errata_block(rows, errata) if errata else None
    s["n_adversarial"] = len(adv)
    s["adversarial"] = adversarial_block(adv)
    s["abstention"] = abstention_block(all_rows)  # I24
    s["by_category_all"] = by_category_all(all_rows)
    s["memory_used"] = memory_used_block(all_rows)
    s["triggers"] = trigger_block(fx_rows or [])
    s["recall_at_10_hits"] = recall_at_10_hits(rows)
    s["sufficiency_on_qa_set"] = (
        {"n": len(rows), "value": sum(r["correct"] for r in rows) / len(rows),
         "complete_qa_set": len(rows) == QA_SET_SIZE}
        if s["mode"] == "retrieval" and rows else None
    )
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
    lists = [r for r in rows if r.get("list_recall")]
    s["list_questions"] = {
        "n": len(lists),
        "mean_recall": (sum(r["list_recall"]["recall"] for r in lists) / len(lists)) if lists else None,
        "judged_correct_partial_list": sum(1 for r in lists if r["correct"] and r["list_recall"]["recall"] < 1),
        "judged_wrong_complete_list": sum(1 for r in lists if not r["correct"] and r["list_recall"]["recall"] == 1),
    } if lists else None
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
    er = s.get("errata")
    if er:
        st, ib = er["strict"], er["incl_borderline"]
        L += [f"\n## Excluding errata ({', '.join(er['tags_excluded'])})",
              "| Category | n | excluded | accuracy |", "|---|---|---|---|",
              f"| overall (with errata) | {er['n']} | 0 | {pc(er['overall_accuracy'])} |",
              f"| overall (excluding errata) | {st['n']} | {st['n_excluded']} | "
              f"{pc(st['accuracy'])} |",
              *[f"| {c} | {v['n']} | {v['n_excluded']} | {pc(v['accuracy'])} |"
                for c, v in st["by_category"].items()],
              f"\n- also excluding borderline entries: n={ib['n']} ({ib['n_excluded']} excluded), "
              f"accuracy {pc(ib['accuracy'])}"]
    adv = s.get("adversarial")
    if adv:
        L += [f"\n- adversarial (cat 5, apart from the headline): n={adv['n']}, accuracy {pc(adv['accuracy'])}"]
    if s.get("by_category_all"):
        L += ["\n## Per category, including cat 5 (adversarial)", "| Category | n | judge accuracy | refusal rate |",
              "|---|---|---|---|",
              *[f"| {c} | {v['n']} | {pc(v['accuracy'])} | {pc(v['refusal_rate'])} |"
                for c, v in s["by_category_all"].items()]]
    ab = s.get("abstention")
    if ab:
        L += ["\n## Abstention (refused = positive; cat 5 should refuse, cats 1-4 should answer)",
              f"- precision {pc(ab['precision'])}, recall {pc(ab['recall'])}, F1 {pc(ab['f1'])} "
              f"(tp {ab['tp']}, fp {ab['fp']}, fn {ab['fn']})",
              f"- answers when answerable: {pc(ab['answer_rate_when_answerable'])} "
              f"(false refusal rate {pc(ab['false_refusal_rate'])}, n={ab['n_answerable']})"]
    mu = s.get("memory_used")
    if mu:
        L += [f"\n- memory used (non-empty context): {pc(mu['rate'])} of {mu['n']} queries; "
              + ", ".join(f"{c} {pc(v)}" for c, v in mu["by_category"].items())]
    tr = s.get("triggers")
    if tr:
        L += [f"\n## Trigger fired rates (n={tr['n']} forensics rows)", "| trigger | fired | rate | acc fired | acc not fired |",
              "|---|---|---|---|---|"]
        for name in ("list_mode", "bridge_hop", "rerank"):
            v = tr[name]
            L.append(f"| {name} | {v['fired']} | {pc(v['rate'])} | {pc(v.get('accuracy_fired'))} | "
                     f"{pc(v.get('accuracy_not_fired'))} |")
        L += [f"| leg:{k} | {v['fired']} | {pc(v['rate'])} | | |" for k, v in tr["legs"].items()]
        L += [f"- bridge gate decisions: {tr['bridge_hop']['gate_decisions'] or 'not logged in this run'}",
              f"- decider decisions: {tr['decider']['decisions'] or 'not logged in this run'}"]
    if s.get("sufficiency_on_qa_set"):
        suff = s["sufficiency_on_qa_set"]
        L += [f"- sufficiency on the QA set: {pc(suff['value'])} (n={suff['n']})"]
    if s.get("recall_at_10_hits"):
        r10 = s["recall_at_10_hits"]
        L += [f"- recall@10 (all gold in final top-10 hits): {pc(r10['value'])} (n={r10['n']})"]
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
    ap.add_argument("--errata", default=str(DEFAULT_ERRATA),
                    help="A5: locomo_errata.json (gold_error, needs_image dropped)")
    ap.add_argument("--no-errata", action="store_true")
    build(ap.parse_args())
