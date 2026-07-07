"""Memory Firewall trust policy (E1 / M17, OWASP ASI06) — pure decision logic.

Trust is assigned at write time from *where the content came from*, never from
what it says: a (role x channel) matrix with a hard cap on retrieved/external
channels so retrieved content can never masquerade as operator input (the MINJA
/ AgentPoison precondition). Quarantine and promotion decisions are pure
functions here; the I/O-bound anomaly signals live in ``core/firewall.py`` and
are passed in as booleans.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from memspine.config import constants
from memspine.core.policies.base import BindablePolicy, PolicyOptions
from memspine.core.records import MemoryRecord, SourceInfo

__all__ = ["TrustPolicy"]

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
#: documents, web pages, third-party messages, INGESTED FILES. Capped, never
#: boosted: an ingested PDF is exactly the RAG poisoning surface E1 defends.
_EXTERNAL_CHANNELS = frozenset({"retrieved", "web", "external", "email", "mcp", "ingest"})


class TrustOptions(PolicyOptions):
    default_trust: float = constants.TRUST_DEFAULT
    retrieved_content_cap: float = constants.TRUST_RETRIEVED_CAP
    quarantine_below: float = constants.QUARANTINE_TRUST_THRESHOLD
    corroborations_to_promote: int = constants.QUARANTINE_PROMOTION_CORROBORATIONS
    role_trust: dict[str, float] = Field(default_factory=lambda: dict(_ROLE_TRUST))


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
