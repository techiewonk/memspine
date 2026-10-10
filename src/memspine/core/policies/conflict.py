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
- W6 merge:      (opt-in, ``merge_containment``) every content word of the
                 incoming value is already in the current one: a restatement,
                 not a new value                               → NOOP
- R4 backfill:   incoming is older than the current fact         → ADD (historical,
                 store closes its validity at existing.valid_from). With the
                 Graphiti overlap rule, a closed interval that ended before the
                 current fact began never displaces it (also ADD).

#19 interval arithmetic (opt-in, ``interval_order``): the verdicts are unchanged;
the store applies them with world-time intervals. A superseded or retracted fact
gets ``invalid_at`` = the next statement's ``valid_from``; an older-arriving
contradiction is stored as history ending at the next statement on its key (not
always the current fact) and closes the history entry it lands inside. Candidate
split first: a statement with the same endpoints as the current fact (same key,
same ``dst:`` tag) is a duplicate, merged, never a contradiction.
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from memspine.core.policies.base import BindablePolicy, PolicyOptions
from memspine.core.query_shape import content_words
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
    #: #19: world-time interval arithmetic for out-of-order statements on one key
    #: (``invalid_at``, history re-closing, same-endpoint duplicates). Off = the
    #: plain R4 backfill (closed at the current fact's start, no ``invalid_at``).
    interval_order: bool = False
    #: W6 (plan v3.2): a same-key write whose content words are all in the current
    #: fact's ("Jon works at the bank" after "Jon works as a teller at the city bank")
    #: is a restatement: NOOP instead of superseding the richer fact. Off: unchanged.
    merge_containment: bool = False
    #: I55 (perspective layer): the conflict key also carries the subject, scope and polarity
    #: tags (``sub:`` / ``scope:`` / ``pol:``). Two same-key statements about different
    #: subjects or of different scope (an event vs a standing preference) coexist (ADD); a
    #: polarity flip on the same subject and scope is a CONTEST (both kept, tagged disputed)
    #: unless trust or time already decides it as the ladder does. Records without those tags
    #: behave as before. Off: unchanged.
    perspective_key: bool = False


class ConflictPolicy(BindablePolicy):
    name: ClassVar[str] = "conflict"
    Options: ClassVar[type[PolicyOptions]] = ConflictOptions

    @property
    def interval_order(self) -> bool:
        """#19: whether the store applies verdicts with world-time intervals."""
        options = self.options
        assert isinstance(options, ConflictOptions)
        return options.interval_order

    @property
    def perspective_key(self) -> bool:
        """I55: whether the key also carries subject and scope (``sub:`` / ``scope:`` tags)."""
        options = self.options
        assert isinstance(options, ConflictOptions)
        return options.perspective_key

    @staticmethod
    def coexists(incoming: MemoryRecord, existing: MemoryRecord) -> bool:
        """I55: two same-key statements about different subjects, or of different scope
        (an event vs a standing preference), both hold: neither supersedes the other."""
        for prefix in ("sub:", "scope:"):
            a = {t for t in incoming.tags if t.startswith(prefix)}
            b = {t for t in existing.tags if t.startswith(prefix)}
            if a and b and a != b:
                return True
        return False

    @staticmethod
    def same_endpoints(incoming: MemoryRecord, existing: MemoryRecord) -> bool:
        """#19 candidate split: two keyed statements naming the same destination
        (``dst:`` tag, written on every edge fact) state the same edge, so they are
        a duplicate pair, not a contradiction. Records without a ``dst:`` tag never
        match (a plain keyed fact has no endpoint to compare)."""
        if incoming.entity is None or incoming.attribute is None:
            return False
        if (incoming.entity, incoming.attribute) != (existing.entity, existing.attribute):
            return False
        mine = {t for t in incoming.tags if t.startswith("dst:")}
        return bool(mine) and mine == {t for t in existing.tags if t.startswith("dst:")}

    def trust_gated(self, incoming: MemoryRecord, existing: MemoryRecord) -> bool:
        """R1: ``incoming`` is markedly less trusted than ``existing`` (by more
        than ``trust_margin``), so it may not change the current fact at all —
        not even as a reinforcing duplicate."""
        options = self.options
        assert isinstance(options, ConflictOptions)
        return incoming.trust < existing.trust - options.trust_margin

    def resolve(self, incoming: MemoryRecord, existing: MemoryRecord) -> ConflictVerdict:
        options = self.options
        assert isinstance(options, ConflictOptions)

        # R0 — identical statement: nothing to do.
        if incoming.content_fingerprint == existing.content_fingerprint:
            return ConflictVerdict.NOOP

        # B-1: an attribute-less record has no (entity, attribute) key, so two
        # such records about one entity are independent facts, not a conflict.
        same_key = (
            incoming.entity is not None
            and incoming.attribute is not None
            and incoming.entity == existing.entity
            and incoming.attribute == existing.attribute
        )
        if not same_key:
            return ConflictVerdict.ADD  # independent facts coexist
        if options.perspective_key and self.coexists(incoming, existing):
            return ConflictVerdict.ADD  # another subject / scope: both hold

        # R1 — trust gate (E1): markedly less-trusted writes cannot displace
        # the current fact; the store records the rejection as a CONFLICT event.
        if self.trust_gated(incoming, existing):
            return ConflictVerdict.NOOP

        if options.perspective_key:
            pol_in = {t for t in incoming.tags if t.startswith("pol:")}
            pol_ex = {t for t in existing.tags if t.startswith("pol:")}
            if pol_in and pol_ex and pol_in != pol_ex and len(pol_in) == len(pol_ex) == 1:
                return ConflictVerdict.CONTEST  # a polarity flip on one key
        # R1' — lower-trust contest (opt-in): inside the margin, but less trusted
        # than the current fact, so it may neither supersede nor retract it.
        if options.contest_lower_trust and incoming.trust < existing.trust - 1e-9:
            return ConflictVerdict.CONTEST

        # R2' — explicit retraction (FORK-A6): a trust-gated write tagged
        # ``retract`` on the same key ends the fact with NO successor. Tag-based,
        # so the rung is deterministic; no LLM decides what a negation is.
        if "retract" in incoming.tags:
            return ConflictVerdict.INVALIDATE

        # W6 merge (opt-in): a restatement adds no new value.
        if options.merge_containment:
            mine = content_words(incoming.content)
            if mine and mine <= content_words(existing.content):
                return ConflictVerdict.NOOP

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
