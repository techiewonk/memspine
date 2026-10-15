"""The Memory Firewall at three gates: insert, derive, read (LIB-38, W-07, G-06).

Pure and deterministic: no I/O, no model calls, no clock. Not wired yet (W2-04 wires it). This is
the N1 Memory Firewall rebuilt as a library: today's ``trust.firewall`` semantics, kept, plus the
two later gates.

* **Gate 1, :func:`gate_insert`** (before a raw write is accepted into derivation). Runs a stack of
  deterministic :class:`Signal` objects over text and metadata, then applies the old decision rule:
  quarantine on rock-bottom trust, on an anomaly signal for a non-privileged writer, or on
  instruction-shaped content from an untrusted origin (external channel, tool or assistant). For
  users and operators an instruction flag stays inert metadata. A quarantined write is still
  stored (inert); ``reject`` exists but is off unless the policy turns it on.
* **Gate 3, :func:`gate_derive`** (before a derived record is admitted). Same signals on the derived
  text, and the trust cap is unconditional (POL-2, G-08): ``tau <= min(base, firewall trust, every
  parent's view trust)``. A quarantined, unreadable or missing parent holds the derived record:
  a poisoned turn cannot be laundered into a clean-looking fact. The derived record also reports
  its source families and independent support (ER-17: an echo or summary is not independent).
* **Gate 2** (input filter for extraction) is :func:`gate_derive_input`: quarantined turns are
  excluded and every kept block is marked as data.
* **Gate 4 on the read side, :func:`gate_read`** (before evidence is rendered). A quarantined
  record is never rendered unless explicitly requested, and then only with a label. Consent
  (LIB-18) and the grant chain (LIB-21: visibility, horizon, attenuation) decide the rest.
  Numeric trust is never access control (POL-2): visibility is checked first.
* **Release (S4-4)** is :func:`release_quarantine`, which calls LIB-08 ``corroboration`` and does not
  re-implement it.

Signals are pure functions over ``(text, meta)``. Base signals: instruction (en/de/nl/es), anomaly
(contradiction of a high-trust value by a low-trust source, claim rate, embedding outlier), MINJA
bridge (fake recall phrasing, repeated prefix) and a secrets/PII presence flag whose detector the
caller supplies. Extended signals (wider instruction patterns, semantic risk, query-history anomaly)
and any caller signal plug into the same stack.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import ConfigDict, Field

from memspine.schemas import InternalModel
from memspine.schemas.durable import LineageEdge
from memspine.trust.consent import (
    PURPOSE_ANY,
    ConsentRegistry,
    RecordMeta,
    Viewer,
    may_read,
)
from memspine.trust.families import (
    DEFAULT_TRUST,
    CorroborationDecision,
    FamilyId,
    StoredRecord,
    corroboration,
    families_of,
    is_independent_family,
)
from memspine.trust.horizon import Grant, TrustedRecord, deposit_trust, effective_trust

__all__ = [
    "AnomalySignal",
    "DerivedMeta",
    "GatePolicy",
    "GateVerdict",
    "InstructionSignal",
    "MinjaBridgeSignal",
    "ParentRef",
    "QueryAnomalySignal",
    "ReadItem",
    "ReleaseVerdict",
    "SecretsSignal",
    "SemanticRiskSignal",
    "Signal",
    "SignalConfig",
    "SignalResult",
    "TurnMeta",
    "build_signals",
    "gate_derive",
    "gate_derive_input",
    "gate_insert",
    "gate_read",
    "instruction_hits",
    "normalize_for_screening",
    "recall_bridge_hits",
    "release_quarantine",
    "semantic_risk_kinds",
]

Action = Literal["accept", "quarantine", "reject"]
SignalKind = Literal["instruction", "anomaly", "info"]

#: Channels whose content originated outside the trust boundary (as ``core.policies.trust``).
EXTERNAL_CHANNELS: frozenset[str] = frozenset(
    {"retrieved", "web", "external", "email", "mcp", "ingest", "rest", "agent_tool"}
)

_ROLE_TRUST: dict[str, float] = {
    "operator": 0.9,
    "system": 0.9,
    "user": 0.7,
    "assistant": 0.5,
    "tool": 0.4,
}


# --------------------------------------------------------------------------- types


class _Frozen(InternalModel):
    model_config = ConfigDict(frozen=True)


class GatePolicy(_Frozen):
    """Thresholds the decision rule reads. Defaults reproduce ``core.policies.trust``."""

    role_trust: dict[str, float] = Field(default_factory=lambda: dict(_ROLE_TRUST))
    default_trust: float = Field(default=0.5, ge=0.0, le=1.0)
    retrieved_cap: float = Field(default=0.3, ge=0.0, le=1.0)
    quarantine_below: float = Field(default=0.25, ge=0.0, le=1.0)
    #: Roles whose writes are never quarantined for an anomaly.
    privileged_roles: frozenset[str] = frozenset({"operator", "system"})
    #: Roles whose imperative content is an injection vector (MINJA), besides external channels.
    untrusted_roles: frozenset[str] = frozenset({"tool", "assistant", "derived"})
    external_channels: frozenset[str] = EXTERNAL_CHANNELS
    #: Off by default: the firewall quarantines, it does not discard (N1 forensics need the text).
    reject_below: float | None = Field(default=None, ge=0.0, le=1.0)
    reject_on_secrets: bool = False
    #: Per-parent attenuation of a derived record at gate 3 (derivation decay, 1.0 = none).
    derivation_decay: float = Field(default=1.0, gt=0.0, le=1.0)


class TurnMeta(_Frozen):
    """What gate 1 knows about a raw write. ``trust=None`` takes the role x channel matrix."""

    record_id: str = ""
    role: str = "user"
    channel: str = "chat"
    trust: float | None = Field(default=None, ge=0.0, le=1.0)
    family: str = ""
    #: Up to ~50 recent live contents of the namespace (prefix-repeat bridge).
    recent_contents: tuple[str, ...] = ()
    #: Cosine of this write against its nearest existing neighbours (embedding outlier).
    neighbour_similarities: tuple[float, ...] = ()
    #: Trust of the highest-trust held value this text contradicts (caller-resolved), if any.
    contradicts_trust: float | None = Field(default=None, ge=0.0, le=1.0)
    #: New claims from this source family inside the current rate window.
    claims_in_window: int = Field(default=0, ge=0)
    #: True when the caller verified that the assistant really said what a recall phrase claims.
    recall_verified: bool = False
    #: MemSAD z-score from a query-history tracker (None: not run / not anomalous).
    query_anomaly_z: float | None = None


class SignalResult(_Frozen):
    name: str
    kind: SignalKind
    score: float = Field(ge=0.0, le=1.0)
    flagged: bool
    reasons: tuple[str, ...] = ()


class Signal(Protocol):
    """A deterministic screen. ``kind`` decides how the gate treats a flag."""

    name: str
    kind: SignalKind

    def evaluate(self, text: str, meta: TurnMeta) -> SignalResult: ...


class GateVerdict(_Frozen):
    """The outcome of one gate, on the record: the schema ``FirewallVerdict`` plus ``gate``."""

    gate: Literal[1, 2, 3, 4]
    action: Action
    trust: float = Field(ge=0.0, le=1.0)
    instruction_flag: bool = False
    anomalous: bool = False
    sensitive: bool = False
    reasons: tuple[str, ...] = ()
    scores: tuple[tuple[str, float], ...] = ()
    signals_run: tuple[str, ...] = ()
    #: Render label: a quarantined record explicitly requested at read carries ``"quarantined"``;
    #: an instruction-flagged one carries ``"inert"`` (shown as data, never as instruction).
    label: str | None = None
    #: Gate 3: source families the derived record speaks for, and how many are independent (ER-17).
    families: tuple[str, ...] = ()
    independent_support: int = 0

    @property
    def quarantine(self) -> bool:
        return self.action == "quarantine"

    @property
    def accepted(self) -> bool:
        return self.action == "accept"


# --------------------------------------------------------------------------- screening text

#: Format characters that render as nothing but split a word for a regex.
_INVISIBLE = re.compile("[­᠎​-‏‪-‮⁠-⁤⁦-⁩﻿]")


def normalize_for_screening(content: str) -> str:
    """NFKC fold, then drop invisible format characters. Used for screening only."""
    return _INVISIBLE.sub("", unicodedata.normalize("NFKC", content))


def _compile(patterns: Iterable[str]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p, re.IGNORECASE | re.DOTALL) for p in patterns)


_BASE_EN = _compile(
    (
        r"\bignore (all|any|the|previous|prior|above)\b.{0,40}\b(instruction|prompt|rule)",
        r"\bdisregard\b.{0,40}\b(instruction|prompt|rule|polic)",
        r"\byou (must|should|will) (always|never|now)\b",
        # tightened from the bare "from now on": "From now on I eat vegan" is a statement, not an order
        r"\bfrom now on,? (?:you|your|ignore|respond|reply|answer|act as|treat|assume|the assistant|the system|always (?:answer|reply|respond|say))\b",
        r"\bnew (system )?instructions?\b",
        r"\bdo not (tell|inform|reveal|mention)\b",
        r"\b(system|developer) prompt\b",
        r"\bwhen (the user|asked about .{1,60}),? (always|never|say|respond|reply)\b",
        r"</?(system|instructions?|admin)>",
        r"\brespond with\b.{0,60}\bexactly\b",
    )
)

_BASE_DE = _compile(
    (
        r"\bignorier(?:e|en|t)\b.{0,40}\b(?:anweisung|instruktion|regel|vorgabe)",
        r"\bvergiss (?:alle|alles|die|deine)\b.{0,30}\b(?:anweisung\w*|regeln?|instruktion\w*|vorherige\w*|bisherige\w*)",
        r"\bab (?:jetzt|sofort),? (?:musst|sollst|antworte|ignoriere|sag)\b",
        r"\bdu (?:musst|sollst) (?:immer|nie|niemals|ab jetzt)\b",
        r"\bsag(?:e)? (?:dem|der) (?:nutzer|benutzer|user)\w*\s+(?:nicht|nichts)\b",
        r"\bneue anweisung(?:en)?\b",
        r"\bsystem-?prompt\b",
    )
)

_BASE_NL = _compile(
    (
        r"\bnegeer\b.{0,40}\b(?:instructie|opdracht|regel|voorgaande|eerdere|vorige)",
        r"\bvergeet (?:alle|alles|de|je)\b.{0,30}\b(?:instructie\w*|regels?|eerdere|vorige|voorgaande)",
        r"\bvanaf nu,? (?:moet je|moet u|zul je|zult u|antwoord|negeer|zeg)\b",
        r"\b(?:je|u) moet (?:altijd|nooit|vanaf nu)\b",
        r"\b(?:vertel|zeg) (?:de|het) gebruiker\w*\s+(?:niet|niets)\b",
        r"\bnieuwe instructies?\b",
        r"\bsysteemprompt\b",
    )
)

_BASE_ES = _compile(
    (
        r"\bignora\b.{0,40}\binstruccion\w*",
        r"\bolvida (?:todo|todas|las)\b.{0,30}\b(?:instruccion\w*|reglas?|anterior\w*)",
        r"\ba partir de ahora,? (?:debes|tienes que|responde|ignora|di)\b",
        r"\b(?:debes|tienes que) (?:siempre|nunca|a partir de ahora)\b",
        r"\bno (?:le )?(?:digas|reveles|menciones|informes)\b.{0,20}\busuario",
        r"\bnuevas instrucciones\b",
    )
)

_BASE_PATTERNS: tuple[re.Pattern[str], ...] = (*_BASE_EN, *_BASE_DE, *_BASE_NL, *_BASE_ES)

#: The framings the base set misses (benchmark templates, forged system banners, template tokens).
#: Benign preference framings ("please always answer in English") are deliberately not here.
_EXTENDED_PATTERNS: tuple[re.Pattern[str], ...] = (
    *_compile(
        (
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
    # case-sensitive: an all-caps banner, not "a system update" in ordinary chat
    re.compile(r"\bSYSTEM (?:UPDATE|OVERRIDE|NOTICE|MESSAGE|ALERT)\b"),
)

#: Content that claims its own authority or binds a future answer (a planted memory does one).
_SEMANTIC_RISK: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
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

#: MINJA-style bridge: a turn that plants a memory through fake recall phrasing.
_RECALL_BRIDGE: tuple[re.Pattern[str], ...] = _compile(
    (
        r"\b(?:remember|recall|don't forget|do not forget|keep in mind)(?: that)?,? you (?:told|said|promised|confirmed|agreed|stated|mentioned|assured)\b",
        r"\bas you (?:told|said|promised|confirmed|agreed|stated|mentioned)(?: me)? (?:earlier|before|previously|last time|yesterday|in our (?:last|previous|earlier) (?:chat|conversation|session))\b",
        r"\byou (?:previously|already|earlier) (?:told|said|promised|confirmed|agreed|stated)(?: me)?\b",
        r"\b(?:in|during) our (?:last|previous|earlier) (?:chat|conversation|session),? you (?:told|said|promised|confirmed|agreed)\b",
        r"\b(?:erinnere dich|denk daran|vergiss nicht)\b.{0,20}\bdu hast (?:mir )?(?:gesagt|versprochen|bestätigt|zugesagt)\b",
        r"\bwie du (?:mir )?(?:vorhin|früher|zuvor|gestern|letztes mal) (?:gesagt|versprochen|bestätigt) hast\b",
        r"\b(?:onthoud|denk eraan)\b.{0,20}\bje hebt (?:me |mij )?(?:verteld|gezegd|beloofd|bevestigd)\b",
        r"\bzoals je (?:me |mij )?(?:eerder|vorige keer|gisteren) (?:verteld|gezegd|beloofd|bevestigd) hebt\b",
        r"\b(?:recuerda|no olvides)\b.{0,20}\b(?:me dijiste|me prometiste|me confirmaste)\b",
        r"\bcomo (?:me )?(?:dijiste|prometiste|confirmaste) (?:antes|ayer|la última vez)\b",
    )
)


def instruction_hits(text: str, *, extended: bool = False) -> tuple[str, ...]:
    """Pattern sources that match the normalised ``text`` (empty when none), in pattern order."""
    norm = normalize_for_screening(text)
    pats = (*_BASE_PATTERNS, *_EXTENDED_PATTERNS) if extended else _BASE_PATTERNS
    return tuple(p.pattern for p in pats if p.search(norm))


def semantic_risk_kinds(text: str) -> tuple[str, ...]:
    norm = normalize_for_screening(text)
    return tuple(kind for kind, p in _SEMANTIC_RISK if p.search(norm))


def recall_bridge_hits(text: str) -> tuple[str, ...]:
    norm = normalize_for_screening(text)
    return tuple(p.pattern for p in _RECALL_BRIDGE if p.search(norm))


def _grade(n: int) -> float:
    """0 hits -> 0; 1 -> 0.5; each further hit adds 0.25, capped at 1."""
    return 0.0 if n <= 0 else min(1.0, 0.5 + 0.25 * (n - 1))


# --------------------------------------------------------------------------- signals


@dataclass(frozen=True)
class InstructionSignal:
    """Imperatives addressed to an assistant or system (en/de/nl/es). ``extended`` widens the set."""

    extended: bool = False
    name: str = "instruction"
    kind: SignalKind = "instruction"

    def evaluate(self, text: str, meta: TurnMeta) -> SignalResult:
        hits = instruction_hits(text, extended=self.extended)
        reasons = ("instruction_shaped_content",) if hits else ()
        return SignalResult(
            name=self.name,
            kind=self.kind,
            score=_grade(len(hits)),
            flagged=bool(hits),
            reasons=reasons,
        )


@dataclass(frozen=True)
class SemanticRiskSignal:
    """Content that claims its own authority or binds a future answer (treated as instruction)."""

    name: str = "semantic_risk"
    kind: SignalKind = "instruction"

    def evaluate(self, text: str, meta: TurnMeta) -> SignalResult:
        kinds = semantic_risk_kinds(text)
        return SignalResult(
            name=self.name,
            kind=self.kind,
            score=_grade(len(kinds)),
            flagged=bool(kinds),
            reasons=tuple(f"semantic_risk:{k}" for k in kinds),
        )


@dataclass(frozen=True)
class AnomalySignal:
    """Contradiction of a high-trust value by a low-trust source, claim rate, embedding outlier."""

    high_trust: float = 0.7
    margin: float = 0.3
    max_claims_per_window: int = 20
    min_neighbours: int = 8
    outlier_similarity: float = 0.05
    exempt_roles: tuple[str, ...] = ()
    name: str = "anomaly"
    kind: SignalKind = "anomaly"

    def evaluate(self, text: str, meta: TurnMeta) -> SignalResult:
        if meta.role in self.exempt_roles:
            return SignalResult(name=self.name, kind=self.kind, score=0.0, flagged=False)
        reasons: list[str] = []
        score = 0.0
        own = meta.trust if meta.trust is not None else 1.0
        held = meta.contradicts_trust
        if held is not None and held >= self.high_trust and own <= held - self.margin:
            reasons.append(f"contradicts_high_trust(held={held:.2f},own={own:.2f})")
            score = max(score, min(1.0, 0.5 + (held - own)))
        if meta.claims_in_window > self.max_claims_per_window:
            reasons.append(f"claim_rate({meta.claims_in_window}>{self.max_claims_per_window})")
            score = max(score, min(1.0, meta.claims_in_window / (2 * self.max_claims_per_window)))
        sims = meta.neighbour_similarities
        if len(sims) >= self.min_neighbours and max(sims) < self.outlier_similarity:
            reasons.append(f"embedding_outlier(nearest={max(sims):.3f})")
            score = max(score, 0.75)
        return SignalResult(
            name=self.name,
            kind=self.kind,
            score=score,
            flagged=bool(reasons),
            reasons=tuple(reasons),
        )


@dataclass(frozen=True)
class MinjaBridgeSignal:
    """A turn that plants a memory via fake recall phrasing, or repeats a recent write's prefix."""

    prefix_chars: int = 96
    exempt_roles: tuple[str, ...] = ()
    name: str = "minja_bridge"
    kind: SignalKind = "anomaly"

    def evaluate(self, text: str, meta: TurnMeta) -> SignalResult:
        if meta.role in self.exempt_roles:
            return SignalResult(name=self.name, kind=self.kind, score=0.0, flagged=False)
        reasons: list[str] = []
        score = 0.0
        hits = () if meta.recall_verified else recall_bridge_hits(text)
        if hits:
            reasons.append("minja_recall_phrase")
            score = max(score, _grade(len(hits)))
        if len(text) >= self.prefix_chars and meta.recent_contents:
            prefix = text[: self.prefix_chars]
            if any(c.startswith(prefix) for c in meta.recent_contents if c != text):
                reasons.append("minja_bridge_prefix")
                score = max(score, 0.75)
        return SignalResult(
            name=self.name,
            kind=self.kind,
            score=score,
            flagged=bool(reasons),
            reasons=tuple(reasons),
        )


@dataclass(frozen=True)
class QueryAnomalySignal:
    """MemSAD: the write sits anomalously close to recently asked queries (z supplied by caller)."""

    name: str = "query_anomaly"
    kind: SignalKind = "anomaly"

    def evaluate(self, text: str, meta: TurnMeta) -> SignalResult:
        z = meta.query_anomaly_z
        if z is None:
            return SignalResult(name=self.name, kind=self.kind, score=0.0, flagged=False)
        return SignalResult(
            name=self.name,
            kind=self.kind,
            score=min(1.0, max(0.0, z / 10.0)),
            flagged=True,
            reasons=(f"query_history_anomaly(z={z:.2f})",),
        )


@dataclass(frozen=True)
class SecretsSignal:
    """Secrets/PII presence flag. ``detector(text)`` returns the kinds found (caller-supplied)."""

    detector: Callable[[str], Sequence[str]]
    name: str = "secrets_pii"
    kind: SignalKind = "info"

    def evaluate(self, text: str, meta: TurnMeta) -> SignalResult:
        kinds = tuple(sorted(set(self.detector(text))))
        return SignalResult(
            name=self.name,
            kind=self.kind,
            score=1.0 if kinds else 0.0,
            flagged=bool(kinds),
            reasons=tuple(f"sensitive:{k}" for k in kinds),
        )


class SignalConfig(_Frozen):
    """Which signals run (mirrors ``FirewallSignalsConfig``). G-06 "A + C": extended ones are on."""

    instruction: bool = True
    anomaly: bool = True
    minja_bridge: bool = True
    instruction_extended: bool = True
    semantic_risk: bool = True
    query_anomaly: bool = False
    minja_bridge_exempt_roles: tuple[str, ...] = ()
    anomaly_exempt_roles: tuple[str, ...] = ()
    minja_bridge_prefix_chars: int = Field(default=96, ge=16)


def build_signals(
    config: SignalConfig | None = None,
    *,
    secrets_detector: Callable[[str], Sequence[str]] | None = None,
    extra: Iterable[Signal] = (),
) -> tuple[Signal, ...]:
    """The stack, base signals first, then extended, then the caller's (fixed order)."""
    cfg = config or SignalConfig()
    out: list[Signal] = []
    if cfg.instruction_extended:
        out.append(InstructionSignal(extended=True))
    elif cfg.instruction:
        out.append(InstructionSignal())
    if cfg.anomaly:
        out.append(AnomalySignal(exempt_roles=cfg.anomaly_exempt_roles))
    if cfg.minja_bridge:
        out.append(
            MinjaBridgeSignal(
                prefix_chars=cfg.minja_bridge_prefix_chars,
                exempt_roles=cfg.minja_bridge_exempt_roles,
            )
        )
    if cfg.semantic_risk:
        out.append(SemanticRiskSignal())
    if cfg.query_anomaly:
        out.append(QueryAnomalySignal())
    if secrets_detector is not None:
        out.append(SecretsSignal(secrets_detector))
    out.extend(extra)
    return tuple(out)


# --------------------------------------------------------------------------- shared decision rule


def _base_trust(meta: TurnMeta, policy: GatePolicy) -> float:
    trust = (
        meta.trust
        if meta.trust is not None
        else policy.role_trust.get(meta.role, policy.default_trust)
    )
    if meta.channel in policy.external_channels:
        trust = min(trust, policy.retrieved_cap)
    return trust


def _untrusted_origin(meta: TurnMeta, policy: GatePolicy) -> bool:
    return meta.channel in policy.external_channels or meta.role in policy.untrusted_roles


def _run(
    signals: Sequence[Signal], text: str, meta: TurnMeta
) -> tuple[list[SignalResult], bool, bool, bool]:
    results = [s.evaluate(text, meta) for s in signals]
    instr = any(r.flagged and r.kind == "instruction" for r in results)
    anom = any(r.flagged and r.kind == "anomaly" for r in results)
    info = any(r.flagged and r.kind == "info" for r in results)
    return results, instr, anom, info


def _decide(
    *,
    trust: float,
    meta: TurnMeta,
    instr: bool,
    anom: bool,
    info: bool,
    policy: GatePolicy,
) -> tuple[Action, list[str]]:
    reasons: list[str] = []
    action: Action = "accept"
    if trust < policy.quarantine_below:
        action = "quarantine"
        reasons.append(f"trust_below_quarantine({trust:.2f})")
    if anom and meta.role not in policy.privileged_roles:
        action = "quarantine"
        reasons.append("anomaly_non_privileged")
    if instr and _untrusted_origin(meta, policy):
        action = "quarantine"
        reasons.append("instruction_from_untrusted_origin")
    if policy.reject_below is not None and trust < policy.reject_below:
        action = "reject"
        reasons.append(f"trust_below_reject({trust:.2f})")
    if policy.reject_on_secrets and info:
        action = "reject"
        reasons.append("secrets_present")
    return action, reasons


def _verdict(
    gate: Literal[1, 2, 3, 4],
    action: Action,
    trust: float,
    results: Sequence[SignalResult],
    decision_reasons: Sequence[str],
    instr: bool,
    anom: bool,
    info: bool,
    extra_reasons: Sequence[str] = (),
    **kw: object,
) -> GateVerdict:
    reasons = [r for res in results for r in res.reasons]
    reasons.extend(extra_reasons)
    reasons.extend(decision_reasons)
    if action != "accept":
        reasons.append("quarantined" if action == "quarantine" else "rejected")
    label = "inert" if instr and action == "accept" else None
    return GateVerdict(
        gate=gate,
        action=action,
        trust=trust,
        instruction_flag=instr,
        anomalous=anom,
        sensitive=info,
        reasons=tuple(reasons),
        scores=tuple((r.name, r.score) for r in results),
        signals_run=tuple(r.name for r in results),
        label=label,
        **kw,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- gate 1


def gate_insert(
    turn_meta: TurnMeta,
    text: str,
    signals: Sequence[Signal] | None = None,
    *,
    policy: GatePolicy | None = None,
) -> GateVerdict:
    """Gate 1: screen a raw write before it is accepted into derivation.

    ``signals=None`` builds the default stack (:func:`build_signals`); pass ``()`` to run none (an
    ablation arm: trust is still scored and the trust floor still applies)."""
    pol = policy or GatePolicy()
    stack = build_signals() if signals is None else tuple(signals)
    trust = _base_trust(turn_meta, pol)
    meta = turn_meta.model_copy(update={"trust": trust})
    results, instr, anom, info = _run(stack, text, meta)
    action, why = _decide(trust=trust, meta=meta, instr=instr, anom=anom, info=info, policy=pol)
    return _verdict(1, action, trust, results, why, instr, anom, info)


# --------------------------------------------------------------------------- gate 2 / 3


class ParentRef(_Frozen):
    """A parent of a derived record, as the gate sees it."""

    record_id: str
    trust: float = Field(ge=0.0, le=1.0)
    quarantined: bool = False
    #: Effective trust at this viewer (grant chain, LIB-21); None: use ``trust``.
    view_trust: float | None = Field(default=None, ge=0.0, le=1.0)
    #: False: the parent cannot be read (erased, outside the horizon): counts as view trust 0.
    visible: bool = True
    families: frozenset[str] = frozenset()
    live: bool = True


class DerivedMeta(_Frozen):
    """A derived item awaiting admission (an extraction item, a summary, a caption)."""

    record_id: str = ""
    #: Trust the item would get on its own (class cap x firewall trust); capped below by parents.
    base_trust: float = Field(default=1.0, ge=0.0, le=1.0)
    role: str = "derived"
    channel: str = "derived"
    family: str = ""


def gate_derive_input(
    turns: Sequence[ParentRef],
) -> tuple[tuple[ParentRef, ...], tuple[ParentRef, ...]]:
    """Gate 2: split extraction input into (kept, excluded). Quarantined, dead or unreadable turns
    never enter a prompt; the kept turns are to be rendered as data by the caller."""
    kept: list[ParentRef] = []
    dropped: list[ParentRef] = []
    for p in turns:
        (dropped if p.quarantined or not p.live or not p.visible else kept).append(p)
    return tuple(kept), tuple(dropped)


def gate_derive(
    derived_meta: DerivedMeta,
    parents: Sequence[ParentRef],
    *,
    text: str = "",
    signals: Sequence[Signal] | None = None,
    policy: GatePolicy | None = None,
) -> GateVerdict:
    """Gate 3: admit a derived record. The cap ``tau <= min(parent views)`` is unconditional (POL-2).

    A quarantined parent holds the derived record (it cannot launder it); an unreadable parent counts
    as view trust 0 and holds it; a derived record with no parents has no lineage and is held (fail
    closed). ``text`` (when given) is screened with the same signals as gate 1, as an untrusted
    origin: an instruction-shaped derived item is held."""
    pol = policy or GatePolicy()
    stack = build_signals() if signals is None else tuple(signals)
    own = TurnMeta(
        record_id=derived_meta.record_id,
        role=derived_meta.role,
        channel=derived_meta.channel,
        trust=derived_meta.base_trust,
        family=derived_meta.family,
    )
    views = [
        (p.view_trust if p.view_trust is not None else p.trust) if p.visible else 0.0
        for p in parents
    ]
    capped = deposit_trust(derived_meta.base_trust, views)
    trust = max(0.0, min(1.0, capped * (pol.derivation_decay if parents else 1.0)))
    meta = own.model_copy(update={"trust": trust})
    results, instr, anom, info = _run(stack, text, meta) if text else ([], False, False, False)
    action, why = _decide(trust=trust, meta=meta, instr=instr, anom=anom, info=info, policy=pol)
    extra: list[str] = []
    if not parents:
        action = "quarantine" if action == "accept" else action
        extra.append("no_lineage")
    for p in sorted(parents, key=lambda x: x.record_id):
        if p.quarantined:
            action = "quarantine" if action == "accept" else action
            extra.append(f"parent_quarantined:{p.record_id}")
        if not p.visible:
            action = "quarantine" if action == "accept" else action
            extra.append(f"parent_unreadable:{p.record_id}")
        elif not p.live:
            action = "quarantine" if action == "accept" else action
            extra.append(f"parent_not_live:{p.record_id}")
    families = sorted({f for p in parents for f in p.families})
    independent = sum(1 for f in families if is_independent_family(f))
    return _verdict(
        3,
        action,
        trust,
        results,
        why,
        instr,
        anom,
        info,
        extra,
        families=tuple(families),
        independent_support=independent,
    )


# --------------------------------------------------------------------------- gate (read)


class ReadItem(_Frozen):
    """What the read gate needs about an item that is about to be rendered as evidence."""

    record_id: str
    owner: str
    scope: str = ""
    trust: float = Field(ge=0.0, le=1.0)
    quarantined: bool = False
    instruction_flag: bool = False
    #: When present with ``consent`` the LIB-18 admission check runs too.
    consent_meta: RecordMeta | None = None


def gate_read(
    item_meta: ReadItem,
    viewer: Viewer,
    horizon: Sequence[Grant] | None = None,
    *,
    consent: ConsentRegistry | None = None,
    purpose: str = PURPOSE_ANY,
    include_quarantined: bool = False,
    min_view_trust: float = 0.0,
) -> GateVerdict:
    """Read gate: may this item be rendered to ``viewer``?

    Order (POL-2, numeric trust is not access control): consent, then the grant chain (a foreign
    item needs a visible chain from its owner ending at the viewer; trust is attenuated by it), then
    the quarantine rule, then the admission threshold. A quarantined item is never rendered unless
    ``include_quarantined`` is set, and then it is labelled ``quarantined``. An instruction-flagged
    item is rendered with the label ``inert`` (as data)."""
    reasons: list[str] = []
    if item_meta.consent_meta is not None and consent is not None:
        d = may_read(item_meta.consent_meta, consent, purpose, viewer)
        if not d.allowed:
            return _read_verdict("reject", 0.0, [f"consent:{d.reason.value}"], None, item_meta)
    own_view = viewer.principal == item_meta.owner
    if own_view:
        view = item_meta.trust
    else:
        path = tuple(horizon or ())
        view = effective_trust(
            TrustedRecord(owner=item_meta.owner, scope=item_meta.scope, trust=item_meta.trust),
            path,
            viewer=viewer.principal,
        )
        if not path or view <= 0.0:
            return _read_verdict("reject", 0.0, ["outside_horizon"], None, item_meta)
        reasons.append(f"attenuated({item_meta.trust:.2f}->{view:.2f})")
    if item_meta.quarantined:
        if not include_quarantined:
            return _read_verdict(
                "quarantine", view, [*reasons, "quarantined_hidden"], None, item_meta
            )
        reasons.append("quarantined_requested")
        label = "quarantined"
    else:
        label = "inert" if item_meta.instruction_flag else None
    if view < min_view_trust:
        return _read_verdict(
            "reject", view, [*reasons, "below_admission_threshold"], None, item_meta
        )
    return _read_verdict("accept", view, reasons, label, item_meta)


def _read_verdict(
    action: Action, trust: float, reasons: list[str], label: str | None, item: ReadItem
) -> GateVerdict:
    return GateVerdict(
        gate=4,
        action=action,
        trust=trust,
        instruction_flag=item.instruction_flag,
        reasons=tuple(reasons),
        label=label,
    )


# --------------------------------------------------------------------------- release (S4-4)


@dataclass(frozen=True)
class ReleaseVerdict:
    release: bool
    released_by: str | None
    decision: CorroborationDecision | None
    #: Every candidate checked, as ``(record_id, reason)``, in the order checked.
    checked: tuple[tuple[str, str], ...] = field(default=())


def release_quarantine(
    held: StoredRecord,
    candidates: Iterable[StoredRecord],
    *,
    lineage_lookup: Callable[[str], Iterable[LineageEdge]] | None = None,
    default_trust: float = DEFAULT_TRUST,
    suffixes: Collection[str] | None = None,
) -> ReleaseVerdict:
    """May any candidate release the quarantined ``held`` record? Calls LIB-08 ``corroboration``.

    Candidates are tried in record-id order; the first release wins. With ``lineage_lookup`` the
    families of each side come from :func:`families_of` (an echo or summary inherits its parents')."""
    checked: list[tuple[str, str]] = []
    fam_kw: dict[str, Collection[str]] = {} if suffixes is None else {"suffixes": suffixes}
    held_fams: frozenset[FamilyId] | None = (
        families_of(held, lineage_lookup, **fam_kw) if lineage_lookup is not None else None  # type: ignore[arg-type]
    )
    last: CorroborationDecision | None = None
    for cand in sorted(candidates, key=lambda c: c.record_id):
        inc_fams = (
            families_of(cand, lineage_lookup, **fam_kw) if lineage_lookup is not None else None
        )  # type: ignore[arg-type]
        d = corroboration(
            cand,
            held,
            incoming_families=inc_fams,
            held_families=held_fams,
            default_trust=default_trust,
        )
        checked.append((cand.record_id, d.reason))
        last = d
        if d.release:
            return ReleaseVerdict(True, cand.record_id, d, tuple(checked))
    return ReleaseVerdict(False, None, last, tuple(checked))
