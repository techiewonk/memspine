"""Conflict policy (M4): bi-temporal verdicts over (entity, attribute) keys.

Deterministic R-ladder — pure decision logic, no I/O; the semantic store acts
on the verdict. The optional LLM judge (judge prompt, D-43) is an escalation
the store may apply to AMBIGUOUS outcomes; the ladder itself never needs it.

Ladder (evaluated on two records sharing a fact key):

- R0 identity:   same content fingerprint                        → NOOP
- R1 trust gate: incoming markedly less trusted than existing    → NOOP (E1 seam)
- R2 authority:  source-authority comparison (shared memory)     → P7
- R3 temporal:   incoming is newer (per ``bias``)                → UPDATE
- R4 backfill:   incoming is older than the current fact         → ADD (historical,
                 store closes its validity at existing.valid_from)
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


class ConflictOptions(PolicyOptions):
    bias: str = "newest"  # R3 default: latest valid_from wins; "oldest" inverts
    trust_margin: float = 0.3  # R1: reject when incoming.trust < existing - margin


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

        # R3 — temporal: the biased-newer statement supersedes the current one.
        incoming_newer = incoming.valid_from >= existing.valid_from
        if options.bias == "oldest":
            incoming_newer = not incoming_newer
        if incoming_newer:
            return ConflictVerdict.UPDATE

        # R4 — historical backfill: keep the current fact, add the older one
        # with closed validity (the store sets valid_to = existing.valid_from).
        return ConflictVerdict.ADD
