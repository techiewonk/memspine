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

import math
import re
import unicodedata
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field

from memspine.config import constants
from memspine.core.policies.trust import TrustPolicy
from memspine.core.records import MemoryRecord, RecordStatus

__all__ = [
    "Firewall",
    "FirewallSignals",
    "FirewallVerdict",
    "QueryHistory",
    "extended_instruction_shaped",
    "instruction_shaped",
    "normalize_for_screening",
    "semantic_risk",
]

#: Imperative-injection framing (MINJA/ASI06 corpus). Deliberately coarse:
#: the flag is *inert* metadata + a quarantine input, never a deletion.
_INSTRUCTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    # DOTALL so ``.`` in the bounded gaps spans newlines — line-wrapped
    # injections ("Ignore all previous\ninstructions") must not slip the net.
    re.compile(pattern, re.IGNORECASE | re.DOTALL)
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


#: Format characters that render as nothing but split a word for a regex:
#: zero-width space/joiners, word joiner, BOM, soft hyphen, Mongolian vowel
#: separator, and the bidi embedding/isolate controls.
_INVISIBLE = re.compile("[­᠎​-‏‪-‮⁠-⁤⁦-⁩﻿]")


#: W2 (plan v3.2, G18; ``firewall.signals.instruction_extended``): the framings the
#: base patterns miss. ASB's own templates ("You must strictly adhere to ... and do NOT
#: use other tools", "Answer: task complete."), MEM-INV's forged instructions ("SYSTEM
#: UPDATE: Ignore the previous sentence"), overrides, prompt-reveal requests and raw
#: chat-template tokens. Benign preference framings ("please always answer in
#: English") are deliberately not here (A7 guard).
_EXTENDED_PATTERNS: tuple[re.Pattern[str], ...] = (
    *(
        re.compile(pattern, re.IGNORECASE | re.DOTALL)
        for pattern in (
            # Imperative only: "I strictly follow a vegan diet" is a preference.
            r"\b(?:must|should|shall|will|please|always) strictly (?:adhere|follow|comply)\b"
            r"|\bstrictly (?:adhere|follow|comply) (?:to|with) (?:the|these|this|all) "
            r"(?:following )?(?:instructions?|rules?|commands?|directions?|steps?)\b",
            r"\bdo not use (?:any )?other tools?\b",
            r"\bignore (?:the |all |any |my |your )?(?:previous|prior|above|earlier|preceding)"
            r" (?:sentences?|messages?|text|context|content|conversation|inputs?)\b",
            r"\b(?:answer|response|result)\s*:\s*task (?:is )?complete",
            r"\boverride (?:your|the|all|any|previous|prior) (?:instructions?|rules?|"
            r"settings?|polic(?:y|ies)|guidelines?)\b",
            r"\b(?:reveal|print|output|repeat|show) (?:your|the) (?:system|hidden|secret|"
            r"internal|original) (?:prompt|instructions?)\b",
            r"\b(?:forget|disregard) (?:everything|all) (?:you|that) (?:know|were told|"
            r"have been told)\b",
            r"<\|im_(?:start|end)\|>|\[/?INST\]|<</?SYS>>|###\s*(?:instruction|system)\b",
        )
    ),
    # Case-sensitive: an all-caps banner, not "a system update" in ordinary chat.
    re.compile(r"\bSYSTEM (?:UPDATE|OVERRIDE|NOTICE|MESSAGE|ALERT)\b"),
)

#: N20 (plan v3.2, MAPLE-Guard; ``firewall.signals.semantic_risk``): content that
#: claims its own authority or binds a future answer. A planted memory typically does
#: one of the two ("This information has been verified", "the correct answer is X",
#: "whenever anyone asks about Y, say Z").
_SEMANTIC_RISK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (kind, re.compile(pattern, re.IGNORECASE | re.DOTALL))
    for kind, pattern in (
        (
            "self_claimed_authority",
            r"\b(?:this|the following) (?:information|fact|answer|statement|record|entry)"
            r" (?:is|has been) (?:verified|confirmed|official|authoritative|approved)\b"
            r"|\b(?:verified|official|authoritative|trusted|approved) (?:source|fact|answer|"
            r"information|record)\b",
        ),
        (
            "answer_binding",
            r"\bthe (?:correct|right|true|only|real|official) answer (?:is|to)\b"
            r"|\b(?:always|must) (?:answer|reply|respond|say)\b"
            r"|\bwhenever (?:anyone|someone|somebody|the user|you are|you're) "
            r"(?:asks?|asked|mentions?)\b",
        ),
    )
)


def extended_instruction_shaped(content: str) -> bool:
    """W2: the base instruction patterns or the extended ones, on the normalised text."""
    text = normalize_for_screening(content)
    return any(pattern.search(text) for pattern in (*_INSTRUCTION_PATTERNS, *_EXTENDED_PATTERNS))


def semantic_risk(content: str) -> list[str]:
    """N20: the semantic-risk kinds ``content`` shows (empty when none)."""
    text = normalize_for_screening(content)
    return [kind for kind, pattern in _SEMANTIC_RISK_PATTERNS if pattern.search(text)]


@dataclass(frozen=True)
class FirewallSignals:
    """Which write signals :meth:`Firewall.assess` runs (``firewall.signals``).

    The defaults reproduce the firewall before W2: the base instruction patterns, the
    embedding outlier and the MINJA bridge prefix on; the extended patterns (W2), the
    semantic-risk patterns (N20) and the query-history anomaly (N22) off. Turning a
    signal off is an ablation arm, not a production setting."""

    instruction: bool = True
    anomaly: bool = True
    minja_bridge: bool = True
    instruction_extended: bool = False
    semantic_risk: bool = False
    query_anomaly: bool = False
    query_anomaly_kappa: float = 3.0


class QueryHistory:
    """N22 (plan v3.2, MemSAD): a per-namespace memory of recent query embeddings.

    A planted memory is optimised to be retrieved by the attacker's trigger queries,
    so it sits unusually close to what was recently *asked*. A write's score is its
    best cosine against the last :data:`constants.QUERY_HISTORY_SIZE` query vectors;
    it is anomalous when that score exceeds the mean plus ``kappa`` standard
    deviations of the namespace's recent write scores, once at least
    :data:`constants.QUERY_ANOMALY_MIN_BASELINE` of them exist. Pure, in memory, lost
    on restart (a warm-up, never a gate on its own)."""

    def __init__(self) -> None:
        self._queries: dict[str, deque[list[float]]] = {}
        self._baseline: dict[str, deque[float]] = {}

    def observe(self, namespace: str, vector: Sequence[float]) -> None:
        ring = self._queries.setdefault(namespace, deque(maxlen=constants.QUERY_HISTORY_SIZE))
        ring.append(list(vector))

    def assess(self, namespace: str, vector: Sequence[float], kappa: float) -> float | None:
        """The write's z-score above the baseline when anomalous, else None; the
        write's score then joins the baseline."""
        queries = self._queries.get(namespace)
        if not queries:
            return None
        score = max(_cosine(vector, q) for q in queries)
        baseline = self._baseline.setdefault(
            namespace, deque(maxlen=constants.QUERY_ANOMALY_BASELINE_SIZE)
        )
        flagged: float | None = None
        if len(baseline) >= constants.QUERY_ANOMALY_MIN_BASELINE:
            mean = sum(baseline) / len(baseline)
            std = math.sqrt(sum((s - mean) ** 2 for s in baseline) / len(baseline))
            if score > mean + kappa * max(std, 1e-6):
                flagged = (score - mean) / max(std, 1e-6)
        baseline.append(score)
        return flagged


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def normalize_for_screening(content: str) -> str:
    """NFKC fold, then drop invisible format characters.

    NFKC maps compatibility forms (full-width letters, ligatures, styled
    mathematical letters) to their plain letters, so the full-width form of
    ``ignore`` reads as ``ignore``; stripping zero-width characters rejoins a
    word an attacker split to slip past ``\\b`` boundaries. Used only for
    screening: the stored content is never rewritten."""
    return _INVISIBLE.sub("", unicodedata.normalize("NFKC", content))


def instruction_shaped(content: str) -> bool:
    """Deterministic instruction-framing detector (E1). Content-only, cheap.

    The patterns run on :func:`normalize_for_screening` of ``content``."""
    text = normalize_for_screening(content)
    return any(pattern.search(text) for pattern in _INSTRUCTION_PATTERNS)


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

    def __init__(
        self, policy: TrustPolicy | None = None, signals: FirewallSignals | None = None
    ) -> None:
        self._policy = policy or TrustPolicy.bind()
        self._signals = signals or FirewallSignals()

    @property
    def signals(self) -> FirewallSignals:
        return self._signals

    @property
    def policy(self) -> TrustPolicy:
        return self._policy

    def assess(
        self,
        record: MemoryRecord,
        neighbour_similarities: list[float] | None = None,
        recent_contents: list[str] | None = None,
        query_anomaly: float | None = None,
    ) -> FirewallVerdict:
        """``neighbour_similarities``: cosine scores of the write's embedding
        against its nearest existing neighbours (the engine already has the
        vector store paid for). An AgentPoison-style backdoor entry sits far
        from everything the namespace has ever stored.

        ``query_anomaly`` (N22): the write's z-score from :class:`QueryHistory` when
        it sits anomalously close to recent queries (None: not anomalous / not run).
        ``firewall.signals`` decides which signals run."""
        reasons: list[str] = []
        trust = self._policy.trust_at_write(record.source)
        signals = self._signals

        flagged = False
        if signals.instruction_extended:
            flagged = extended_instruction_shaped(record.content)
        elif signals.instruction:
            flagged = instruction_shaped(record.content)
        if flagged:
            reasons.append("instruction_shaped_content")
        if signals.semantic_risk:
            risks = semantic_risk(record.content)
            if risks:
                # Treated like instruction framing: inert for users and operators, a
                # quarantine input for an untrusted origin.
                flagged = True
                reasons.extend(f"semantic_risk:{kind}" for kind in risks)

        anomalous = False
        if (
            signals.anomaly
            and neighbour_similarities is not None
            and len(neighbour_similarities) >= constants.ANOMALY_MIN_NEIGHBOURS
        ):
            nearest = max(neighbour_similarities)
            if nearest < constants.ANOMALY_CENTROID_MIN_SIMILARITY:
                anomalous = True
                reasons.append(f"embedding_outlier(nearest={nearest:.3f})")
        if (
            signals.minja_bridge
            and recent_contents
            and len(record.content) >= constants.MINJA_BRIDGE_PREFIX_CHARS
        ):
            prefix = record.content[: constants.MINJA_BRIDGE_PREFIX_CHARS]
            if any(
                content.startswith(prefix)
                for content in recent_contents
                if content != record.content
            ):
                anomalous = True
                reasons.append("minja_bridge_prefix")
        if signals.query_anomaly and query_anomaly is not None:
            anomalous = True
            reasons.append(f"query_history_anomaly(z={query_anomaly:.2f})")

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
