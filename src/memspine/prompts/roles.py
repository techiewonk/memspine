"""Role catalog (D-43): every internal LLM call belongs to exactly one role.

Each role has a shipped default prompt in ``defaults/``; the engine verifies at
startup that every role a feature needs resolves to an active prompt (§4.2).
"""

from __future__ import annotations

__all__ = ["CATALOGUE_ROLE", "PROMPT_ROLES", "catalogue_role"]

PROMPT_ROLES: tuple[str, ...] = (
    "extract",  # entity/fact extraction (M13.3, D-28 LLM fallback)
    "judge",  # conflict adjudication (M4 R-ladder escalation)
    "dedupe",  # borderline-duplicate confirmation (M5)
    "chat",  # user-facing generation
    "consolidate",  # M2 five-stage pipeline (P3)
    "summarize",  # M6 views-not-replacements (P3)
    "subcluster",  # community summaries (P6, D-40)
    "query_rewrite",  # E8 HyDE-style rewriting (P7)
    "reflect",  # M13.7 reflections (P5)
    "firewall_flag",  # E1 instruction-shaped-content detection (P4)
    "extract_edges",  # C1: relationship-edge extraction (graphiti-style writes)
    "resolve_entity",  # C1: entity coreference/aliasing resolution (GP-7: @batch)
    "summarize_entity",  # GP-6: batched entity summaries (summarize_entities stage)
    "invalidate_edge",  # C1: edge add/update/invalidate/noop adjudication
    "anticipate",  # H8: write-time anticipatory retrieval cues (T-Mem-style)
    "relevance",  # H17: 3-way relevance filter on read (Hindsight-style), opt-in
    "plan",  # G2a: read planner for read(mode="auto") (JustMem-style), opt-in
    "sufficiency",  # #38: completeness check + missing-info queries on compose reads, opt-in
    "verify_answer",  # #39: answer verification against the context (Engine.verify_answer)
    "predict_episode",  # #62: Nemori predict step (episode from stored memory), opt-in
    "calibrate",  # #62: Nemori calibrate step (prediction vs episode -> surprise), opt-in
    "describe_asset",  # W2-11: optional vision caption for an asset (write.asset_describer)
    "window_extract",  # W1: call 1 of the adaptive-window write (facts / edges / episode), opt-in
)

#: MD-01 (P-01): the engine's role names mapped onto the model-tier catalogue
#: (``memspine.models.tiers.ROLE_CATALOGUE``). Worker-tier roles read raw turns and stay local;
#: reader roles may be hosted. A role absent here (and from the catalogue) has no tier and is left
#: as bound.
CATALOGUE_ROLE: dict[str, str] = {
    "extract": "extract",
    "window_extract": "extract",
    "anticipate": "extract",
    "extract_edges": "extract_edges",
    "invalidate_edge": "extract_edges",
    "summarize": "summarize",
    "summarize_entity": "summarize",
    "consolidate": "summarize",
    "subcluster": "summarize",
    "predict_episode": "summarize",
    "calibrate": "summarize",
    "session_close": "call2",
    "resolve_entity": "resolve",
    "dedupe": "dedupe",
    "firewall_flag": "dedupe",
    "sufficiency": "sufficiency",
    "query_rewrite": "query_rewrite",
    "relevance": "relevance",
    "chat": "chat",
    "verify_answer": "verify",
    "judge": "judge",
    "reflect": "reflect",
    "plan": "plan",
}


def catalogue_role(role: str) -> str | None:
    """The tier-catalogue role an engine role belongs to, or None when it has none."""
    return CATALOGUE_ROLE.get(role)
