"""Build docs/v2/data/plan.json and docs/v2/PLAN.md: the work-package plan with status.

    python docs/v2/data/build_plan.py

Every one of the 155 decisions must be covered by at least one package (checked here). Status values:
Not started, Ready, In progress, Blocked, Needs measurement, Done, Dropped.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATUSES = ("Not started", "Ready", "In progress", "Blocked", "Needs measurement", "Done", "Dropped")

# (id, phase, title, decisions, depends_on, kind, gate, status, note)
# kind: "move" = pure restructure (tests + CPU golden); "behaviour" = needs a GPU screen through the paired gate;
# "measure" = a run or screen; "infra" = tooling.
WP = [
    # Phase 0: settle and prepare
    ("P0-01", 0, "Paired adoption gate (focus, blind, full set)", ["G-01", "G-03"], [], "infra",
     "Replay: identical arm NO CHANGE; missing set INCOMPLETE", "In progress",
     "Built 2026-10-12 (d0ab058, 5bf5643); open: sealed one-shot blind run per adoption"),
    ("P0-02", 0, "Environment pinning per run", ["G-24"], [], "infra",
     "A fresh per-run venv reproduces clean-jina-full", "In progress",
     "Lock fixed (1d15194); per-run freeze recorded (7ce1d55); open: separate venv per pinned run"),
    ("P0-03", 0, "Run manifest and comparability key", ["G-25"], ["P0-02"], "infra",
     "Manifest records commit, env hash, flags, prompts, models, data hashes", "Not started", ""),
    ("P0-04", 0, "CPU golden and import-boundary test (restructure guard)", ["G-16"], [], "infra",
     "Golden identical on two runs at HEAD", "Ready", "S0 of RESTRUCTURE"),
    ("P0-05", 0, "Engine LLM fixes: hard per-call bound, think=False on Ollama", ["P-15"], [], "behaviour",
     "Regression tests; re-screens below", "Done", "2d05ac7, 798c4b5"),
    ("P0-06", 0, "Re-screen engine-LLM arms on the thinking fix (f-gc9, f-gc3, f-win, f-gc2)",
     ["O-07", "O-16", "W-12"], ["P0-05"], "measure", "Paired gate vs f-jref2-i2 on the focus slice",
     "Needs measurement", "GPU stopped by the owner; f-gc9 was net +15 before the fix"),
    ("P0-07", 0, "A03 query contract: full set, then blind sets", ["R-03"], ["P0-01"], "measure",
     "PROMOTE on 1,540 vs clean-jina-full, then CONFIRMS on MAB-CR and ConvoMem", "Needs measurement",
     "Focus +15/-7 net +8 HOLD; full run stopped partway"),
    ("P0-08", 0, "Benchmark suite and behavioural controls", ["G-02", "G-04"], ["P0-01"], "infra",
     "Dev: LoCoMo (+cat 5), OP-Bench; blind: MAB-CR, ConvoMem, PrefEval; behavioural suite blocks adoption",
     "Not started", "LongMemEval stays out"),
    ("P0-09", 0, "Prompt gold-lint over single words, partials and harness prompts", ["P-13", "W-18"], [], "infra",
     "Lint fails on any gold token in any prompt", "In progress", "Single words and harness prompts done (11853ed); open: partials glob, rendered-prompt warnings"),
    # Phase 1: foundations (restructure in place)
    ("F-01", 1, "Graph key identity: kind, occurrence, A-B-A, old-key aliases", ["M-17", "W-20"], ["P0-04"], "behaviour",
     "A-B-A returns to A; same-day events stay two; replay idempotent; GC arm screen", "Ready", "S1; G-W50"),
    ("F-02", 1, "Record envelope: layer field, type tag, tri-temporal times, provenance class, axes",
     ["M-02", "M-03", "M-06", "M-07", "M-08", "M-10", "M-16"], ["P0-04"], "move",
     "Replay byte-stable; upcaster round-trips; golden unchanged", "Ready", "S2; Q1 two layers on"),
    ("F-03", 1, "Derivation fingerprint on derived records and caches", ["P-12", "P-25"], ["F-02"], "move",
     "Cache misses once; outputs identical", "Not started", "S3"),
    ("F-04", 1, "Durable outbox and inline runner, read barrier", ["W-02", "W-10", "W-13", "W-25", "G-19", "P-21"],
     ["F-02"], "behaviour", "Fault-injection tests; golden unchanged with barrier; window arm screen", "Not started",
     "S4; Q2; model calls outside namespace locks"),
    ("F-05", 1, "Package skeleton; move store and trust leaves", ["G-16"], ["P0-04"], "move",
     "Both suites and golden green; no old import paths", "Not started", "S5"),
    ("F-06", 1, "Move read, compose and models leaves", ["G-16"], ["F-05"], "move", "Same as F-05", "Not started", "S6"),
    ("F-07", 1, "Config split and flag registry", ["G-21", "G-22"], ["F-05"], "move",
     "Same YAML keys; every default flag has evidence", "Not started", "S7"),
    ("F-08", 1, "Wiring out of the engine (Services container)", ["G-16"], ["F-06"], "move", "Golden", "Not started", "S8"),
    ("F-09", 1, "Write path out of the engine", ["W-01", "W-09"], ["F-08"], "move", "Golden", "Not started", "S9"),
    ("F-10", 1, "Read path out of the engine", ["R-05"], ["F-08"], "move", "Golden byte-identical hit order", "Not started", "S10"),
    ("F-11", 1, "Compose out of the engine", ["R-20"], ["F-10"], "move", "Golden context hash identical", "Not started", "S11"),
    ("F-12", 1, "Trust, admin and memory-type mechanisms out", ["M-21", "M-24"], ["F-08"], "move", "Golden", "Not started", "S12"),
    ("F-13", 1, "Scoped handle API; engine reduced to lifecycle and admin", ["G-16", "G-17", "G-18"], ["F-09", "F-10", "F-11", "F-12"],
     "move", "Harness on the handle; paired-gate replay NO CHANGE vs f-jref2-i2", "Not started", "S13; no compatibility aliases"),
    ("F-14", 1, "Retire measured-negative and dead code", ["G-22"], ["F-13"], "move", "Golden unchanged; retired flags raise ConfigError",
     "Blocked", "Needs owner on unscreened candidates (event ledger, maxsim, causal walk, bridge hop, prefilter, novelty exclusions, agentic query)"),
    ("F-15", 1, "Split the pipelines module into derive stages", ["O-04"], ["F-08"], "move", "Golden; stage order unchanged", "Not started", "S15 part 1"),
    ("F-16", 1, "Storage profiles, lineage table, Neo4j adapter, migration by replay",
     ["M-01", "M-04", "M-05", "M-09", "M-18", "M-25", "M-26", "G-12", "G-23"], ["F-02", "F-05"], "behaviour",
     "Isolation, erasure, rebuild tests on embedded and server profiles; v1 log replays byte-stably", "Not started", "S16; Q4 Neo4j"),
    # Phase 2: write path
    ("W2-01", 2, "Source envelope intake, receipt, idempotency, re-ingest", ["W-01", "W-02", "W-03", "W-24"], ["F-04", "F-09"], "behaviour",
     "Write p95 at most 250 ms; idempotent retries", "Not started", ""),
    ("W2-02", 2, "Stamping: three clocks, time zone, speaker/addressee/participants, provenance class",
     ["W-04", "W-05", "W-08", "M-08"], ["F-02", "W2-01"], "behaviour", "Focus slice non-inferior", "Not started", ""),
    ("W2-03", 2, "Role policy, echo guard and routing", ["W-06", "W-09"], ["W2-01"], "behaviour", "Focus slice non-inferior", "Not started", ""),
    ("W2-04", 2, "Firewall at three gates; base plus extended signals", ["W-07", "G-06"], ["W2-01"], "behaviour",
     "False-positive screen on the behavioural suite", "Not started", "Q8 chat-preset exemption"),
    ("W2-05", 2, "Segmentation: adaptive window, time-gap cut, token batching", ["W-11"], ["F-04"], "behaviour",
     "Calls per turn down, grounding inside band", "Not started", ""),
    ("W2-06", 2, "Online extraction call: ItemV1, prompt v2, constrained output, repair, dead letters",
     ["W-12", "W-14", "W-15", "W-16", "P-04", "P-05", "P-06", "P-09", "P-11"], ["F-04", "W2-05"], "behaviour",
     "Window arm vs reference; items dropped down; at most 1 call per window", "Not started", "G-W49"),
    ("W2-07", 2, "Prompt layout rules: no examples, stable prefix, same language", ["W-18", "P-07", "P-08", "P-10"], ["P0-09"], "behaviour",
     "English output unchanged on focus slice (per partial)", "Not started", ""),
    ("W2-08", 2, "Grounding: spans per assertion, rule checks, assent", ["W-17"], ["W2-06"], "behaviour",
     "Grounding at least 95% on a labelled cross-domain set", "Not started", ""),
    ("W2-09", 2, "Online entity resolution with pending_merge", ["W-19", "M-15"], ["W2-06", "F-01"], "behaviour",
     "Calibrated thresholds: no homonym merges above a stated rate", "Not started", ""),
    ("W2-10", 2, "Integration ladder and cardinality registry", ["W-21", "W-22", "M-12"], ["F-01", "W2-06"], "behaviour",
     "MAB-CR non-inferior; A-B-A tests", "Not started", "Q7 coexist unless marked"),
    ("W2-11", 2, "Asset describer (ingest and read triggers, off by default)", ["W-23"], ["W2-01"], "behaviour", "Off by default; on-arm screen", "Not started", ""),
    ("W2-12", 2, "Cost ledger and write targets", ["W-26", "P-14", "P-22"], ["F-03"], "infra", "Targets as regression tests", "Not started", ""),
    ("W2-13", 2, "Small local pre-pass (GLiNER2) as hints", ["P-23"], ["W2-06"], "behaviour", "Calls down, grounded facts not down", "Not started", ""),
    # Phase 3: read path
    ("R3-01", 3, "Depth tiers and ReadRequest; evidence unit", ["R-01", "R-02"], ["F-10"], "behaviour", "Fast tier zero model calls; p95 SLO", "Not started", ""),
    ("R3-02", 3, "Typed query contract by rules; render header", ["R-03", "R-20"], ["P0-07"], "behaviour", "A03 full set and blind", "Blocked", "Waits for P0-07"),
    ("R3-03", 3, "Rule router; LLM rewrites only in deep tier", ["R-04"], ["R3-01"], "behaviour", "Focus non-inferior", "Not started", ""),
    ("R3-04", 3, "Core legs: vector, language-aware BM25, temporal on event intervals", ["R-05", "R-06", "R-07"], ["F-10", "F-02"], "behaviour",
     "Focus non-inferior; temporal category up", "Not started", ""),
    ("R3-05", 3, "Routed legs: entity anti-hub, graph one hop, episode lift", ["R-08", "R-09", "R-10"], ["R3-04"], "behaviour",
     "Each leg its own focus and blind screen", "Not started", "Q1: legs run only when their layer exists"),
    ("R3-06", 3, "Family fusion, protected pool, calibrated floors", ["R-11", "R-12", "R-13"], ["R3-04"], "behaviour",
     "Source-family pool non-inferior to per-leg pool", "Not started", ""),
    ("R3-07", 3, "Jina rerank on every read; licence fallback; bounded boosts", ["R-14", "R-15", "R-16", "P-18"], ["R3-06"], "behaviour",
     "Matches the 81.4% reference; boosts off until screened", "Not started", "Q5, Q6"),
    ("R3-08", 3, "Lift derived hits to turns under one score", ["R-17"], ["R3-07"], "behaviour", "Focus non-inferior", "Not started", ""),
    ("R3-09", 3, "Packer early-break fix; block shares", ["R-18"], ["F-11"], "behaviour", "Fix: tests only; shares: equal-budget screen", "Ready", "Early-break fix needs no screen"),
    ("R3-10", 3, "Neighbour windows after rerank", ["R-19"], ["R3-07"], "behaviour", "Focus non-inferior", "Not started", ""),
    ("R3-11", 3, "Store-calibrated relevance gate with named bypass", ["R-21"], ["R3-07"], "behaviour",
     "OP-Bench up; no recall loss on MAB-CR, ConvoMem, LoCoMo", "Not started", ""),
    ("R3-12", 3, "Deep tier: one sufficiency check and one rewrite", ["R-22"], ["R3-03"], "behaviour",
     "f-suff: net beyond band, no category loss, at most 2x wall time", "Not started", ""),
    ("R3-13", 3, "External public knowledge (opt-in, separate block)", ["R-23"], ["R3-01"], "behaviour", "Separate arm; never mixed in headline", "Not started", ""),
    ("R3-14", 3, "Time view and two-axis as-of reads", ["R-24", "M-11"], ["F-02", "R3-04"], "behaviour", "Temporal and MAB-CR non-inferior", "Not started", ""),
    ("R3-15", 3, "Parallel legs, latency SLOs, caching, prefix layout", ["R-25", "R-26", "P-20", "P-24"], ["R3-06"], "infra",
     "p95 SLO tests in CI", "Not started", ""),
    # Phase 4: offline memory
    ("O4-01", 4, "Scheduler, triggers, namespace jobs, leases, watermarks, budget, freshness",
     ["O-01", "O-02", "O-03", "O-05", "O-06"], ["F-04", "F-15"], "behaviour", "Restart and lock fault tests; evals deterministic (off by default)",
     "Not started", ""),
    ("O4-02", 4, "Call 2 at session close: summary, stitched episode, profile ops", ["O-07", "O-08", "O-09", "M-14"], ["O4-01"], "behaviour",
     "G-C9-style screen", "Not started", "f-gc9 summaries +15 before the thinking fix"),
    ("O4-03", 4, "Reconcile backlog, staged entity resolution, contradictions", ["O-10", "O-11", "O-12"], ["O4-01", "W2-09"], "behaviour",
     "Contradiction-recall set; MAB-CR non-inferior", "Not started", ""),
    ("O4-04", 4, "Near-duplicate merge and value histories", ["O-13", "O-14", "M-13"], ["O4-03"], "behaviour",
     "Zero wrong numeric merges; MAB-CR token check", "Not started", ""),
    ("O4-05", 4, "Pattern observations (opt-in L4)", ["O-15", "M-13"], ["O4-04"], "behaviour",
     "OP-Bench, PrefEval, MAB-CR non-inferior; sleep calls within budget", "Not started", "Q3"),
    ("O4-06", 4, "Entity summaries and communities (opt-in)", ["O-16", "O-17", "M-19"], ["O4-03"], "behaviour", "Global-question workload", "Not started", ""),
    ("O4-07", 4, "Profile card, guarded operations, reflection", ["O-18", "O-19", "O-20", "M-20"], ["O4-02"], "behaviour",
     "OP-Bench and PrefEval up; hand audit at least 90%", "Not started", ""),
    ("O4-08", 4, "Standing answers (opt-in)", ["O-21"], ["O4-04"], "behaviour", "Refresh on watermark; cited sources", "Not started", ""),
    ("O4-09", 4, "Decay tiers, compression, multi-resolution text", ["O-22", "M-22", "P-16"], ["O4-01"], "behaviour",
     "Context tokens down at no QA loss", "Not started", ""),
    ("O4-10", 4, "Prospective and procedural memory", ["O-23", "O-24"], ["O4-01"], "behaviour", "Advisory only; off by default", "Not started", ""),
    ("O4-11", 4, "Explicit feedback only; observer views (multi-agent)", ["O-25", "O-26", "M-24"], ["O4-03"], "behaviour", "Multi-agent template tests", "Not started", ""),
    # Cross-cutting models
    ("MD-01", 2, "Model tiers, placement, no-model fallbacks, device plan, thinking per role",
     ["P-01", "P-02", "P-03", "P-15", "P-19"], ["F-06"], "behaviour", "Worker-tier screen per role", "Not started", ""),
    ("MD-02", 2, "Default embedders (CPU multilingual, GPU profile)", ["P-17", "M-23"], ["F-06"], "behaviour",
     "English R@k within band; non-English probe", "Not started", ""),
    # Phase 5: trust, API, operations
    ("T5-01", 5, "Trust horizon on grants; lineage always recorded", ["G-07", "G-08"], ["F-16"], "behaviour", "Read cost of the bound; N1 tests", "Not started", ""),
    ("T5-02", 5, "Low-trust rendering in context", ["G-09"], ["F-11"], "behaviour", "Count wrapped blocks; QA non-inferior", "Not started", ""),
    ("T5-03", 5, "Privacy tiers, consent and purpose scopes", ["G-10", "G-11"], ["F-12"], "behaviour", "Remote-tier gate tests", "Not started", ""),
    ("T5-04", 5, "Erasure index across caches and projections", ["G-13", "M-25"], ["F-16"], "behaviour", "Forget probes on both profiles", "Not started", ""),
    ("T5-05", 5, "Retention and audit per profile", ["G-14", "G-15"], ["F-12"], "infra", "Profile tests", "Not started", ""),
    ("T5-06", 5, "Observability: per-stage ledger, optional OTel, no content in spans", ["G-20"], ["W2-12"], "infra", "Tests", "Not started", ""),
    # Phase 6: release and paper
    ("R6-01", 6, "Freeze configuration; sealed blind runs; loop metrics", ["G-05", "G-26"], ["R3-07", "O4-07"], "measure",
     "One sealed blind run per set; loop metrics on scripted episodes", "Not started", ""),
    ("R6-02", 6, "Release memspine 2.0 with notes of removed APIs", ["G-23"], ["R6-01"], "infra", "Release checklist", "Not started", "No compatibility layer"),
]


#: Memory type -> layer of the decided stack (Q1): L0 raw, L1 facts, L2 episodes and summaries, L3 graph,
#: L4 observations and profile, L5 standing answers, procedures and plans. Working memory is context (C), shown as "ctx".
TYPE_LAYER = {"episodic": "L0", "resource": "L0", "semantic": "L1", "associative": "L3", "reflective": "L4",
              "procedural": "L5", "prospective": "L5", "working": "ctx", "shared": "scope"}
OP_ORDER = ["D", "K_C", "K_P", "R", "C", "G"]


def _layers(types: set[str]) -> list[str]:
    out = {TYPE_LAYER[t] for t in types if t in TYPE_LAYER}
    return sorted(out, key=lambda x: (x[0] != "L", x))


def _lane(wid: str, kind: str, files: list[str]) -> str:
    """Agent lane: packages in the same serial lane run one at a time; different lanes may run in parallel."""
    if wid.startswith("F-") and wid not in ("F-01", "F-02", "F-03", "F-04", "F-16"):
        return "serial: engine split"
    if any(f.endswith("engine.py") for f in files) and wid[0] in "FWRO":
        return "serial: engine split" if wid.startswith("F-") else f"after F-13 ({wid.split('-')[0]})"
    return {"P0": "infra", "F": "foundations", "W2": "write", "R3": "read", "O4": "offline", "MD": "models",
            "T5": "trust", "R6": "release"}.get(wid.split("-")[0], "other")


def main() -> None:
    answered = json.loads((HERE / "decisions_answered.json").read_text(encoding="utf-8"))
    titles = {q["id"]: q["title"] for q in answered}
    verify = {}
    for p in sorted(HERE.glob("verify_*.json")):
        for v in json.loads(p.read_text(encoding="utf-8")):
            verify[v["id"]] = v
    rows = []
    for (wid, phase, title, dec, dep, kind, gate, status, note) in WP:
        assert status in STATUSES, (wid, status)
        for d in dec:
            assert d in titles, (wid, d)
        types = {t for d in dec for t in verify.get(d, {}).get("memory_types", [])}
        ops = {o for d in dec for o in verify.get(d, {}).get("operators", [])}
        locs = [loc.split(":")[0] for d in dec for loc in verify.get(d, {}).get("v1_locations", [])]
        files = sorted(set(locs), key=lambda f: (-locs.count(f), f))[:4]
        rows.append({"id": wid, "phase": phase, "title": title, "decisions": dec, "depends_on": dep, "kind": kind,
                     "gate": gate, "status": status, "note": note, "layers": _layers(types),
                     "operators": [o for o in OP_ORDER if o in ops], "files": files,
                     "lane": _lane(wid, kind, files), "agent": "sonnet",
                     "components": sorted({verify[d]["v2_component"] for d in dec if d in verify})[:4]})
    ids = {r["id"] for r in rows}
    for r in rows:
        for d in r["depends_on"]:
            assert d in ids, (r["id"], d)
    covered = {d for r in rows for d in r["decisions"]}
    missing = sorted(set(titles) - covered)
    assert not missing, f"decisions without a work package: {missing}"
    (HERE / "plan.json").write_text(json.dumps({"statuses": STATUSES, "packages": rows}, indent=1, ensure_ascii=False) + "\n",
                                    encoding="utf-8")
    phases = {0: "Phase 0: settle and prepare", 1: "Phase 1: foundations (restructure in place)", 2: "Phase 2: write path and models",
              3: "Phase 3: read path", 4: "Phase 4: offline memory", 5: "Phase 5: trust, API, operations", 6: "Phase 6: release and paper"}
    from collections import Counter
    c = Counter(r["status"] for r in rows)
    out = ["# MemSpine restructure plan: work packages and status", "",
           "Generated from `data/build_plan.py` (the source of truth for rows); status is also kept on the live tracker page and synced back here.",
           f"{len(rows)} packages cover all {len(titles)} decisions. Kinds: **move** = pure restructure (tests and CPU golden), **behaviour** = "
           "needs a GPU screen through the paired gate, **measure** = a run, **infra** = tooling.", "",
           "Layers follow the decided stack (Q1): L0 raw, L1 facts, L2 episodes and summaries, L3 graph, L4 observations and "
           "profile, L5 standing answers, procedures and plans; ctx = working context. Ops are the loop operators (D deposit, "
           "K_C consolidate, K_P personalise, R retrieve, C compose, G generate). Each package is one task for one agent "
           "(Sonnet); packages in a `serial` lane run one at a time, different lanes may run in parallel once their "
           "dependencies are Done.", "",
           "| Status | Count |", "|---|---|"] + [f"| {s} | {c.get(s, 0)} |" for s in STATUSES]
    for ph in sorted(phases):
        out += ["", f"## {phases[ph]}", "",
                "| Id | Work package | Layers | Ops | Decisions | Depends on | Kind | Agent lane | Gate | Status | Note |",
                "|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in rows:
            if r["phase"] == ph:
                out.append(f"| {r['id']} | {r['title']} | {' '.join(r['layers']) or '-'} | {' '.join(r['operators']) or '-'} | "
                           f"{', '.join(r['decisions'])} | {', '.join(r['depends_on']) or '-'} | {r['kind']} | {r['lane']} | "
                           f"{r['gate']} | **{r['status']}** | {r['note']} |")
    (HERE.parent / "PLAN.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"{len(rows)} packages, all {len(titles)} decisions covered; {dict(c)}")


if __name__ == "__main__":
    main()
