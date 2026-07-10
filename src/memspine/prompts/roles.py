"""Role catalog (D-43): every internal LLM call belongs to exactly one role.

Each role has a shipped default prompt in ``defaults/``; the engine verifies at
startup that every role a feature needs resolves to an active prompt (§4.2).
"""

from __future__ import annotations

__all__ = ["PROMPT_ROLES"]

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
    "resolve_entity",  # C1: entity coreference/aliasing resolution
    "invalidate_edge",  # C1: edge add/update/invalidate/noop adjudication
)
