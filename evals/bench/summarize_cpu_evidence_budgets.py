"""Validate and report the bounded CPU ranking/evidence experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", type=Path)
    a = ap.parse_args()
    models = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(a.runs.glob("*.json"))]
    assert len(models) == 6
    hashes = {r["fixture_sha256"] for r in models}
    assert len(hashes) == 1
    fit_hashes = {r["fit_units_source_sha256"] for r in models}
    assert len(fit_hashes) == 1
    summary = []
    retention = []
    for model in models:
        assert model["status"] == "ok" and len(model["rows"]) == 48
        assert model["truncated_pairs"] == 0
        if "parameters" in model:
            assert model["parameters"] < 250_000_000
        assert {(r["case"], r["length"]) for r in model["rows"]} == {
            (i, n) for i in range(16) for n in (64, 128, 256)
        }
        for row in model["rows"]:
            assert len(set(row["ranking"])) == 48 and set(row["required"]) <= set(row["ranking"])
            assert row["all_required_top12"] == (set(row["required"]) <= set(row["ranking"][:12]))
            assert len(row["packs"]) == 6
            for packed in row["packs"]:
                assert (
                    0 <= packed["used_tokens"] <= packed["budget"] and packed["kept_bundles"] <= 12
                )
                assert packed["complete"] == (set(row["required"]) <= set(packed["selected_ids"]))
                assert packed["retained"] == len(set(row["required"]) & set(packed["selected_ids"]))
                if packed["mode"] != "flat_full":
                    assert packed["partial_dependency_records"] == 0
                assert 0 < packed["p50_ms"] <= packed["p95_ms"] < math.inf
        for length in (64, 128, 256):
            rows = [r for r in model["rows"] if r["length"] == length]
            times = sorted(t for r in rows for t in r["rank_samples_ms"])
            summary.append(
                {
                    "model": model["model"],
                    "length": length,
                    "p50_ms": statistics.median(times),
                    "p95_ms": times[math.ceil(0.95 * len(times)) - 1],
                    "top1": sum(r["relevant_top1"] for r in rows),
                    "all_required_top12": sum(r["all_required_top12"] for r in rows),
                    "mrr_first_relevant": statistics.mean(
                        r["first_relevant_reciprocal_rank"] for r in rows
                    ),
                    "sampled_peak_rss_mib": model["sampled_peak_rss_mib"],
                    "index_setup_mean_ms": statistics.mean(r["index_setup_ms"] for r in rows),
                    "doc_token_min": min(r["doc_token_min"] for r in rows),
                    "doc_token_max": max(r["doc_token_max"] for r in rows),
                }
            )
            for budget in (2048, 3072):
                for mode in ("flat_full", "linked_full", "linked_annotated_spans"):
                    packs = [
                        p
                        for r in rows
                        for p in r["packs"]
                        if p["budget"] == budget and p["mode"] == mode
                    ]
                    retention.append(
                        {
                            "model": model["model"],
                            "length": length,
                            "budget": budget,
                            "mode": mode,
                            "complete": sum(p["complete"] for p in packs),
                            "multi_complete": sum(
                                p["complete"]
                                for r in rows
                                if r["multi"]
                                for p in r["packs"]
                                if p["budget"] == budget and p["mode"] == mode
                            ),
                            "dependency_missing_records": sum(
                                p["partial_dependency_records"] for p in packs
                            ),
                            "required_span_retention": sum(p["retained"] for p in packs)
                            / sum(p["required"] for p in packs),
                            "used_mean_tokens": statistics.mean(p["used_tokens"] for p in packs),
                            "packing_median_of_p50_ms": statistics.median(
                                p["p50_ms"] for p in packs
                            ),
                        }
                    )
    root = Path(__file__).resolve().parents[2]
    artifact = {
        "date": "2026-10-18",
        "cpu": "AMD Ryzen 9 9950X3D; four CPU threads per sequential process",
        "budgets": [2048, 3072],
        "batch_size": 16,
        "candidate_count": 48,
        "ranked_root_limit": 12,
        "fixture_sha256": next(iter(hashes)),
        "fit_units_source_sha256": next(iter(fit_hashes)),
        "script_sha256": hashlib.sha256(
            Path(__file__).with_name("cpu_evidence_budget_kernels.py").read_bytes()
        ).hexdigest(),
        "summary": summary,
        "retention": retention,
        "raw_models": models,
        "validation": "Six models, 48 cases/model, identical fixtures, no truncated pairs, exact 48-ID pools, budget ceilings, top12/retention recomputation, linked dependency completeness passed.",
    }
    (root / "docs/schema_review/cpu_evidence_budget_results.json").write_text(
        json.dumps(artifact, indent=2) + "\n", encoding="utf-8"
    )
    tiny = next(s for s in summary if "TinyBERT" in s["model"] and s["length"] == 256)
    mini = next(
        s
        for s in summary
        if s["model"] == "cross-encoder/ms-marco-MiniLM-L-6-v2" and s["length"] == 256
    )
    lines = [
        "# CPU ranking and 2K/3K evidence-budget experiment",
        "",
        "Measured 2026-10-18. **Synthetic, length-controlled kernel probes; not production, LoCoMo accuracy or a best-in-class leaderboard.** No generative calls, service startup or engine/default changes.",
        "",
        "## Protocol and scope",
        "",
        "- Ryzen 9 9950X3D, Windows, Python 3.13.5, four threads per sequential isolated process. Cached pinned weights only; CPU float32 for contextual/cross encoders. No downloads, training or ONNX/int8 execution in this follow-up.",
        "- Six methods, 16 queries at each of 64/128/256 target BERT-token message lengths: 48 conditions per method. Eight earlier single-support cases plus eight multi-support cases (identity, relative time, negation, correction, duration, intersection, event count and gift ownership).",
        "- Each pool has 48 candidates and includes all annotated required sources. Distractors include near-topic actor/modality mismatches. Neutral conversational padding alternates before/after the important sentence. Actual token min/max is recorded. These are approximations of realistic lengths, not sampled real conversations.",
        "- One complete warmup per condition, three timed ranking repeats; quantiles pool the 48 samples at each length. Quality is over 16 unique conditions, not 48 independent samples. Cross-encoders score 48 query/message pairs in three batches of 16. Cached similarity times query encoding plus dot products; document embedding setup is recorded separately.",
        "- All cross-encoder inputs fit the 512-token cap; zero truncated pairs is validated. Ranking p50 excludes indexing, packing and model loading. Packing includes exact counting and is separately replayed 30 times per condition.",
        "- 2K means 2,048 and 3K means 3,072 **evidence-only** tokens, counted with the same pinned TinyBERT/BERT tokenizer, including rendered source/session/time headers. This is not a universal or generative-reader token count and does not include answer/system-prompt reserves.",
        "- Packing uses the actual pure fit_units loop from src/memspine/read/tiers.py, loaded by AST to avoid service imports. Its unit_tokens dependency is supplied fixture-computed exact rendered costs. The full schema, assembly routing and access-control path are not exercised. Top12 ranked roots enter the comparison.",
        "- Flat/full treats messages independently. Linked/full closes pre-supplied dependency links and keeps each bundle whole. Linked/annotated-spans additionally uses manually annotated exact source sentences: an **optimistic trimming bound**, not an implemented automatic selector. Links are supplied fixture metadata, not inferred by the benchmark.",
        "- Each bundle pays for all copied support excerpts; no free cross-bundle dedup. The fitter skips oversize units and can accept later smaller units. Missing evidence is recorded as incomplete by the harness; runtime readiness/abstention is not tested.",
        "- RSS includes Python, Torch and tokenizer imports even for BM25/static workers in this common harness. It is sampled at 10ms, not guaranteed OS peak or model-only RAM. Do not compare it directly to the earlier lighter Model2Vec process.",
        "",
        "## Speed and ranking quality",
        "",
        "Top1 means the first item is one of the required sources; it is not answer correctness. All@12 requires every annotated source in the first twelve.",
        "",
        "| Method | Target tokens | Rank48 p50 ms | p95 ms | Top1 /16 | All@12 /16 | MRR first source | Index setup mean ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for s in summary:
        lines.append(
            f"| {s['model']} | {s['length']} | {s['p50_ms']:.3f} | {s['p95_ms']:.3f} | {s['top1']} | {s['all_required_top12']} | {s['mrr_first_relevant']:.3f} | {s['index_setup_mean_ms']:.2f} |"
        )
    lines += [
        "",
        "## Complete evidence at the longest messages",
        "",
        "Each result is complete cases out of 16 (eight require multiple sources). Partial means retained records whose declared dependencies are absent; zero partial does not imply all requested evidence was retrieved.",
        "",
        "| Method | Budget | Flat/full complete | Linked/full complete | Annotated-span bound | Flat partial records | Linked/full packing p50 ms* |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for model in models:
        for budget in (2048, 3072):
            rr = {
                r["mode"]: r
                for r in retention
                if r["model"] == model["model"] and r["length"] == 256 and r["budget"] == budget
            }
            lines.append(
                f"| {model['model']} | {budget} | {rr['flat_full']['complete']} | {rr['linked_full']['complete']} | {rr['linked_annotated_spans']['complete']} | {rr['flat_full']['dependency_missing_records']} | {rr['linked_full']['packing_median_of_p50_ms']:.3f} |"
            )
    lines += [
        "",
        "*Median across per-condition packing medians, including source rendering/token counting; ranking is excluded. All lengths, span-level retention, multi-support counts, tokens used and memory appear in the portable JSON.",
        "",
        "## Interpretation and next steps",
        "",
        f"At 256-token messages, TinyBERT ranks 48 candidates in {tiny['p50_ms']:.1f} ms median ({tiny['p95_ms']:.1f} ms p95); MiniLM cross-encoder takes {mini['p50_ms']:,.1f} ms ({mini['p95_ms']:,.1f} ms p95). Their linked/full completeness is 15/16 versus 14/16 at 2K, and both 16/16 at 3K. These are input-specific observations, not stable general quality estimates.",
        "",
        "**Recommended comparison profile: 3K for full-message bundles; 2K only with explicit incomplete handling or a separately validated compact-evidence selector.** The earlier accepted 4K design remains unchanged. BM25 and both static models also reach 16/16 linked/full completeness at 3K on these lexically simple supplied candidate pools, so this fixture does not justify mandatory neural reranking.",
        "",
        "The earlier short-text suggestion that MiniLM is the stronger reranker is now qualified: its extra latency does not buy extra 3K completeness here. Model2Vec32M gives better first-source ordering than 8M at the longest length (11/16 versus 6/16), but the same 3K completeness. Retain 8M as the lower-memory candidate profile and consider 32M only after incremental hybrid benefit is measured.",
        "",
        "In the remaining TinyBERT 2K linked/full failure, the four-source recorded-hike count cannot fit after earlier ranked units consume budget. The harness marks it incomplete; this does not mean no supporting sources exist. An obligation-aware selector or read-time deduplication of repeated source spans may help, but neither optimisation was measured or implemented here.",
        "",
        "Recommendations must account for both ranking and completeness; neither a larger budget nor a cross-encoder repairs missing identity/time links. The annotated-span result is a ceiling conditional on known correct links and spans. It cannot justify promoting an untested trimmer.",
        "",
        "- Keep lexical/BM25 as an inexpensive candidate lane; this small fixed-pool reference implementation is not a lexical-provider throughput benchmark.",
        "- Preserve source-bound dependency bundles and explicit incomplete status at either budget. Do not persist or answer from isolated high-scoring fragments as if their support were present.",
        "- Treat 2K/3K as configurable comparison profiles, not a replacement of the previously accepted 4K target. Choose the smallest budget meeting independently measured coverage; do not silently drop dependencies to meet a cap.",
        "- Cache immutable excerpt token costs by tokenizer revision and text/content hash to reduce repeated counting; scope access checks and invalidation independently. This optimisation was not implemented here.",
        "- Next: independent naturally written longer messages, automatic dependency/span construction with measured precision, a reader-specific tokenizer/reserve, inaccessible/deleted-source handling, and ONNX/int8 quality parity. No full suite is needed for those kernel follow-ups.",
        "",
        "Fixture limitations include only 16 query templates, repeated neutral padding/distractors, fixed source-ID tie breaking and guaranteed inclusion of all required evidence in each pool. Scores were not calibrated for abstention, and no first-stage retrieval recall, model disagreement policy, relation extraction or final answer quality is measured. Generalisation needs independently authored cases.",
        "",
        "## Artifacts and reproduction",
        "",
        "- [Portable results and per-condition outputs](schema_review/cpu_evidence_budget_results.json)",
        "- [Focused replay validation](schema_review/cpu_evidence_budget_validation.json): every packing outcome reproduced, exact source spans and message lengths checked, fitter boundary/duplicate cases checked.",
        "- [Kernel script](../evals/bench/cpu_evidence_budget_kernels.py) and [validation/report builder](../evals/bench/summarize_cpu_evidence_budgets.py).",
        "- Raw model logs and incremental JSON: evals/runs/_analysis/cpu_evidence_budgets_2026-10-18/.",
        "",
        "```powershell",
        "$py = 'C:/Users/hembad/AppData/Local/Temp/memspine-ner-runtime-313/Scripts/python.exe'",
        "& $py evals/bench/cpu_evidence_budget_kernels.py --python $py `",
        "  --deps 'C:/Users/hembad/AppData/Local/Temp/memspine-cpu-kernel-review-deps' `",
        "  --out 'evals/runs/_analysis/cpu_evidence_budgets_recheck'",
        "& $py evals/bench/summarize_cpu_evidence_budgets.py evals/runs/_analysis/cpu_evidence_budgets_recheck",
        "```",
        "",
        artifact["validation"],
    ]
    (root / "docs/CPU_EVIDENCE_BUDGET_BENCHMARK.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": "passed",
                "models": len(models),
                "conditions_per_model": 48,
                "packing_outcomes": len(models) * 48 * 6,
            }
        )
    )


if __name__ == "__main__":
    main()
