"""Memory Firewall trust policy (E1 / M17, OWASP ASI06) — pure decision logic.

Trust is assigned at write time from *where the content came from*, never from
what it says: a (role x channel) matrix with a hard cap on retrieved/external
channels so retrieved content can never masquerade as operator input (the MINJA
/ AgentPoison precondition). Quarantine and promotion decisions are pure
functions here; the I/O-bound anomaly signals live in ``core/firewall.py`` and
are passed in as booleans.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar, Literal

from pydantic import Field, field_validator

from memspine.config import constants
from memspine.core.policies.base import BindablePolicy, PolicyOptions
from memspine.core.records import MemoryRecord, SourceInfo

__all__ = ["EvidenceVerdict", "TrustPolicy"]

EvidenceVerdict = Literal["promote", "disputed", "pending_evidence"]

#: Base trust per source role (who authored it). Operators outrank agents;
#: anything an LLM or tool produced starts midfield at best.
_ROLE_TRUST: dict[str, float] = {
    "operator": 0.9,
    "system": 0.9,
    "user": 0.7,
    "assistant": 0.5,
    "tool": 0.4,
}

#: Channels whose content originated OUTSIDE the trust boundary — retrieved
#: documents, web pages, third-party messages, INGESTED FILES, and REST callers
#: (the no-authn REST protocol forces this channel so a caller-supplied role can
#: never escalate trust — ADR-018/SEC-C1). Capped, never boosted: an ingested
#: PDF is exactly the RAG poisoning surface E1 defends. ``agent_tool`` (I65) is a
#: model's own memory tool call: its text is model output, capped like the rest.
_EXTERNAL_CHANNELS = frozenset({"retrieved", "web", "external", "email", "mcp", "ingest", "rest", "agent_tool"})


class TrustOptions(PolicyOptions):
    # Bounded fields: a config override outside [0, 1] (or a zero promotion
    # floor) must be a loud ConfigError, never a silent cap defeat (E1).
    default_trust: float = Field(default=constants.TRUST_DEFAULT, ge=0.0, le=1.0)
    retrieved_content_cap: float = Field(default=constants.TRUST_RETRIEVED_CAP, ge=0.0, le=1.0)
    quarantine_below: float = Field(default=constants.QUARANTINE_TRUST_THRESHOLD, ge=0.0, le=1.0)
    corroborations_to_promote: int = Field(
        default=constants.QUARANTINE_PROMOTION_CORROBORATIONS, ge=1
    )
    role_trust: dict[str, float] = Field(default_factory=lambda: dict(_ROLE_TRUST))
    #: N26 (plan v3.2, ADR-060): document type -> authority tier (3 register /
    #: official, 2 news or web, 1 media, 0 agent report or memory recall). A record's
    #: type is its ``doctype:<t>`` tag, else its source channel. A typed record's trust
    #: is capped at ``SOURCE_TIER_TRUST_CAP[tier]``. Empty (default): no tiers.
    source_types: dict[str, int] = Field(default_factory=dict)
    #: N26: hold a non-privileged write whose tier is below ``authority_min_tier`` in
    #: quarantine with a ``pending_evidence`` reason until authoritative support
    #: arrives (``Engine.review_evidence``). Off by default.
    hold_needs_evidence: bool = False
    authority_min_tier: int = Field(default=constants.EVIDENCE_AUTHORITY_MIN_TIER, ge=0, le=3)

    @field_validator("source_types")
    @classmethod
    def _tiers_in_range(cls, value: dict[str, int]) -> dict[str, int]:
        bad = {name: tier for name, tier in value.items() if tier not in (0, 1, 2, 3)}
        if bad:
            raise ValueError(f"source_types tiers must be 0, 1, 2 or 3: {bad}")
        return value

    @field_validator("role_trust")
    @classmethod
    def _roles_in_unit_interval(cls, value: dict[str, float]) -> dict[str, float]:
        bad = {role: t for role, t in value.items() if not 0.0 <= t <= 1.0}
        if bad:
            raise ValueError(f"role_trust values must be in [0, 1]: {bad}")
        return value


class TrustPolicy(BindablePolicy):
    name: ClassVar[str] = "trust"
    Options: ClassVar[type[PolicyOptions]] = TrustOptions

    def _options(self) -> TrustOptions:
        options = self.options
        assert isinstance(options, TrustOptions)
        return options

    def trust_at_write(self, source: SourceInfo) -> float:
        """Source class x channel → trust in [0, 1]. External channels are
        capped regardless of the claimed role — a web page that says it is
        the operator is still a web page (E1)."""
        options = self._options()
        trust = options.role_trust.get(source.role, options.default_trust)
        if source.channel in _EXTERNAL_CHANNELS:
            trust = min(trust, options.retrieved_content_cap)
        return trust

    def source_tier(self, record: MemoryRecord) -> int | None:
        """N26: the record's document-type authority tier, None when untyped."""
        types = self._options().source_types
        if not types:
            return None
        for tag in record.tags:
            if tag.startswith(constants.DOCTYPE_TAG_PREFIX):
                tier = types.get(tag[len(constants.DOCTYPE_TAG_PREFIX) :])
                if tier is not None:
                    return tier
        return types.get(record.source.channel)

    def tier_capped(self, trust: float, record: MemoryRecord) -> float:
        """N26: ``trust`` capped by the record's document-type tier (unchanged untyped)."""
        tier = self.source_tier(record)
        return trust if tier is None else min(trust, constants.SOURCE_TIER_TRUST_CAP[tier])

    def needs_evidence(self, record: MemoryRecord) -> bool:
        """N26: hold this write until authoritative support arrives?"""
        options = self._options()
        if not options.hold_needs_evidence or record.source.role in ("operator", "system"):
            return False
        tier = self.source_tier(record)
        return tier is not None and tier < options.authority_min_tier

    def evidence_verdict(
        self, supporter_tiers: Sequence[int | None], contradicted: bool
    ) -> EvidenceVerdict:
        """N26 (CPB b7 governance): ``promote`` on at least one authoritative
        supporter; ``disputed`` when authoritative support meets a contradiction;
        else ``pending_evidence``."""
        floor = self._options().authority_min_tier
        supported = any(tier is not None and tier >= floor for tier in supporter_tiers)
        if not supported:
            return "pending_evidence"
        return "disputed" if contradicted else "promote"

    def should_quarantine(
        self,
        record: MemoryRecord,
        anomalous: bool = False,
        instruction_shaped: bool = False,
    ) -> bool:
        """Quarantine on: rock-bottom trust, an anomaly signal on a
        non-privileged write, or instruction-shaped content from an
        *untrusted origin* (external channel, or tool/assistant authorship —
        the MINJA injection vectors). Operators and users legitimately write
        imperatives; for them the flag stays inert metadata."""
        options = self._options()
        if record.trust < options.quarantine_below:
            return True
        privileged = record.source.role in ("operator", "system")
        if anomalous and not privileged:
            return True
        if instruction_shaped:
            return record.source.channel in _EXTERNAL_CHANNELS or record.source.role in (
                "tool",
                "assistant",
            )
        return False

    def may_promote(self, record: MemoryRecord) -> bool:
        """A quarantined record earns activation once independently
        corroborated by enough trusted writes (E1)."""
        return record.corroborations >= self._options().corroborations_to_promote
