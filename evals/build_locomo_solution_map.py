# ruff: noqa: E501
"""Generator of ``analysis/locomo_solution_map.json``: root-cause class -> solution -> setting keys -> screens.

    python evals/build_locomo_solution_map.py [--out evals/analysis/locomo_solution_map.json] [--check]

CPU only, no model call. Text-free by construction: ids, setting keys, package ids, screen ids, doc section names.
The screen catalogue (id, layer, title, setting, status) is read from ``docs/v2/SCREEN_ATLAS.html`` (the JS array
``S``) and ``evals/v2_queue.tsv`` (batch, arm, sets); the class registry comes from ``locomo_problem_classes``.

Solution ``status`` (one of ``STATUSES``):

* ``in_reference``       built and ON in the clean-jina-full reference config (it cannot recover what it already recovered;
                         what is still wrong is the residual it does not fix)
* ``built_off_default``  built in the engine (``src/memspine/config/schema``), off by default and in the reference
* ``built_harness_flag`` built as an eval-harness flag or reader prompt variant (``evals/memspine_evals``), not an engine setting
* ``not_built``          proposed in a forensic doc, no code (a gap)

``screens`` lists the v2 catalogue ids that exercise the solution (alone or inside a bundle). An empty list on a built
solution means *built but no screen is queued for it* (a testing gap, reported as such).

``relation`` of a solution to a class: ``primary`` = the solution is aimed at this class, ``secondary`` = it may touch it.
Every mapping cites the doc section it comes from in ``doc_refs``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != HERE]
sys.path.append(str(HERE))

from locomo_problem_classes import CLASSES  # noqa: E402

STATUSES = ("in_reference", "built_off_default", "built_harness_flag", "not_built")
RELATIONS = ("primary", "secondary")
SCHEMA = "locomo_solution_map/v1"

R = "evals/analysis/"
D_RECALL = R + "RECALL_GAPS_FORENSIC.md"
D_READER = R + "READER_GAPS_FORENSIC.md"
D_FULL = R + "FULL_RUN_FORENSICS_2026-10-10.md"
D_B1 = R + "B1_LIST_QUESTIONS_DESIGN.md"
D_GAP = R + "GAP_REGISTER.md"
D_OPEN = R + "READER_OPEN_GAPS_2026-10-10.md"
D_ATLAS = "docs/v2/SCREEN_ATLAS.html"
D_PLAN = "docs/v2/PLAN.md"


def ref(file: str, section: str) -> dict:
    return {"file": file, "section": section}


def sol(
    sid: str,
    name: str,
    status: str,
    keys: list[str],
    packages: list[str],
    screens: list[str],
    refs: list[dict],
    classes: dict[str, str],
    note: str = "",
) -> dict:
    return {
        "id": sid,
        "name": name,
        "status": status,
        "keys": keys,
        "packages": packages,
        "screens": screens,
        "doc_refs": refs,
        "classes": classes,
        "note": note,
    }


P, S2 = "primary", "secondary"

SOLUTIONS: list[dict] = [
    # ------------------------------------------------------------------ fusion / pool / rerank
    sol(
        "SOL-POOLPROT",
        "Reserve pool slots for the top of every leg (leg-protected pool)",
        "in_reference",
        ["read.pool_protect_per_leg (=3 in clean-jina-full)", "read.pool_protect_mode: per_leg|source_family"],
        ["P0-02", "R3-06"],
        ["S05", "S09"],
        [ref(D_FULL, "s5 lever 1; s2.3(c)"), ref(D_GAP, "I75; R02")],
        {"fusion_loss_vector_only": P, "fusion_loss_lexical_only": P, "fusion_loss_two_leg_crowded": P},
        "per_leg=3 is on in the reference; source_family mode (R02) is built, unscreened. S05 is a parity run, not a gain test.",
    ),
    sol(
        "SOL-FAMFUS",
        "Family fusion (dense / lexical / structured / derived families vote separately)",
        "built_off_default",
        ["read.family_fusion"],
        ["R3-06"],
        ["S14", "S51", "S60", "S61"],
        [ref(D_ATLAS, "S14, S51"), ref(D_RECALL, "s3 (RRF loses items a leg ranked high)")],
        {"fusion_loss_vector_only": P, "fusion_loss_lexical_only": P, "fusion_loss_two_leg_crowded": P, "lexical_outranked_recall": S2},
        "S14 sample +4/-1; S51 is the 437-question focus confirmation.",
    ),
    sol(
        "SOL-RRPOOL",
        "Rerank a wider search pool (rerank_v2 + rerank_v2_search_pool)",
        "built_off_default",
        ["read.rerank_v2", "read.rerank_v2_search_pool=30", "read.rerank_keep"],
        ["R3-07", "FU-RRPOOL"],
        ["S16", "S67", "S60", "S61"],
        [ref(D_RECALL, "s8 H2 (rerank a pool of 30)"), ref(D_FULL, "s5 lever 4")],
        {"rerank_keep_cut": P, "fusion_loss_vector_only": S2, "fusion_loss_two_leg_crowded": S2, "list_aggregation_generic": S2, "lexical_outranked_recall": S2},
    ),
    sol(
        "SOL-RERANKKEEP",
        "Raise rerank_keep (10 -> 15)",
        "built_off_default",
        ["read.rerank_keep=15"],
        [],
        [],
        [ref(D_FULL, "s2.3(c): rerank_keep 15 would have kept all five cut golds")],
        {"rerank_keep_cut": P},
        "Config only; no v2 screen queued for it.",
    ),
    sol(
        "SOL-POOL3",
        "Larger candidate pool with a tiered window",
        "built_off_default",
        ["read.candidate_pool=3 (reference: 2)"],
        [],
        [],
        [ref(D_RECALL, "s8 H1"), ref(D_FULL, "s5 lever 4")],
        {"list_aggregation_generic": P, "fusion_loss_vector_only": S2, "fusion_loss_two_leg_crowded": S2, "implicit_or_paraphrased_gold": S2},
        "Window-by-rank is new code; the pool key exists. No v2 screen queued.",
    ),
    sol(
        "SOL-LIFT",
        "Lift derived hits to source turns before rerank",
        "built_off_default",
        ["read.lift_before_rerank"],
        ["R3-08"],
        ["S15", "S60", "S61"],
        [ref(D_ATLAS, "S15")],
        {"rerank_keep_cut": S2, "implicit_or_paraphrased_gold": S2},
        "Only matters once derived (L2/L3) records exist; inert on a J run.",
    ),
    sol(
        "SOL-LEGS-V2",
        "v2 leg registry, rule router, depth tiers",
        "built_off_default",
        ["read.legs_v2", "read.router_rules", "read.tiers_v2"],
        ["R3-04", "R3-03", "R3-01"],
        ["S10", "S12", "S36", "S37", "S38", "S60"],
        [ref(D_ATLAS, "S10, S12, S36-S38")],
        {"lexical_outranked_recall": S2, "temporal_evidence_missing": S2},
        "Mostly a safety / parity check (new path must not lose); little targeted gain expected.",
    ),
    sol(
        "SOL-LEXICAL",
        "Lexical index options (english analyzer, date words in BM25)",
        "built_off_default",
        ["read.lexical_analyzer=english", "read.lexical_dates"],
        [],
        [],
        [ref(D_RECALL, "s8 M1, T2, O2")],
        {"lexical_outranked_recall": P, "temporal_evidence_missing": S2},
        "RECALL_GAPS_FORENSIC measured +13 net (english) and +10 net (lexical_dates) before the reranker; churn.",
    ),
    # ------------------------------------------------------------------ recall: multi-hop, implicit, list
    sol(
        "SOL-GRAPH",
        "Graph extraction in sleep plus one-hop graph / entity legs",
        "built_off_default",
        ["read.graph_leg_v2", "read.entity_leg_v2", "graph.edge_compiler_v2", "graph.key_v2", "consolidation (gc3 arm)"],
        ["F-02c", "R3-05a", "R3-05b"],
        ["S02", "S13", "S28", "S30", "S32", "S39", "S61"],
        [ref(D_ATLAS, "S02, S30, S32"), ref(D_RECALL, "s4 (multi-hop evidence structure)")],
        {"multi_evidence_recall": P, "implicit_or_paraphrased_gold": S2, "list_aggregation_generic": S2, "vocabulary_mismatch_recall": S2},
        "S02 sample +5/-1 (the base for every sleep screen); S30 +6/-5 within noise.",
    ),
    sol(
        "SOL-EPISODE",
        "Session summaries / episodes lifted to turns (L2)",
        "built_off_default",
        ["offline.session_close_v2", "read.episode_leg_v2", "sleep summary arm (gc9)"],
        ["O4-02", "R3-05c"],
        ["S01", "S31", "S33", "S61", "S64"],
        [ref(D_ATLAS, "S01, S31, S33"), ref(D_RECALL, "s8 N (implicit answer)")],
        {"implicit_or_paraphrased_gold": P, "multi_evidence_recall": S2, "vocabulary_mismatch_recall": S2, "temporal_evidence_missing": S2},
        "S01 sample +3/-1.",
    ),
    sol(
        "SOL-WINDOWFACTS",
        "Write-time fact extraction (window_extract, write-v2 stack incl. temporal stage W2-14)",
        "built_off_default",
        ["write.window_extract=facts", "write.envelope_v2", "write.temporal_v2", "write.stamping_v2", "write.segmentation_v2"],
        ["W2-06a", "W2-14", "W2-05", "W2-02"],
        ["S03", "S04", "S43", "S44", "S45", "S48", "S62"],
        [ref(D_ATLAS, "S03, S44, S48, S62"), ref(D_PLAN, "W2-14")],
        {"implicit_or_paraphrased_gold": S2, "list_aggregation_generic": S2, "temporal_evidence_missing": S2, "date_offset_small": S2},
        "S03 sample +3/-7 and S04 +1/-7: single window-facts screens lose; the W2 stack is retested as the S62 bundle.",
    ),
    sol(
        "SOL-PLANNER",
        "LLM planner / query rewrites / HyDE-style answer hypothesis",
        "built_off_default",
        ["read.planner=llm", "read.planner_version", "read.compose_rewrites"],
        [],
        [],
        [ref(D_RECALL, "s8 H3, N3, I2"), ref(D_B1, "s2 (H3 planner)")],
        {"list_aggregation_generic": P, "multi_evidence_recall": P, "vocabulary_mismatch_recall": P, "implicit_or_paraphrased_gold": S2, "open_domain_inference_retrieval": S2},
        "Planner and rewrites exist; true HyDE (answer hypothesis as a leg) is not built. No v2 screen queued.",
    ),
    sol(
        "SOL-AGENTIC",
        "Bounded second-pass retrieval for missing evidence (agentic query/slot, second_round, fact chain)",
        "built_off_default",
        ["read.agentic_mode=query|slot", "read.second_round", "read.fact_chain", "read.event_ledger"],
        [],
        [],
        [ref(D_GAP, "R01; E01; E02"), ref(D_FULL, "s5 lever 3")],
        {"multi_evidence_recall": P, "list_aggregation_generic": P, "open_domain_inference_retrieval": S2},
        "Built 2026-10-11 as opt-in, unscreened; none of its keys has a v2 screen id.",
    ),
    sol(
        "SOL-DEEPREAD",
        "Deep read: one sufficiency check and at most one targeted re-search",
        "built_off_default",
        ["read.deep_v2", "llm.roles.sufficiency"],
        ["R3-12"],
        ["S55", "S63"],
        [ref(D_ATLAS, "S55"), ref(D_FULL, "s5 lever 3")],
        {"multi_evidence_recall": P, "open_domain_inference_retrieval": S2, "inference_open_domain": S2, "refusal_premise_denial": S2},
        "Needs the bound sufficiency role (S55 reports extra calls per read).",
    ),
    sol(
        "SOL-LISTMODE",
        "List mode: speaker-vote leg and wider pool for list-shaped questions",
        "in_reference",
        ["read.list_mode (on in clean-jina-full)"],
        [],
        [],
        [ref(D_B1, "s1, s2"), ref(D_FULL, "s0 headlines (list_mode fired 230 q)")],
        {"list_aggregation_generic": P, "list_incomplete": S2},
        "On in the reference; it fired on 14.9% of questions at 78.7% against 80.7% when not fired. The residual list failures are what it does not fix.",
    ),
    sol(
        "SOL-CONTRACT",
        "Typed query contract (list / when / count) shapes evidence and answer format",
        "built_off_default",
        ["read.query_contract=heuristic", "read.contract_rules"],
        ["P0-07", "R3-02"],
        ["S06", "S11", "S60"],
        [ref(D_GAP, "A03"), ref(D_ATLAS, "S06, S11")],
        {"list_incomplete": P, "count_wrong": P, "list_aggregation_generic": S2, "date_duration_arithmetic": S2},
        "S06 sample +1/-0; P0-07 full-set run was stopped partway.",
    ),
    sol(
        "SOL-STANDING",
        "Derived list memory: standing answers, patterns, communities",
        "built_off_default",
        ["offline.standing_v2", "offline.patterns_v2", "offline.communities_v2", "read.standing_v2"],
        ["O4-08", "O4-05", "O4-06"],
        ["S34", "S35", "S40", "S64"],
        [ref(D_RECALL, "s8 H4 (derived list memory)"), ref(D_ATLAS, "S34, S35, S40")],
        {"list_aggregation_generic": P, "list_incomplete": S2, "count_wrong": S2, "multi_evidence_recall": S2},
    ),
    sol(
        "SOL-OBLIG",
        "Evidence obligations for joins, durations, lists, counts",
        "built_off_default",
        ["read.obligations", "read.deep_v2"],
        ["R3-16"],
        ["S56", "S63"],
        [ref(D_ATLAS, "S56"), ref(D_GAP, "R3-16 in docs/v2/data/packages.py")],
        {"list_incomplete": P, "count_wrong": P, "date_duration_arithmetic": S2, "list_aggregation_generic": S2},
    ),
    sol(
        "SOL-COUNT",
        "Enumerate-then-count and evidence table (count-verify, verify-slots)",
        "built_harness_flag",
        ["--count-verify", "--evidence-table", "--verify-slots strict|soft|diagnosed"],
        [],
        [],
        [ref(D_GAP, "A04; A06; E06; I56"), ref(D_READER, "s7 LIST")],
        {"count_wrong": P, "list_incomplete": P},
        "Eval-harness answer post-steps, unscreened; a harness result is not an engine result.",
    ),
    # ------------------------------------------------------------------ temporal
    sol(
        "SOL-TEMPLEG",
        "Temporal leg on relative / partial dates and event intervals",
        "built_off_default",
        ["read.temporal_relative", "read.temporal_infer_year", "read.temporal_leg_event_dates", "read.temporal_leg_mentions (on)", "read.timeview_v2"],
        ["R3-04", "R3-14", "W2-14"],
        ["S24", "S62", "S63"],
        [ref(D_RECALL, "s8 T1-T4"), ref(D_FULL, "s4.1")],
        {"temporal_evidence_missing": P},
        "S24 (time view) is skipped: no as-of question set. W2-14 (write.temporal_v2) is measured only through the S62 bundle.",
    ),
    sol(
        "SOL-RELANCHOR",
        "Resolve relative phrases against the session date (annotation) and repair the gaps",
        "in_reference",
        ["read.resolve_relative_dates (on)", "read.relative_dates_anchored (on)", "I79 annotator (bare weekday, 'the weekend', 'N units now')"],
        ["W2-14", "LIB-01"],
        ["S62"],
        [ref(D_FULL, "s4.1 (annotation coverage 186 of 189)"), ref(D_GAP, "I79; A05")],
        {"date_offset_small": P, "date_wrong_event": S2, "relative_date_gold_vs_judge": S2},
        "The base annotator is on; the I79 extensions are built, unscreened. Residual failures are arithmetic on resolved dates.",
    ),
    sol(
        "SOL-DURATION",
        "Duration / interval / milestone arithmetic by code",
        "built_harness_flag",
        ["--duration-solve (I76)", "--milestones (E05)", "--date-repair (I57)"],
        [],
        [],
        [ref(D_GAP, "I76; I57; A05; E05"), ref(D_READER, "s7 DARITH")],
        {"date_duration_arithmetic": P, "date_offset_small": P, "date_wrong_event": S2},
        "Screened in r7-post as small safe candidates (+3/0 and +1/0); harness flags, no v2 screen id.",
    ),
    # ------------------------------------------------------------------ reader
    sol(
        "SOL-READERPROMPT",
        "Reader prompt variants (world-knowledge clause, inference route, answer-style, caption line)",
        "built_harness_flag",
        ["readers.py: dated_world, dated_infer, grounded_detail, routed_generic"],
        [],
        [],
        [ref(D_READER, "s7 INF, DET, IMG"), ref(D_OPEN, "s1")],
        {"inference_open_domain": P, "wrong_detail": P, "image_caption_misuse": P, "refusal_premise_denial": P, "list_incomplete": S2},
        "Prompt-only changes have had the wrong sign before (ADR-054: grounded prompt lost 14 LIST/DET/IMG rows).",
    ),
    sol(
        "SOL-VERIFY",
        "Answer verification and one bounded repair (verify-answer, verify-slots diagnosed)",
        "built_harness_flag",
        ["--verify-answer", "--verify-slots diagnosed"],
        [],
        [],
        [ref(D_GAP, "G01; E06"), ref(D_READER, "s7 DIST")],
        {"distractor_line": P, "wrong_detail": S2, "refusal_premise_denial": S2, "date_wrong_event": S2},
        "Costs one extra reader call on fired questions.",
    ),
    sol(
        "SOL-PRESENT",
        "Present the retrieved hits first, neighbours separate; per-shape window",
        "built_off_default",
        ["read.present_order", "read.replay_window_before/after"],
        [],
        [],
        [ref(D_READER, "s7 DIST A/B")],
        {"distractor_line": P, "wrong_detail": P, "speaker_confusion": S2},
        "Built, no v2 screen. A per-shape window (2/4 only for temporal / multi-turn shapes) is new code.",
    ),
    sol(
        "SOL-BUNDLES",
        "Neighbour turns v2, exact token budget, complete evidence bundles",
        "built_off_default",
        ["read.neighbours_v2", "read.exact_budget", "read.evidence_bundles"],
        ["R3-10", "R3-09a", "R3-09b"],
        ["S19", "S52", "S53", "S54", "S63"],
        [ref(D_GAP, "R03"), ref(D_ATLAS, "S19, S53, S54")],
        {"wrong_detail": S2, "distractor_line": S2, "list_incomplete": S2, "multi_evidence_recall": S2},
        "S19 measured inert on the 80-question sample; S53 needs a binding budget (S52 reference); S54 runs on mined facts that cite their turns.",
    ),
    sol(
        "SOL-PERSPECTIVE",
        "Perspective policy (subject-weight) and perspective-leg weight",
        "in_reference",
        ["read.perspective_mode=subject_weight (on)", "read.perspective_leg", "read.perspective_weight", "read.leg_weights"],
        [],
        [],
        [ref(D_FULL, "s3 (perspective trade-off), s5 lever 1 (half-weight voter)")],
        {"speaker_confusion": P, "fusion_loss_lexical_only": S2},
        "On in the reference; the perspective leg itself crowds BM25-only gold (FULL_RUN_FORENSICS s3.3), tunable by leg_weights (I75).",
    ),
    sol(
        "SOL-ASSET",
        "Resolve image evidence from the original asset",
        "built_off_default",
        ["ingest.assets", "read.asset_evidence=cached|fetch"],
        [],
        [],
        [ref(D_GAP, "E04"), ref(D_READER, "s7 IMG/IMGX")],
        {"image_caption_misuse": S2},
        "Needs a vision model; opt-in and unscreened.",
    ),
    # ------------------------------------------------------------------ metric side
    sol(
        "SOL-DATEGUARD",
        "Deterministic judge pre-checks: date guard and judge conventions (I58)",
        "in_reference",
        ["judge date guard (date_equivalent)", "judge_conventions v1 (list_superset, typo, numeral)"],
        [],
        [],
        [ref(D_FULL, "s2.3(e); s1 (85 verdicts from the guard)"), ref(D_READER, "s4.5, s7 JFN A")],
        {"judge_false_negative": P, "relative_date_gold_vs_judge": P},
        "A measurement fix: it changes the number, not the system. Reports a second column.",
    ),
    sol(
        "SOL-JUDGE-I77",
        "Judge conventions beyond list/typo: containment, date-range and relative-year equivalence",
        "not_built",
        ["(proposed) conventions v3"],
        [],
        [],
        [ref(D_GAP, "I77"), ref(D_FULL, "s2.3(e)")],
        {"judge_false_negative": P, "relative_date_gold_vs_judge": P},
        "Gap register item I77 (NEW 2026-10-10): not implemented.",
    ),
    sol(
        "SOL-ERRATA",
        "Errata file: report accuracy with and without gold/dataset errors",
        "in_reference",
        ["analysis/locomo_errata.json"],
        [],
        [],
        [ref(D_READER, "s5, s7 GOLD"), ref(D_FULL, "s2.3(f)")],
        {"gold_error": P},
        "Changes the denominator, not the system; nothing to screen.",
    ),
    # ------------------------------------------------------------------ gaps (no code)
    sol(
        "SOL-GAP-HYDE",
        "Answer-hypothesis (HyDE) leg for implicit / paraphrased gold",
        "not_built",
        ["(proposed) read.hyde_leg"],
        [],
        [],
        [ref(D_RECALL, "s8 N3 (true HyDE = new code)")],
        {"implicit_or_paraphrased_gold": P, "vocabulary_mismatch_recall": S2},
        "compose_rewrites is the nearest built option; the answer-hypothesis leg does not exist.",
    ),
    sol(
        "SOL-GAP-BIGREADER",
        "Larger reader (27-32B) or thinking mode for inference and detail failures",
        "not_built",
        ["(proposed) llm.roles.reader"],
        [],
        [],
        [ref(D_FULL, "s5 lever 9 (C11 blocked by GPU memory)"), ref(D_READER, "s7 INF B")],
        {"inference_open_domain": P, "wrong_detail": S2, "distractor_line": S2, "refusal_premise_denial": S2},
        "Blocked by GPU memory; no screen id.",
    ),
    sol(
        "SOL-GAP-DISTRACTOR",
        "Owner / event check for wrong-line and wrong-speaker answers",
        "not_built",
        ["(proposed) answer provenance check"],
        [],
        [],
        [ref(D_READER, "s7 DIST C"), ref(D_FULL, "s2.1 d:dis, d:spk")],
        {"distractor_line": P, "speaker_confusion": P},
        "verify-answer is the nearest built step; no engine-side owner check exists.",
    ),
]

# classes for which "no solution needed" is the honest answer (they are not engine faults)
NO_SOLUTION_NEEDED = {
    "ok": "correct answer",
    "gold_error": "dataset error: the errata file reports accuracy with and without it (SOL-ERRATA); not an engine fault",
    "write_path_loss": "0 cases in clean-jina-full (FULL_RUN_FORENSICS s2.3(a): the write path is clean)",
    "no_gold_resolvable": "malformed evidence id (locomo_errata bad_evidence_id): a dataset fix, not an engine one",
}

# ---------------------------------------------------------------------------------------------------------------------
# Screen catalogue
# ---------------------------------------------------------------------------------------------------------------------
HEADERS = ["id", "layer", "title", "setting", "kind", "status", "measured", "what", "flow", "hyp", "reco"]
BUNDLE_OF = {
    "S60": ["S11", "S12", "S14", "S15", "S16", "S17", "S36", "S37", "S38"],
    "S61": ["S13", "S30", "S31", "S32", "S33", "S39"],
    "S62": ["S43", "S44", "S45", "S48", "S49", "S50"],
    "S63": ["S19", "S20", "S21", "S22", "S24", "S27"],
    "S64": ["S33", "S34", "S35", "S40", "S41", "S42", "S46", "S47"],
}


def load_screens(atlas: Path | None = None, queue: Path | None = None) -> dict[str, dict]:
    atlas = atlas or REPO / "docs" / "v2" / "SCREEN_ATLAS.html"
    queue = queue or HERE / "v2_queue.tsv"
    txt = atlas.read_text(encoding="utf-8")
    m = re.search(r"const S=(\[.*?\n\]);", txt, re.S)
    if not m:
        raise SystemExit(f"cannot find the S array in {atlas}")
    rows = json.loads(m.group(1))
    out: dict[str, dict] = {}
    for r in rows:
        d = dict(zip(HEADERS, r, strict=False))
        out[d["id"]] = {
            "id": d["id"],
            "layer": d["layer"],
            "title": d["title"],
            "setting": d["setting"],
            "reader_only": d["kind"] == "J",
            "status": d["status"],
            "measured": d["measured"],
        }
    if queue.exists():
        for line in queue.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            c = line.split("\t")
            if len(c) >= 10 and c[0] in out:
                out[c[0]].update({"batch": c[1], "package": c[2], "arm": c[3], "sets": c[6], "queue_kind": c[8], "est_min": int(c[9]) if c[9].isdigit() else None})
    for b, comps in BUNDLE_OF.items():
        if b in out:
            out[b]["bundle_of"] = comps
    return out


def build_map(screens: dict[str, dict] | None = None) -> dict:
    screens = screens or load_screens()
    sols = json.loads(json.dumps(SOLUTIONS))
    class_to_sols: dict[str, list[dict]] = {c: [] for c in CLASSES}
    for s in sols:
        for c, rel in s["classes"].items():
            class_to_sols[c].append({"solution": s["id"], "relation": rel, "status": s["status"], "screens": s["screens"]})
    screen_targets: dict[str, dict] = {sid: {"primary": [], "secondary": [], "solutions": []} for sid in screens}
    for s in sols:
        for sid in s["screens"]:
            if sid not in screen_targets:
                continue
            screen_targets[sid]["solutions"].append(s["id"])
            for c, rel in s["classes"].items():
                if c not in screen_targets[sid][rel]:
                    screen_targets[sid][rel].append(c)
    for sid, t in screen_targets.items():
        t["secondary"] = [c for c in t["secondary"] if c not in t["primary"]]
        screens[sid]["targets"] = t
    structural_gaps = []
    for c in CLASSES:
        if c in NO_SOLUTION_NEEDED:
            continue
        sl = class_to_sols[c]
        prim = [x for x in sl if x["relation"] == "primary"]
        if not sl:
            structural_gaps.append({"class": c, "gap": "no_solution", "detail": "no solution is mapped to this class"})
            continue
        built = [x for x in prim if x["status"] != "not_built"]
        if not built:
            structural_gaps.append({"class": c, "gap": "no_built_primary_solution", "detail": "only proposed (not built) solutions are aimed at this class"})
            continue
        if not any(x["screens"] for x in prim if x["status"] != "not_built") and not any(x["status"] == "in_reference" for x in prim):
            structural_gaps.append({"class": c, "gap": "no_screen_for_built_solution", "detail": "built primary solutions exist but none has a v2 screen id"})
        elif all(x["status"] == "in_reference" for x in prim):
            structural_gaps.append({"class": c, "gap": "reference_already_has_it", "detail": "the only primary solutions are already on in the reference; the residual needs a new idea"})
    return {
        "schema": SCHEMA,
        "note": "Text-free: ids, setting keys, package ids, screen ids, doc sections. Generated by evals/build_locomo_solution_map.py; hypotheses, not measurements.",
        "reference_run": "clean-jina-full",
        "statuses": list(STATUSES),
        "relations": list(RELATIONS),
        "classes": {
            c: {k: v for k, v in meta.items()} | {"solutions": class_to_sols[c], "no_solution_needed": NO_SOLUTION_NEEDED.get(c)}
            for c, meta in CLASSES.items()
        },
        "solutions": sols,
        "screens": screens,
        "structural_gaps": structural_gaps,
    }


def validate(m: dict) -> list[str]:
    """Schema and referential-integrity errors (empty list = valid). Used by the unit test and ``--check``."""
    err: list[str] = []
    if m.get("schema") != SCHEMA:
        err.append("schema id")
    ids = set()
    for s in m.get("solutions", []):
        for k in ("id", "name", "status", "keys", "packages", "screens", "doc_refs", "classes"):
            if k not in s:
                err.append(f"{s.get('id')}: missing {k}")
        if s.get("status") not in STATUSES:
            err.append(f"{s.get('id')}: bad status {s.get('status')}")
        if s["id"] in ids:
            err.append(f"duplicate solution {s['id']}")
        ids.add(s["id"])
        if not s.get("doc_refs"):
            err.append(f"{s['id']}: no doc_ref (every mapping must cite its section)")
        for r_ in s.get("doc_refs", []):
            if not r_.get("file") or not r_.get("section"):
                err.append(f"{s['id']}: incomplete doc_ref")
        for c, rel in s.get("classes", {}).items():
            if c not in m["classes"]:
                err.append(f"{s['id']}: unknown class {c}")
            if rel not in RELATIONS:
                err.append(f"{s['id']}: bad relation {rel}")
        for sid in s.get("screens", []):
            if sid not in m["screens"]:
                err.append(f"{s['id']}: unknown screen {sid}")
        if s["status"] == "not_built" and s["screens"]:
            err.append(f"{s['id']}: a not_built solution cannot have screens")
    for c, meta in m["classes"].items():
        listed = c in {g["class"] for g in m["structural_gaps"]}
        if not meta.get("solutions") and not meta.get("no_solution_needed") and not listed:
            err.append(f"class {c}: no solution and not listed as a gap")
    return err


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "analysis" / "locomo_solution_map.json"))
    ap.add_argument("--check", action="store_true", help="validate only, write nothing")
    a = ap.parse_args()
    m = build_map()
    errs = validate(m)
    if errs:
        raise SystemExit("solution map invalid:\n  " + "\n  ".join(errs))
    if a.check:
        print(f"ok: {len(m['solutions'])} solutions, {len(m['classes'])} classes, {len(m['screens'])} screens")
        return
    # R14 additions are maintained by hand (sourced from docs/v2/data/r14_master.json); keep them across rebuilds
    out = Path(a.out)
    if out.exists():
        prev = json.loads(out.read_text(encoding="utf-8"))
        if "r14_additions" in prev:
            m["r14_additions"] = prev["r14_additions"]
    out.write_text(json.dumps(m, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {a.out}: {len(m['solutions'])} solutions, {len(m['classes'])} classes, {len(m['screens'])} screens, {len(m['structural_gaps'])} structural gaps")


if __name__ == "__main__":
    main()
