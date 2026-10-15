"""Derive: the single fact-eligibility policy for every consumer of stored facts (MF-10, NEW-01).

Several offline stages read stored semantic records as *evidence facts*. Before this module two
predicates answered "is this record a fact?" with different channel lists (``is_fact`` in
``envelope_adapter`` and ``is_input_fact`` in ``offline_common``), so a derived view (observation,
standing answer, entity/community/session summary, multires) could pass one and not the other,
and an unkeyed summary could be merged or archived against a restated fact. Both predicates are now
thin wrappers over :func:`is_evidence_fact`; edit the tables here, nowhere else.

What is never an evidence fact (for any purpose)
------------------------------------------------
* a record that is not ``semantic``, is quarantined or flagged as an instruction;
* a derived view: a record written through one of :data:`DERIVED_VIEW_CHANNELS` or carrying one of
  :data:`NON_FACT_TAGS`. Views are projections of facts; feeding them back would loop (a pattern
  mined from a pattern) or let a summary be merged/archived as if it were a restated fact.

Purpose table (``x`` = required, ``-`` = not checked here)
-----------------------------------------------------------
=============== ========= ========= ============ ==========================================
purpose         lifecycle entity    attribute    notes
=============== ========= ========= ============ ==========================================
EDGES           x (+hist) x         -            edge compiler (kept as before)
RECONCILE       x         x         x            supersession only among keyed facts
HISTORIES_MERGE x         x         -            near-duplicate merge (NEW-01: was unkeyed)
HISTORIES_VALUE x (+hist) x         x            value-history projection
ANY             x (+hist) -         -            legacy ``is_fact`` default (no key needed)
INTEGRATION     x         x         -            live index; ``attr:`` tag may supply the
                                                  attribute, so only the entity is required
PATTERNS        -         x         -            lifecycle is judged by ``fact_envelope``
STANDING        -         x         -            same
=============== ========= ========= ============ ==========================================

*lifecycle* = non-empty content and status ACTIVATED (``+hist``: or ARCHIVED with a successor, not
``taint_archived``, when the caller asks for ``include_history``).

Explicit exceptions: none by default. A caller may pass ``allow_channels`` to whitelist a derived
channel for one call; nothing in the engine does today.
"""

from __future__ import annotations

from collections.abc import Collection
from enum import StrEnum

from memspine.config import constants
from memspine.store.records import MemoryRecord, RecordStatus

__all__ = [
    "DERIVED_VIEW_CHANNELS",
    "NON_FACT_TAGS",
    "FactPurpose",
    "is_derived_view",
    "is_evidence_fact",
]


class FactPurpose(StrEnum):
    EDGES = "edges"
    RECONCILE = "reconcile"
    HISTORIES_MERGE = "histories_merge"
    HISTORIES_VALUE = "histories_value"
    INTEGRATION = "integration"
    PATTERNS = "patterns"
    STANDING = "standing"
    #: lifecycle + derived-view gate only (the legacy ``is_fact`` default); no key requirement
    ANY = "any"


#: Channels whose semantic records are derived views or machine-built structure, not facts.
#: Mined facts (``mining``), user/assistant messages, corrections and ingests stay facts.
DERIVED_VIEW_CHANNELS: frozenset[str] = frozenset(
    {
        "observation",
        "standing",
        "list_card",
        "consolidation",
        "reflection",
        "anticipation",
        constants.ENTITY_SUMMARY_CHANNEL,
        constants.SESSION_CLOSE_CHANNEL,
        constants.WINDOW_EPISODE_CHANNEL,
        "community_summary",
        "multires",
        "extract_graph",
        "rule_edges",
        "reorganize",
    }
)

#: Tags that mark a record as a cue/card or as withdrawn from the evidence base.
NON_FACT_TAGS: frozenset[str] = frozenset(
    {constants.CUE_TAG, constants.LIST_CARD_TAG, "taint_archived", "quarantine_rejected"}
)

#: purpose -> (needs lifecycle gate, needs entity, needs attribute)
_RULES: dict[FactPurpose, tuple[bool, bool, bool]] = {
    FactPurpose.EDGES: (True, True, False),
    FactPurpose.RECONCILE: (True, True, True),
    FactPurpose.HISTORIES_MERGE: (True, True, False),
    FactPurpose.HISTORIES_VALUE: (True, True, True),
    FactPurpose.INTEGRATION: (True, True, False),
    FactPurpose.PATTERNS: (False, True, False),
    FactPurpose.STANDING: (False, True, False),
    FactPurpose.ANY: (True, False, False),
}


def is_derived_view(record: MemoryRecord) -> bool:
    """True for a record from a derived-view channel or tagged as a cue, card or withdrawn."""
    return record.source.channel in DERIVED_VIEW_CHANNELS or bool(NON_FACT_TAGS & set(record.tags))


def is_evidence_fact(
    record: MemoryRecord,
    purpose: FactPurpose,
    *,
    include_history: bool = False,
    allow_channels: Collection[str] = (),
) -> bool:
    """Whether ``record`` is an evidence fact for ``purpose`` (see the module table)."""
    if record.memory_type != "semantic" or record.quarantined or record.instruction_flag:
        return False
    channel = record.source.channel
    if channel not in allow_channels and channel in DERIVED_VIEW_CHANNELS:
        return False
    if NON_FACT_TAGS & set(record.tags):
        return False
    lifecycle, need_entity, need_attribute = _RULES[purpose]
    if need_entity and not record.entity:
        return False
    if need_attribute and record.attribute is None:
        return False
    if not lifecycle:
        return True
    if not record.content.strip():
        return False
    if record.status is RecordStatus.ACTIVATED:
        return True
    return (
        include_history
        and record.status is RecordStatus.ARCHIVED
        and record.evolve_to is not None
        and "taint_archived" not in record.tags
    )
