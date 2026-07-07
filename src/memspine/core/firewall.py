"""Memory Firewall orchestration (E1 / M17): the write-path gate.

Combines three *deterministic* signals — no LLM in the loop, so the defense
cannot itself be prompt-injected:

1. **Trust matrix** (``TrustPolicy``): source role x channel, external capped.
2. **Instruction-shaped-content flag**: regex heuristics for imperative
   injection framing. Flagged content is stored *inert* (``instruction_flag``)
   so assembly can wrap or exclude it; flag + non-privileged source ⇒
   quarantine.
3. **Write-path anomaly detection**: embedding outlier vs. the namespace
   centroid (AgentPoison plants backdoor outliers) + MINJA bridging heuristic
   (progressive injections share long content prefixes with recent writes).

The engine calls :meth:`Firewall.assess` before the write door; the verdict
travels on the record (trust / quarantined / instruction_flag columns, P0 DDL)
so every later consumer — retrieval, consolidation, dedup — reads one row.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from memspine.config import constants
from memspine.core.policies.trust import TrustPolicy
from memspine.core.records import MemoryRecord, RecordStatus

__all__ = ["Firewall", "FirewallVerdict", "instruction_shaped"]

#: Imperative-injection framing (MINJA/ASI06 corpus). Deliberately coarse:
#: the flag is *inert* metadata + a quarantine input, never a deletion.
_INSTRUCTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bignore (all|any|the|previous|prior|above)\b.{0,40}\b(instruction|prompt|rule)",
        r"\bdisregard\b.{0,40}\b(instruction|prompt|rule|polic)",
        r"\byou (must|should|will) (always|never|now)\b",
        r"\bfrom now on\b",
        r"\bnew (system )?instructions?\b",
        r"\bdo not (tell|inform|reveal|mention)\b",
        r"\b(system|developer) prompt\b",
        r"\bwhen (the user|asked about .{1,60}),? (always|never|say|respond|reply)\b",
        r"</?(system|instructions?|admin)>",
        r"\brespond with\b.{0,60}\bexactly\b",
    )
)


def instruction_shaped(content: str) -> bool:
    """Deterministic instruction-framing detector (E1). Content-only, cheap."""
    return any(pattern.search(content) for pattern in _INSTRUCTION_PATTERNS)


@dataclass
class FirewallVerdict:
    trust: float
    instruction_flag: bool = False
    anomalous: bool = False
    quarantine: bool = False
    reasons: list[str] = field(default_factory=list)

    def apply(self, record: MemoryRecord) -> MemoryRecord:
        """Stamp the verdict onto the record (columns are P0 DDL, E1)."""
        update: dict[str, object] = {
            "trust": self.trust,
            "instruction_flag": self.instruction_flag,
        }
        if self.quarantine:
            update["quarantined"] = True
            update["status"] = RecordStatus.QUARANTINED
        return record.model_copy(update=update)


class Firewall:
    """Write-path gate. Pure given its inputs — the engine supplies the
    namespace context (recent contents + vectors) it has already paid for."""

    def __init__(self, policy: TrustPolicy | None = None) -> None:
        self._policy = policy or TrustPolicy.bind()

    @property
    def policy(self) -> TrustPolicy:
        return self._policy

    def assess(
        self,
        record: MemoryRecord,
        neighbour_similarities: list[float] | None = None,
        recent_contents: list[str] | None = None,
    ) -> FirewallVerdict:
        """``neighbour_similarities``: cosine scores of the write's embedding
        against its nearest existing neighbours (the engine already has the
        vector store paid for). An AgentPoison-style backdoor entry sits far
        from everything the namespace has ever stored."""
        reasons: list[str] = []
        trust = self._policy.trust_at_write(record.source)

        flagged = instruction_shaped(record.content)
        if flagged:
            reasons.append("instruction_shaped_content")

        anomalous = False
        if (
            neighbour_similarities is not None
            and len(neighbour_similarities) >= constants.ANOMALY_MIN_NEIGHBOURS
        ):
            nearest = max(neighbour_similarities)
            if nearest < constants.ANOMALY_CENTROID_MIN_SIMILARITY:
                anomalous = True
                reasons.append(f"embedding_outlier(nearest={nearest:.3f})")
        if recent_contents and len(record.content) >= constants.MINJA_BRIDGE_PREFIX_CHARS:
            prefix = record.content[: constants.MINJA_BRIDGE_PREFIX_CHARS]
            if any(
                content.startswith(prefix)
                for content in recent_contents
                if content != record.content
            ):
                anomalous = True
                reasons.append("minja_bridge_prefix")

        stamped = record.model_copy(update={"trust": trust})
        quarantine = self._policy.should_quarantine(
            stamped, anomalous=anomalous, instruction_shaped=flagged
        )
        if quarantine:
            reasons.append("quarantined")
        return FirewallVerdict(
            trust=trust,
            instruction_flag=flagged,
            anomalous=anomalous,
            quarantine=quarantine,
            reasons=reasons,
        )
