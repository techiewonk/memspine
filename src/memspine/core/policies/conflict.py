"""Conflict policy (M4): bi-temporal verdicts over (entity, attribute) keys.

Deterministic R-ladder — pure decision logic, no I/O; the semantic store acts
on the verdict. The optional LLM judge (judge prompt, D-43) is an escalation
the store may apply to AMBIGUOUS outcomes; the ladder itself never needs it.

Ladder (evaluated on two records sharing a fact key):

- R0 identity:   same content fingerprint                        → NOOP
- R1 trust gate: incoming markedly less trusted than existing    → NOOP (E1 seam)
- R2 authority:  source-authority comparison (shared memory)     → P7
- R3 temporal:   incoming is newer (per ``bias``)                → UPDATE
- R2'' retraction: incoming tagged ``retract``                     → INVALIDATE
- R2'' contest:  (opt-in, H9) same event time (within a window) and
                 trust within ``contest_trust_margin``, nothing decides → CONTEST
                 (both kept and tagged ``disputed``; the current fact stays the
                 single active one, so the store's invariant holds)
- R3 temporal:   incoming is newer (per ``bias``)                → UPDATE
- R4 backfill:   incoming is older than the current fact         → ADD (historical,
                 store closes its validity at existing.valid_from). With the
                 Graphiti overlap rule, a closed interval that ended before the
                 current fact began never displaces it (also ADD).
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from memspine.core.policies.base import BindablePolicy, PolicyOptions
from memspine.core.records import MemoryRecord

__all__ = ["ConflictPolicy", "ConflictVerdict"]


class ConflictVerdict(StrEnum):
    ADD = "add"
    UPDATE = "update"
    INVALIDATE = "invalidate"
    NOOP = "noop"
    CONTEST = "contest"


class ConflictOptions(PolicyOptions):
    bias: str = "newest"  # R3 default: latest valid_from wins; "oldest" inverts
    trust_margin: float = 0.3  # R1: reject when incoming.trust < existing - margin
    #: H9: keep both values (tagged ``disputed``) when neither event time nor trust
    #: decides between them, instead of letting the later write silently win.
    contest_ties: bool = False
    contest_window_seconds: float = 0.0
    contest_trust_margin: float = 0.05
    #: A write less trusted than the current fact (but inside ``trust_margin``)
    #: CONTESTs it instead of superseding or retracting it, so a lower-trust source
    #: cannot silently archive a higher-trust fact (the supersession availability
    #: cell, `paper_aamas27/results/edge_cells.md`). Off by default.
    contest_lower_trust: bool = False


class ConflictPolicy(BindablePolicy):
    name: ClassVar[str] = "conflict"
    Options: ClassVar[type[PolicyOptions]] = ConflictOptions

    def resolve(self, incoming: MemoryRecord, existing: MemoryRecord) -> ConflictVerdict:
        options = self.options
        assert isinstance(options, ConflictOptions)

        # R0 — identical statement: nothing to do.
        if incoming.content_fingerprint == existing.content_fingerprint:
            return ConflictVerdict.NOOP

        same_key = (
            incoming.entity is not None
            and incoming.entity == existing.entity
            and incoming.attribute == existing.attribute
        )
        if not same_key:
            return ConflictVerdict.ADD  # independent facts coexist

        # R1 — trust gate (E1): markedly less-trusted writes cannot displace
        # the current fact; the store records the rejection as a CONFLICT event.
        if incoming.trust < existing.trust - options.trust_margin:
            return ConflictVerdict.NOOP

        # R1' — lower-trust contest (opt-in): inside the margin, but less trusted
        # than the current fact, so it may neither supersede nor retract it.
        if options.contest_lower_trust and incoming.trust < existing.trust - 1e-9:
            return ConflictVerdict.CONTEST

        # R2' — explicit retraction (FORK-A6): a trust-gated write tagged
        # ``retract`` on the same key ends the fact with NO successor. Tag-based,
        # so the rung is deterministic; no LLM decides what a negation is.
        if "retract" in incoming.tags:
            return ConflictVerdict.INVALIDATE

        # Graphiti overlap rule: an incoming statement whose validity interval is
        # closed and ended before the current fact began cannot contradict it.
        if incoming.valid_to is not None and incoming.valid_to <= existing.valid_from:
            return ConflictVerdict.ADD

        # R2'' — contest (H9, opt-in): nothing orders the two statements.
        if options.contest_ties:
            gap = abs((incoming.valid_from - existing.valid_from).total_seconds())
            if (
                gap <= options.contest_window_seconds
                and abs(incoming.trust - existing.trust) <= options.contest_trust_margin
            ):
                return ConflictVerdict.CONTEST

        # R3 — temporal: the biased-newer statement supersedes the current one.
        incoming_newer = incoming.valid_from >= existing.valid_from
        if options.bias == "oldest":
            incoming_newer = not incoming_newer
        if incoming_newer:
            return ConflictVerdict.UPDATE

        # R4 — historical backfill: keep the current fact, add the older one
        # with closed validity (the store sets valid_to = existing.valid_from).
        return ConflictVerdict.ADD
