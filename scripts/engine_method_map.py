#!/usr/bin/env python
"""Method-to-module map of ``src/memspine/engine.py`` (restructure package P0-04).

Parses engine.py with ``ast`` and assigns every top-level class, every method of
those classes and every module-level function to a target package/module of the
restructure tree (``docs/v2/RESTRUCTURE.md`` section 1) and to the work package
that moves it (section 3). It also records the intra-engine call graph.

Assignment order (confidence in the output):

1. ``manual``    explicit override table ``MANUAL`` below (qualified name -> target).
2. ``rule``      first matching entry of ``RULES`` (regex over the qualified name).
3. ``heuristic`` no rule matched: majority vote over already assigned neighbours
                 in the call graph (callers and callees); if there are none, the
                 stale line-range hint of RESTRUCTURE section 3.

The line ranges in RESTRUCTURE section 3 are for commit c83e93a and are stale. They
are only a last-resort hint and are reported next to each row (``hint_wp``).

Usage (from the repo root)::

    python scripts/engine_method_map.py            # write json + md under docs/v2/data
    python scripts/engine_method_map.py --check    # exit 1 if a unit is unassigned
                                                   # or the committed files are stale

No third-party imports; deterministic output.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "src" / "memspine" / "api" / "engine.py"
OUT_JSON = ROOT / "docs" / "v2" / "data" / "engine_method_map.json"
OUT_MD = ROOT / "docs" / "v2" / "data" / "engine_method_map.md"

# --------------------------------------------------------------------------------------
# Target tree: package -> work package that owns the move (RESTRUCTURE section 3).
# Sub-packages of a package may override (see WP_BY_MODULE).
# --------------------------------------------------------------------------------------
WP_BY_PACKAGE = {
    "schemas": "MOD-01",
    "store": "F-05",
    "trust": "F-12",
    "memory": "F-12",
    "ingest": "F-09",
    "derive": "F-15",
    "read": "F-10a",
    "compose": "F-11",
    "models": "F-06",
    "runtime": "F-08",
    "api": "F-13a",
    "config": "F-07",
    "observability": "F-13a",
}
# F-10b takes gate / timeview / deep; F-13b rest+mcp is not in engine.py (api/rest, api/mcp, api/tools).
WP_BY_MODULE = {
    "read/gate.py": "F-10b",
    "read/timeview.py": "F-10b",
    "read/deep.py": "F-10b",
    "read/lift.py": "F-10b",
    "api/engine.py": "F-13a",
    "api/handle.py": "F-13a",
    "ingest/commit.py": "F-04",
}

# Stale hints from RESTRUCTURE section 3 (commit c83e93a), used only as last resort.
HINT_RANGES = [
    (1672, 2000, "F-09"),
    (2013, 2330, "F-12"),
    (2331, 3900, "F-10a/b"),
    (3879, 4200, "F-11"),
    (4270, 4995, "F-09"),
    (4996, 5835, "F-10a/b"),
    (5836, 6575, "F-11"),
    (6576, 6760, "F-10a/b"),
    (7505, 8062, "F-10a/b"),
    (8063, 9610, "F-11"),
    (9611, 13207, "F-12"),
    (13468, 13838, "F-09"),
    (13838, 15179, "F-08"),
]
HINT_WP_TO_PKG = {
    "F-09": ("ingest", "write.py"),
    "F-12": ("trust", "firewall.py"),
    "F-10a/b": ("read", "router.py"),
    "F-11": ("compose", "sections.py"),
    "F-08": ("runtime", "container.py"),
}

# --------------------------------------------------------------------------------------
# Rules: (regex over qualified name, "package/module.py"). First match wins.
# Qualified name is "Class.method" for methods, bare name for module functions/classes.
# --------------------------------------------------------------------------------------
RULES: list[tuple[str, str]] = [
    # ---- public result / decision types -> schemas (versioned I/O types)
    (r"^(AuthorizeDecision|WriteOutcome|ReadResult|AnswerVerification)$", "schemas/public.py"),
    # ---- Engine lifecycle, wiring, builders -> runtime/container.py (F-08)
    (r"^Engine\.(__init__|start|stop|_start_inner|_teardown|is_started|_require_started)$", "runtime/container.py"),
    (r"^Engine\._build_(secrets|storage|cache|vector_store|lexical_store|graph_store|llm_router)$", "runtime/container.py"),
    (r"^Engine\._build_(embedder|runner|extractor|write_pipeline)$", "runtime/container.py"),
    (r"^Engine\._(lexical_projector_name|lexical_variant|client_is_memory|derived_base|rescore_settings)$", "runtime/container.py"),
    (r"^Engine\._(memory_policy|consolidation_option|entity_finder|config|rerank_provider)$", "runtime/container.py"),
    (r"^Engine\._(check_decision_provider|decision_provider|decider_active|decider_adapter)$", "runtime/container.py"),
    (r"^Engine\.(set_decider|set_asset_adapter|set_external_provider)$", "runtime/container.py"),
    (r"^Engine\._(asset_adapter|external_broker)$", "runtime/container.py"),
    (r"^Engine\._(install_write_timers|write_timers)$", "runtime/container.py"),
    (r"^Engine\.write_timers$", "runtime/container.py"),
    # ---- derivation builders (callables handed to the pipelines) -> derive
    (r"^Engine\._build_(entity_summarizer|entity_resolver|summarize|summarize_incremental|episode_predictor|calibrator|reflector|anticipator|assertion_projector|fact_miner|fact_dater|class_labeller|edge_extractor|session_edge_extractor|edge_adjudicator)$", "derive/stages.py"),
    (r"^Engine\._(edge_extract_callable|predict_calibrate_role)$", "derive/stages.py"),
    (r"^Engine\._deposit_(surprise_fact|profile_reflection|anticipated_cues|mined_fact|list_card)$", "derive/integrate.py"),
    (r"^Engine\._(write_extracted_fact|auto_watch)$", "derive/integrate.py"),
    (r"^Engine\._(reflect|evolve_links)$|^Engine\.(reflect|associate|related)$", "derive/graph.py"),
    (r"^Engine\.(sleep|rebuild|_inflate_all|sync_prompt_versions)$", "derive/scheduler.py"),
    (r"^_fact_value$", "derive/integrate.py"),
    # ---- sync wrappers, stats and admin facade -> api
    (r"^Engine\.(start_sync|write_sync|retrieve_sync|stop_sync|_run_sync|_close_sync_loop)$", "api/engine.py"),
    (r"^Engine\.(describe|model_calls|rerank_stats|model_usage|usage|structured_stats|llm|asset_stats|external_stats|window_stats)$", "api/engine.py"),
    (r"^Engine\.(namespace_stats|conversation|session_memories|timeline|sessions|brief|rating_profile)$", "api/handle.py"),
    (r"^Engine\.(ingest|export)$", "api/handle.py"),
    # ---- ingest: write door (F-09)
    (r"^Engine\.(write|write_ex|write_messages|write_episode|add_cues)$", "ingest/write.py"),
    (r"^Engine\._(write_turns|write_locked|write_locked_ex|write_screened|screen_derived|screen_write|prewarm_embeddings|prefetch_neighbours|depositable_contents|link_reply|reopen_session|localize|event_time|sequenced|redact_fields)$", "ingest/write.py"),
    (r"^Engine\._(grade_sensitivity|govern_write|mark_inferred|sensitivity_scope_open|scope_check|inherit_governance|derived_parents_gone)$", "ingest/write.py"),
    (r"^Engine\.(review_inferred|end_session)$", "ingest/write.py"),
    (r"^Engine\._(append_and_project|projection_batch|flush_projections|evict_from_encoder|records)$", "ingest/commit.py"),
    (r"^Engine\._(namespace_lock|pipeline_ctx|ledger_key|record_reads|consume_reads)$", "ingest/commit.py"),
    (r"^Engine\._(build_windows|window_session_closed|deposit_window_episode)$|^Engine\.(flush_windows|close_session)$", "ingest/segment.py"),
    (r"^_(zone_of|parse_event_time)$", "ingest/stamp.py"),
    (r"^_(reply_targets|reply_target|caller_tags|merge_correction_tags|looks_like_recall|speaker_of|turn_content)$", "ingest/write.py"),
    (r"^_NeighbourBatch(\..*)?$", "ingest/write.py"),
    (r"^_screen_text$", "trust/privacy.py"),
    # ---- trust: firewall, horizon, privacy, consent, erasure, audit, quarantine
    (r"^Engine\.(principal_reputation|effective_trust|authorize|approve|_approved|_parent_trust_cap|_integrity|_grants_cached)$", "trust/firewall.py"),
    (r"^Engine\._(assess_write|gated_assess|corroborate|release_change|withheld_texts)$", "trust/firewall.py"),
    (r"^Engine\.(retract|send)$", "trust/firewall.py"),
    (r"^Engine\.(forget|erase_subject|erase_namespace|verify_forget|forget_requests)$", "trust/erasure.py"),
    (r"^Engine\._(descendants|log_derivations|forget_many|erased_entity_nodes|forget_target|erasable_texts|purge_caches|forget_locked)$", "trust/erasure.py"),
    (r"^Engine\._(audit_head_hash|append_audit|audit_read|audit_action)$|^Engine\.(bulk_read_alerts|verify_audit_chain|audit_chain_ok)$", "trust/audit.py"),
    (r"^Engine\._(consent_ok|consent_context)$", "trust/consent.py"),
    (r"^Engine\.(expire_retention|vet)$|^Engine\._(retention)$", "trust/horizon.py"),
    (r"^Engine\.(audit_taint|quarantine|rollback_taint|repair_taint|verify_integrity|list_quarantined|approve_quarantined|reject_quarantined)$", "trust/quarantine.py"),
    (r"^Engine\._(taint_archived_ids|lineage_window|undo_displacement|held_record|lineage_roots|independent_roots|independent_principal)$", "trust/quarantine.py"),
    (r"^_taint_archive_delta$", "trust/quarantine.py"),
    (r"^Engine\._remote_view$", "trust/privacy.py"),
    (r"^Engine\.(correct|_apply_correction|review_evidence|feedback|classify)$", "trust/firewall.py"),
    # ---- memory-type mechanisms (F-12)
    (r"^Engine\.(set_persona)$", "memory/working.py"),
    (r"^Engine\.(add_skill|record_plan|promote_skill|deprecate_skill|skills|recall_plan|recall_plans|recall_lessons|record_outcome|prune_experience|record_trajectory|trajectory_window|add_exemplar)$", "memory/procedural.py"),
    (r"^Engine\._(write_procedural|procedural_policy|require_procedural|is_live_advisory|append_feedback|repeat_lesson|quarantine_lesson|similar_advisory|task_state_record|put_task_state)$", "memory/procedural.py"),
    (r"^Engine\.(set_task_state|task_state|update_subgoal|close_task|task_search)$", "memory/procedural.py"),
    (r"^Engine\.(watch|due|acknowledge_watch)$|^Engine\._require_prospective$", "memory/prospective.py"),
    (r"^Engine\.(grant|revoke|shared_search|subscribe|subscriptions|grants_from)$|^Engine\._(shared_search|require_shared)$", "memory/shared.py"),
    # ---- read: legs, fusion, gate, timeview, deep, router (F-10)
    (r"^Engine\.(retrieve|search|_search|read|_read|_read_routed)$", "read/router.py"),
    (r"^Engine\._(plan_read_mode|query_contract|llm_query_contract|llm_read_plan|query_rewrite_probes|list_mode_fires|list_fires|bridge_fires|decided)$", "read/request.py"),
    (r"^Engine\._(probe_legs|vector_leg|lexical_leg|encoder|encoder_legs|metadata_legs|person_time_leg|maxsim_leg|word_vector_leg|session_leg|speaker_vote_leg|bridge_leg|anchor_legs|cluster_legs|leg_weights_for|strip_speaker_names|bridge_gate)$", "read/legs.py"),
    (r"^Engine\._(graph_admit|graph_seeds|graph_walk|graph_leg|graph_node_legs|graph_rerank|causal_walk|community_gate|admitted_edge_sources|edge_sources|graph_facts_section)$", "read/legs.py"),
    (r"^Engine\._(mmr_order|tie_keys|with_session_neighbours|session_capped|penalise_reinjected)$", "read/fusion.py"),
    (r"^Engine\._(gate_hits|gate_raw_tops|store_calibration|calibrated_gate|gate_bypassed|relevance_gate|relevance_filter|raw_evidence_strong)$", "read/gate.py"),
    (r"^Engine\._(date_allowed|by_time|current_state_view|history_view|timeline_blocks|live_view)$", "read/timeview.py"),
    (r"^Engine\._(perspective_options|persp_state_for|annotate_perspective|question_perspective|perspective_leg|apply_perspective|applies_to_person|entity_vocabulary)$", "read/lift.py"),
    (r"^Engine\._(agentic_fires|agentic_extend|agentic_assemble|agentic_slot_loop|slot_expand|slot_neighbours|fact_chain_extend|event_ledger_extend|attach_antecedents|missing_info_queries)$", "read/deep.py"),
    (r"^Engine\._(static_embedding_prefilter)$|^_static_prefilter$", "read/rerank.py"),
    (r"^Engine\._(asset_overlay|external_overlay|read_overlay|attachment_refs|register_assets)$", "read/legs.py"),
    (r"^_(ids|cut_missing|log_leg_floor|leg_rank_map|protected_ids|leg_family|family_slots|cosine|unit|jaccard|minmax_quiet|minmax_normalize|balanced_pool)$", "read/fusion.py"),
    (r"^_(snapshot_listings|snapshot_scoped|PassiveScope|passive_scoped|passive_hidden|without_entity|current_at|as_options_dict|shares_key|message_sender)$", "read/timeview.py"),
    (r"^search_forensics$", "observability/trace.py"),
    # ---- compose (F-11)
    (r"^Engine\.(assemble|_assemble|_assemble_core|_compose|_compose_checked)$", "compose/windows.py"),
    (r"^Engine\._(procedural_blocks|place_procedural|expand_trajectories|place_lead|claims_only|decorate)$", "compose/pack.py"),
    (r"^Engine\._(lead_clean|lead_record|standing_block|lead_section|cards_section|from_low_trust|cards_text|slots_section|subject_card|facts_to_sources|recent_section|session_digest_section|novelty_section|profile_section|packed_profile_section|entity_summary_section|duration_section|count_section|replay_neighbour)$", "compose/sections.py"),
    (r"^Engine\._(read_headers|count_allowance|cap_lead_blocks|headers_cost|cards_gated|header_hide|header_abstains|attach_headers|best_source_turn|profile_line_relevant|budget_factor|reply_budget|context_eligible|dedupe_candidates)$", "compose/pack.py"),
    (r"^Engine\._(window_sides|expand_neighbours|window_turns)$", "compose/windows.py"),
    (r"^Engine\._(render|render_volatile|render_dated|annotate_dates|wrap_instruction|wrap_untrusted|wrap_for_context)$|^Engine\.chat_messages$", "compose/render.py"),
    (r"^Engine\.(final_answer|verify_answer)$", "compose/answer.py"),
    (r"^_(gap_marker|unrendered|attributed|excerpted|gisted|json_line)$", "compose/render.py"),
]

# --------------------------------------------------------------------------------------
# Manual overrides for ambiguous methods (qualified name -> (module, reason)).
# Reviewed by a human; these win over RULES.
# --------------------------------------------------------------------------------------
MANUAL: dict[str, tuple[str, str]] = {
    "Engine._integrity": ("trust/firewall.py", "tiny accessor for the integrity monitor used by the write firewall"),
    "Engine._grants_cached": ("memory/shared.py", "cached grant lookup; consumed by authorize and shared reads, owner is shared memory"),
    "Engine.principal_reputation": ("trust/firewall.py", "reputation feeds trust caps, not the shared-memory grants"),
    "Engine._remote_view": ("trust/privacy.py", "external-provider redaction view (privacy), not read"),
    "Engine.expire_retention": ("trust/horizon.py", "retention horizon sweep"),
    "Engine.vet": ("trust/firewall.py", "operator vetting of a record, firewall verb"),
    "Engine.classify": ("trust/privacy.py", "sensitivity classification verb"),
    "Engine.feedback": ("trust/firewall.py", "feedback adjusts trust of the cited records"),
    "Engine.review_evidence": ("trust/firewall.py", "evidence review feeds trust"),
    "Engine._record_reads": ("read/router.py", "read-side bookkeeping consumed by _consume_reads (cue learning); not a commit step"),
    "Engine._consume_reads": ("read/router.py", "read-side bookkeeping, see _record_reads"),
    "Engine._ledger_key": ("read/router.py", "key for the read ledger"),
    "Engine._records": ("store/log.py", "record fetch by id under the namespace; store-level"),
    "Engine._namespace_lock": ("runtime/locks.py", "namespace lock: runtime concern (models never run inside it)"),
    "Engine._pipeline_ctx": ("runtime/container.py", "builds the PipelineContext handed to derive stages (DI provider)"),
    "Engine._append_and_project": ("store/log.py", "append to the event log and project; store.log + projections"),
    "Engine._projection_batch": ("store/log.py", "projection batching around the log"),
    "Engine._flush_projections": ("store/log.py", "projection flush"),
    "Engine._evict_from_encoder": ("read/legs.py", "encoder cache eviction on erase; touches the read encoder leg"),
    "Engine._derived_parents_gone": ("trust/erasure.py", "derived-record cascade check after a forget"),
    "Engine._inherit_governance": ("trust/firewall.py", "trust/provenance inheritance for derived deposits"),
    "Engine._link_reply": ("ingest/write.py", "reply edge on write"),
    "Engine._reopen_session": ("ingest/segment.py", "session reopen belongs with window/session segmentation"),
    "Engine.end_session": ("ingest/segment.py", "session lifecycle"),
    "Engine.add_cues": ("derive/integrate.py", "cue derivation write; deposits derived cues"),
    "Engine.correct": ("ingest/write.py", "correction is a write (supersede) with firewall screening"),
    "Engine._apply_correction": ("ingest/write.py", "apply step of correct()"),
    "Engine.timeline": ("read/timeview.py", "time-ordered read over facts"),
    "Engine.sessions": ("read/timeview.py", "session listing read"),
    "Engine.set_persona": ("memory/working.py", "persona block of working memory"),
    "Engine.ingest": ("api/handle.py", "high-level ingest verb wrapping write_messages"),
    "Engine.export": ("store/export.py", "export of a namespace; store.export"),
    "Engine.rebuild": ("store/migrate.py", "projection rebuild from the log"),
    "Engine.sync_prompt_versions": ("derive/scheduler.py", "prompt-version resync triggers re-derivation"),
    "Engine._deposit_window_episode": ("ingest/segment.py", "deposit of a closed window as an episode"),
    "Engine._with_session_neighbours": ("read/legs.py", "session-neighbour expansion leg"),
    "Engine._session_capped": ("read/fusion.py", "per-session cap on fused hits"),
    "Engine._raw_evidence_strong": ("read/gate.py", "gate decision input"),
    "Engine._live_view": ("compose/render.py", "live (non-archived) view used when rendering"),
    "Engine._timeline_blocks": ("compose/sections.py", "timeline blocks are context sections"),
    "Engine._history_view": ("compose/sections.py", "history section of the context"),
    "Engine._current_state_view": ("compose/sections.py", "current-state section of the context"),
    "Engine._graph_facts_section": ("compose/sections.py", "context section"),
    "Engine._reply_budget": ("compose/pack.py", "output budget share B_output"),
    "Engine._context_eligible": ("compose/pack.py", "eligibility filter for packing"),
    "Engine._dedupe_candidates": ("compose/pack.py", "packing de-duplication"),
    "Engine._cluster_legs": ("read/legs.py", "cluster leg"),
    "Engine._entity_vocabulary": ("read/lift.py", "entity vocabulary for perspective lift"),
    "Engine._applies_to_person": ("read/lift.py", "perspective filter"),
    "Engine._list_mode_fires": ("read/request.py", "list-mode trigger decision from the query contract"),
    "Engine._list_fires": ("read/request.py", "list trigger"),
    "Engine._bridge_fires": ("read/request.py", "bridge trigger"),
    "Engine._decided": ("read/request.py", "decider verdict used by trigger checks"),
    "Engine._asset_adapter": ("ingest/assets.py", "asset adapter resolution at deposit"),
    "Engine._register_assets": ("ingest/assets.py", "asset registration on write"),
    "Engine._attachment_refs": ("ingest/assets.py", "attachment references on write"),
    "Engine.config": ("api/engine.py", "public read-only config property of the facade"),
    "Engine.asset_stats": ("api/engine.py", "stats accessor"),
    "Engine.external_stats": ("api/engine.py", "stats accessor"),
    "Engine._asset_overlay": ("read/legs.py", "asset overlay appended to a read"),
    "Engine._external_overlay": ("read/legs.py", "external-provider overlay appended to a read"),
    "Engine._read_overlay": ("read/legs.py", "read overlay composition"),
    "Engine._external_broker": ("runtime/container.py", "external broker provider"),
    "_json_line": ("compose/render.py", "one-line JSON rendering used by prompt builders (compose/render or prompts); used in relevance filter"),
    "_message_sender": ("ingest/write.py", "sender of a message record, used by effective-trust/authorize path"),
    "_shares_key": ("compose/render.py", "used only in render ordering (present_order)"),
    "_as_options_dict": ("config/loader.py", "coerces config sub-trees to dicts; used by builders and policies"),
    "_current_at": ("read/timeview.py", "as-of validity predicate"),
    "_snapshot_listings": ("read/timeview.py", "snapshot-consistent listings for reads"),
    "_snapshot_scoped": ("read/timeview.py", "decorator giving a read a snapshot scope"),
    "_PassiveScope": ("read/timeview.py", "passive-session visibility scope of a read"),
    "_passive_scoped": ("read/timeview.py", "decorator widening passive-session scope"),
    "_passive_hidden": ("read/timeview.py", "passive-session visibility predicate"),
    "_without_entity": ("compose/sections.py", "used by timeline rendering"),
    "_static_prefilter": ("read/rerank.py", "static-embedding prefilter before the reranker"),
    "_balanced_pool": ("read/fusion.py", "balanced candidate pool"),
    "_unit": ("read/fusion.py", "vector normalisation helper"),
    "_cosine": ("read/fusion.py", "similarity helper"),
    "_jaccard": ("read/fusion.py", "set similarity helper"),
    "search_forensics": ("observability/trace.py", "installs the FORENSICS sink of core/trace_sink"),
    "_excerpted": ("compose/render.py", "excerpt a record for rendering"),
    "_gisted": ("compose/render.py", "gist a record for rendering"),
    "_attributed": ("compose/render.py", "attribution marker for rendering"),
    "_unrendered": ("compose/render.py", "detects unrendered placeholders"),
    "_gap_marker": ("compose/render.py", "time-gap marker"),
}

# Units with sub-packages other than the single-file modules above are fine: the
# module column is a target file in the tree of RESTRUCTURE section 1; sub-packages
# (read/legs/, derive/stages/) are listed here as files for readability.


def _wp_for(module: str) -> str:
    if module in WP_BY_MODULE:
        return WP_BY_MODULE[module]
    return WP_BY_PACKAGE[module.split("/", 1)[0]]


def _hint_wp(line: int) -> str | None:
    for lo, hi, wp in HINT_RANGES:
        if lo <= line <= hi:
            return wp
    return None


def _walk_units(tree: ast.Module) -> list[dict]:
    units: list[dict] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            units.append(_unit(node, "function", node.name, None))
        elif isinstance(node, ast.ClassDef):
            methods = [
                n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            units.append(_unit(node, "class", node.name, None, container=bool(methods)))
            for m in methods:
                units.append(_unit(m, "method", f"{node.name}.{m.name}", node.name))
    return units


def _unit(node: ast.AST, kind: str, qual: str, cls: str | None, container: bool = False) -> dict:
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    end = node.end_lineno or node.lineno
    return {
        "name": qual,
        "kind": kind,
        "class": cls,
        "start": start,
        "end": end,
        "lines": end - start + 1,
        "async": isinstance(node, ast.AsyncFunctionDef),
        "container": container,
        "_node": node,
    }


def _call_graph(units: list[dict]) -> None:
    """Attach ``calls`` (intra-engine references) and ``called_by`` to each unit."""
    by_method = {
        u["name"].split(".", 1)[1]: u["name"] for u in units if u["kind"] == "method" and u["class"] == "Engine"
    }
    by_func = {u["name"]: u["name"] for u in units if u["kind"] in ("function", "class")}
    for u in units:
        if u["container"] or u["kind"] == "class":
            u["calls"] = []
            continue
        found: set[str] = set()
        for n in ast.walk(u["_node"]):
            if (
                isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name)
                and n.value.id in ("self", "engine")
                and u["class"] == "Engine"
                and n.attr in by_method
            ):
                found.add(by_method[n.attr])
            elif isinstance(n, ast.Name) and n.id in by_func:
                found.add(by_func[n.id])
        found.discard(u["name"])
        u["calls"] = sorted(found)
    called_by: dict[str, set[str]] = defaultdict(set)
    for u in units:
        for c in u["calls"]:
            called_by[c].add(u["name"])
    for u in units:
        u["called_by"] = sorted(called_by.get(u["name"], ()))


def _assign(units: list[dict]) -> None:
    compiled = [(re.compile(p), m) for p, m in RULES]
    for u in units:
        u["module"] = None
        u["confidence"] = None
        u["reason"] = ""
        name = u["name"]
        if name == "Engine":
            u["module"], u["confidence"] = "api/engine.py", "manual"
            u["reason"] = "container class: the facade that remains after all moves (api/engine.py + api/handle.py)"
            continue
        if name in MANUAL:
            u["module"], u["reason"] = MANUAL[name]
            u["confidence"] = "manual"
            continue
        for rx, mod in compiled:
            if rx.match(name):
                u["module"], u["confidence"] = mod, "rule"
                break
    # Methods of non-Engine classes inherit the class assignment.
    byname = {u["name"]: u for u in units}
    for u in units:
        if u["module"] is None and u["kind"] == "method" and u["class"] != "Engine":
            parent = byname[u["class"]]
            if parent["module"]:
                u["module"], u["confidence"] = parent["module"], "rule"
                u["reason"] = "inherits from its class"
    # Heuristic pass: call-graph majority vote over assigned neighbours, iterated;
    # last resort the stale line-range hint.
    for _ in range(5):
        changed = False
        for u in units:
            if u["module"] is not None:
                continue
            votes = Counter()
            for nb in u["calls"] + u["called_by"]:
                m = byname[nb]["module"]
                if m and byname[nb]["confidence"] in ("rule", "manual"):
                    votes[m] += 1
            if votes:
                (mod, n), = votes.most_common(1)
                total = sum(votes.values())
                u["module"], u["confidence"] = mod, "heuristic"
                u["reason"] = f"call-graph vote {n}/{total} over neighbours"
                changed = True
        if not changed:
            break
    for u in units:
        if u["module"] is None:
            wp = _hint_wp(u["start"])
            pkg, mod = HINT_WP_TO_PKG.get(wp or "", ("api", "engine.py"))
            u["module"], u["confidence"] = f"{pkg}/{mod}", "heuristic"
            u["reason"] = f"line-range hint {wp}" if wp else "no signal; defaulted to api/engine.py"
    for u in units:
        u["package"] = u["module"].split("/", 1)[0]
        u["work_package"] = _wp_for(u["module"])
        u["hint_wp"] = _hint_wp(u["start"])


def build() -> dict:
    src = ENGINE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    units = _walk_units(tree)
    _call_graph(units)
    _assign(units)
    total_lines = len(src.splitlines())
    counted = [u for u in units if not u["container"]]
    covered = sum(u["lines"] for u in counted)
    for u in units:
        u.pop("_node", None)
    packages: dict[str, dict] = {}
    for u in counted:
        p = packages.setdefault(u["package"], {"units": 0, "lines": 0, "modules": {}})
        p["units"] += 1
        p["lines"] += u["lines"]
        m = p["modules"].setdefault(u["module"], {"units": 0, "lines": 0, "work_package": u["work_package"]})
        m["units"] += 1
        m["lines"] += u["lines"]
    return {
        "source": "src/memspine/api/engine.py",
        "engine_lines": total_lines,
        "units": len(units),
        "counted_units": len(counted),
        "covered_lines": covered,
        "uncovered_lines": total_lines - covered,
        "note": (
            "Totals count methods, module functions and method-less classes once; container "
            "classes (Engine, _NeighbourBatch) are listed but excluded from totals. Uncovered lines "
            "are imports, module constants, ContextVars and blank/comment lines between units."
        ),
        "packages": dict(sorted(packages.items())),
        "units_detail": units,
    }


def render_md(data: dict) -> str:
    units = data["units_detail"]
    lines: list[str] = []
    a = lines.append
    a("# Engine method-to-module map")
    a("")
    a("Generated by `scripts/engine_method_map.py` from `src/memspine/engine.py`; do not edit by hand")
    a("(change `RULES` / `MANUAL` in the script and re-run). Targets follow `docs/v2/RESTRUCTURE.md` section 1;")
    a("work-package ownership follows section 3. The line ranges of section 3 are stale and are used only as a")
    a("last-resort hint (`hint_wp`).")
    a("")
    a(f"- engine.py: {data['engine_lines']} lines; {data['units']} units "
      f"({data['counted_units']} counted: methods, functions, method-less classes).")
    a(f"- Lines inside counted units: {data['covered_lines']}; outside any unit "
      f"(imports, module constants, ContextVars, blank): {data['uncovered_lines']}.")
    conf = Counter(u["confidence"] for u in units if not u["container"])
    a(f"- Confidence: rule {conf['rule']}, manual {conf['manual']}, heuristic {conf['heuristic']}.")
    a("- Unassigned: 0.")
    a("")
    a("## Summary per target package")
    a("")
    a("| Package | Units | Lines | Modules |")
    a("|---|---:|---:|---|")
    for pkg, p in data["packages"].items():
        mods = ", ".join(f"`{m}` ({v['units']}/{v['lines']}, {v['work_package']})" for m, v in sorted(p["modules"].items()))
        a(f"| {pkg} | {p['units']} | {p['lines']} | {mods} |")
    a("")
    a("Modules cell: `path` (units/lines, owning work package).")
    a("")
    a("## Heuristic assignments (review these)")
    a("")
    heur = [u for u in units if u["confidence"] == "heuristic"]
    if not heur:
        a("None.")
    else:
        a("| Unit | Lines | Assigned | Why |")
        a("|---|---|---|---|")
        for u in heur:
            a(f"| `{u['name']}` | {u['start']}-{u['end']} | `{u['module']}` | {u['reason']} |")
    a("")
    a("## Full table")
    a("")
    a("Sorted by source line. `calls` / `called_by` count intra-engine references (full lists in the JSON).")
    a("")
    a("| Unit | Kind | Lines | n | Package | Module | WP | Conf | calls | called_by | hint_wp |")
    a("|---|---|---|---:|---|---|---|---|---:|---:|---|")
    for u in units:
        a(
            f"| `{u['name']}` | {u['kind']}{' (async)' if u['async'] else ''} | {u['start']}-{u['end']} | "
            f"{u['lines']} | {u['package']} | `{u['module']}` | {u['work_package']} | {u['confidence']} | "
            f"{len(u['calls'])} | {len(u['called_by'])} | {u['hint_wp'] or ''} |"
        )
    a("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="verify coverage and that outputs are current")
    args = ap.parse_args(argv)
    data = build()
    unassigned = [u["name"] for u in data["units_detail"] if not u.get("module")]
    heur = [u for u in data["units_detail"] if u["confidence"] == "heuristic"]
    js = json.dumps(data, indent=1, ensure_ascii=False) + "\n"
    md = render_md(data)
    if args.check:
        bad = list(unassigned)
        if not OUT_JSON.exists() or OUT_JSON.read_text(encoding="utf-8") != js:
            bad.append("engine_method_map.json is stale")
        if not OUT_MD.exists() or OUT_MD.read_text(encoding="utf-8") != md:
            bad.append("engine_method_map.md is stale")
        if bad:
            print("FAIL:", *bad, sep="\n  ")
            return 1
        print("ok")
        return 0
    OUT_JSON.write_text(js, encoding="utf-8", newline="\n")
    OUT_MD.write_text(md, encoding="utf-8", newline="\n")
    print(f"units: {data['units']} (counted {data['counted_units']}), unassigned: {len(unassigned)}")
    for pkg, p in data["packages"].items():
        print(f"  {pkg:14s} {p['units']:4d} units {p['lines']:6d} lines")
    print(f"heuristic assignments: {len(heur)}")
    for u in heur:
        print(f"  {u['name']:45s} {u['start']}-{u['end']} -> {u['module']}  ({u['reason']})")
    return 1 if unassigned else 0


if __name__ == "__main__":
    sys.exit(main())
