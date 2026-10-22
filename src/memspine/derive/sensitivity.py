"""Sensitivity labelling layer (RM-99): label one claim with RM-52 sensitivity labels.

The proposal is ``docs/SENSITIVITY_LABELLING_PROPOSAL.md`` (RM-98). Detectors run in a fixed
order and their findings are unioned (labels only tighten policy, RM-52):

1. **Vocabulary defaults** (``vocabulary``, confidence 1.0): the ``sensitivity`` labels a
   packaged vocabulary pack declares on the claim's predicate or property key. Every packaged
   pack is consulted, including proposed ones, because a label can only tighten handling.
2. **Rule / pattern detectors** over the claim's value texts, deterministic and offline:

   * ``pattern``: the firewall's identifier patterns (``trust.redaction``), checksums
     included (Luhn card numbers, ISO 13616 mod-97 IBANs), and the ``[REDACTED:<kind>]``
     markers the write door leaves behind, plus a UK-postcode shape and cue-anchored postal
     codes;
   * ``terms``: a small, documented medical-term list (:data:`HEALTH_TERMS`) and the W16
     first-person financial cues (``core.sensitive``);
   * ``write_tags``: the write door's ``sensitive:<category>`` / ``sensc:<category>`` tags.
3. **Model detector** (optional, off unless ``models.bindings.gliner_sensitivity`` is bound):
   a local GLiNER2 entity pass (:class:`GlinerSensitivityDetector`), offline only
   (``local_files_only`` required), never a generative model.

Every result carries the labeller id and version (rules version + vocabulary digest + model
id), per-label confidence and the detectors that fired, never the matched text. The three
states (RM-86): labels found, *checked_none* (complete check, nothing found) and not checked.
An enabled detector that fails makes the check incomplete (``checked=False``), never
silently complete. A label is a hint for stricter handling, not a classification of the
content as safe when absent.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Literal, NamedTuple, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from memspine.core.sensitive import SENSITIVE_CATEGORIES
from memspine.observability.logging import get_logger
from memspine.semantics.registry import SemanticRegistry, load_builtin_packs
from memspine.semantics.sensitivity import SENSITIVITY_LABELS, SensitivityLabel
from memspine.trust.redaction import redact

_log = get_logger(__name__)

__all__ = [
    "HEALTH_TERMS",
    "LABELLER_ID",
    "NOT_CHECKED",
    "PATTERN_LABELS",
    "RULES_VERSION",
    "ClaimText",
    "GlinerSensitivityDetector",
    "LabelHit",
    "SensitivityLabeller",
    "SensitivityModel",
    "SensitivityResult",
    "SensitivityState",
    "detect_patterns",
    "detect_terms",
    "detect_write_tags",
    "vocabulary_defaults",
]

#: The labeller's stable id (recorded on every result).
LABELLER_ID = "memspine.sensitivity"
#: Bump when a rule, a pattern mapping, a confidence or the term list changes.
RULES_VERSION = "1"

SensitivityState = Literal["labelled", "checked_none", "incomplete", "not_checked"]


class LabelHit(NamedTuple):
    """One detector finding: a label, its confidence and the detector id (never the text)."""

    label: SensitivityLabel
    confidence: float
    detector: str


@dataclass(frozen=True)
class ClaimText:
    """What the labeller sees of one claim: names, value texts and write-door tags."""

    predicate: str | None = None
    attribute_key: str | None = None
    values: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()


class SensitivityResult(BaseModel):
    """The labelling of one claim (RM-99)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    labels: frozenset[SensitivityLabel] = frozenset()
    #: True = complete check; False = incomplete (an enabled detector failed or did not run);
    #: None = not checked.
    checked: bool | None = None
    labeller_id: str | None = None
    labeller_version: str | None = None
    confidence: dict[SensitivityLabel, float] = Field(default_factory=dict)
    #: label -> ids of the detectors that found it (sorted).
    detectors: dict[SensitivityLabel, tuple[str, ...]] = Field(default_factory=dict)

    @property
    def state(self) -> SensitivityState:
        if self.checked is None:
            return "not_checked"
        if self.labels:
            return "labelled" if self.checked else "incomplete"
        return "checked_none" if self.checked else "incomplete"


#: The not-checked result (no labeller ran).
NOT_CHECKED = SensitivityResult()


# --------------------------------------------------------------------------- 1. vocabulary


@dataclass(frozen=True)
class _Defaults:
    predicates: Mapping[str, frozenset[SensitivityLabel]]
    properties: Mapping[str, frozenset[SensitivityLabel]]
    digest: str


def _defaults_from(
    predicates: Iterable[tuple[str, Iterable[SensitivityLabel]]],
    properties: Iterable[tuple[str, Iterable[SensitivityLabel]]],
) -> _Defaults:
    preds: dict[str, frozenset[SensitivityLabel]] = {}
    props: dict[str, frozenset[SensitivityLabel]] = {}
    for name, labels in predicates:
        if labels := frozenset(labels):
            preds[name] = preds.get(name, frozenset()) | labels
    for name, labels in properties:
        if labels := frozenset(labels):
            props[name] = props.get(name, frozenset()) | labels
    payload = {
        "predicates": {k: sorted(v) for k, v in sorted(preds.items())},
        "properties": {k: sorted(v) for k, v in sorted(props.items())},
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return _Defaults(MappingProxyType(preds), MappingProxyType(props), digest)


@lru_cache(maxsize=1)
def _packaged_defaults() -> _Defaults:
    packs = load_builtin_packs()
    return _defaults_from(
        ((spec.name, spec.sensitivity) for pack in packs for spec in pack.predicates),
        ((spec.name, spec.sensitivity) for pack in packs for spec in pack.properties),
    )


def _registry_defaults(registry: SemanticRegistry) -> _Defaults:
    return _defaults_from(
        ((name, spec.sensitivity) for name, spec in registry.predicates.items()),
        ((name, spec.sensitivity) for name, spec in registry.properties.items()),
    )


def vocabulary_defaults(
    claim: ClaimText, registry: SemanticRegistry | None = None
) -> list[LabelHit]:
    """The labels declared on the claim's predicate and property key (confidence 1.0)."""
    defaults = _packaged_defaults() if registry is None else _registry_defaults(registry)
    return _vocabulary_hits(claim, defaults)


def _vocabulary_hits(claim: ClaimText, defaults: _Defaults) -> list[LabelHit]:
    labels: set[SensitivityLabel] = set()
    if claim.predicate:
        labels |= defaults.predicates.get(claim.predicate, frozenset())
    if claim.attribute_key:
        labels |= defaults.properties.get(claim.attribute_key, frozenset())
    return [LabelHit(label, 1.0, "vocabulary") for label in sorted(labels)]


# --------------------------------------------------------------------------- 2. rules

#: Identifier kind (``trust.redaction``) -> (label, confidence). Checksummed kinds score
#: highest; cue-anchored identifiers next; shape-only kinds lowest. IP addresses, secrets and
#: home-directory user names are deliberately unmapped (see the proposal).
PATTERN_LABELS: Mapping[str, tuple[SensitivityLabel, float]] = MappingProxyType(
    {
        "iban": ("financial", 0.95),
        "payment_card": ("financial", 0.9),
        "bank_account": ("financial", 0.8),
        "email": ("contact", 0.95),
        "phone": ("contact", 0.75),
        "us_ssn": ("identity", 0.85),
        "passport": ("identity", 0.85),
        "driving_licence": ("identity", 0.85),
        "licence_plate": ("identity", 0.6),
        "street_address": ("location", 0.8),
        "postcode": ("location", 0.65),
    }
)

#: A full UK postcode (outward + inward code, upper case), e.g. "SW1A 1AA", "M1 1AE".
_UK_POSTCODE = re.compile(r"\b(?:[A-PR-UWYZ][A-HK-Y]?\d[A-Z\d]? ?\d[ABD-HJLNP-UW-Z]{2})\b")
#: Any code right after a postal-code cue ("zip code 94110", "postcode: 75008").
_CUED_POSTCODE = re.compile(
    r"(?i)\b(?:zip(?: ?code)?|post ?code|postal code)\b\s*(?:is\s*)?[:#]?\s*[A-Z0-9][A-Z0-9 -]{2,9}\b"
)
_MARKER = re.compile(r"\[REDACTED:([a-z_0-9]+)\]")


def detect_patterns(text: str) -> list[LabelHit]:
    """Identifier patterns, checksums and redaction markers over one value text."""
    hits: list[LabelHit] = []
    kinds = redact(text, secrets=True, pii=True, pii_extended=True)[1]
    kinds += _MARKER.findall(text)
    if _UK_POSTCODE.search(text) or _CUED_POSTCODE.search(text):
        kinds.append("postcode")
    for kind in dict.fromkeys(kinds):
        mapped = PATTERN_LABELS.get(kind)
        if mapped is not None:
            hits.append(LabelHit(mapped[0], mapped[1], f"pattern:{kind}"))
    return hits


#: Small, documented medical-term list (health). Unambiguous condition, treatment and
#: medication words, plus cue phrases. Ambiguous words are anchored to a medical context
#: ("cancer" alone is also a star sign; "depression" also an economic term), and figurative
#: uses ("not brain surgery") are left out. Negated mentions ("I don't have diabetes") are
#: still health information and are labelled.
HEALTH_TERMS: tuple[str, ...] = (
    r"diabet(?:es|ic)",
    r"asthma(?:tic)?",
    r"epilep(?:sy|tic)",
    r"hypertension",
    r"migraines?",
    r"chemotherapy",
    r"dialysis",
    r"insulin",
    r"hiv",
    r"adhd",
    r"c[o]?eliac disease",
    r"arthritis",
    r"eczema",
    r"antidepressants?",
    r"metformin",
    r"inhaler",
    r"epipen",
    r"pregnan(?:t|cy)",
    r"allerg(?:y|ies|ic to)",
    r"intolerant to|intolerance to",
    r"diagnosed with",
    r"(?:was|been|got|were) prescribed|prescribed (?:me|him|her|them)",
    r"(?:clinical|postpartum|major) depression",
    r"(?:has|have|had|with|beat|survived) cancer|cancer (?:treatment|diagnosis|survivor)",
    r"(?:breast|lung|skin|prostate|colon|bowel) cancer",
    r"anxiety disorder|panic attacks?",
    r"my (?:doctor|therapist|psychiatrist|gp|cardiologist|oncologist)",
)
_HEALTH = re.compile(r"(?i)\b(?:" + "|".join(HEALTH_TERMS) + r")\b")


def detect_terms(text: str) -> list[LabelHit]:
    """The medical-term list (health, 0.7) and the W16 financial cues (financial, 0.6)."""
    hits: list[LabelHit] = []
    if _HEALTH.search(text):
        hits.append(LabelHit("health", 0.7, "terms:health"))
    if SENSITIVE_CATEGORIES["financial"].search(text):
        hits.append(LabelHit("financial", 0.6, "terms:financial"))
    return hits


#: Write-door topic tags (``sensitive:<cat>`` W16, ``sensc:<cat>`` I52) -> label.
_TAG_LABELS: Mapping[str, SensitivityLabel] = MappingProxyType(
    {"health": "health", "financial": "financial", "finance": "financial", "location": "location"}
)


def detect_write_tags(tags: Iterable[str]) -> list[LabelHit]:
    """Labels from the write door's topic tags (confidence 0.7)."""
    found: set[SensitivityLabel] = set()
    for tag in tags:
        for prefix in ("sensitive:", "sensc:"):
            if tag.startswith(prefix) and (label := _TAG_LABELS.get(tag[len(prefix) :])):
                found.add(label)
    return [LabelHit(label, 0.7, "write_tags") for label in sorted(found)]


# --------------------------------------------------------------------------- 3. model


@runtime_checkable
class SensitivityModel(Protocol):
    """An optional, offline model detector (opt-in)."""

    @property
    def detector_id(self) -> str: ...

    async def detect(self, text: str) -> Sequence[LabelHit]: ...


class GlinerSensitivityDetector:
    """GLiNER2 entity pass mapped to sensitivity labels (``[ner]``, local CPU, offline only).

    Bound through ``models.bindings.gliner_sensitivity`` (a GLiNER profile with task
    ``entities`` and ``local_files_only: true``). Mentions are reduced to label and confidence;
    the matched text never leaves this call."""

    #: GLiNER entity label -> sensitivity label.
    LABELS: Mapping[str, SensitivityLabel] = MappingProxyType(
        {
            "medical condition": "health",
            "medication": "health",
            "symptom": "health",
            "phone number": "contact",
            "email address": "contact",
            "bank account number": "financial",
            "credit card number": "financial",
            "passport number": "identity",
            "national identification number": "identity",
            "date of birth": "identity",
            "street address": "location",
            "postal code": "location",
        }
    )

    def __init__(self, pool: Any, settings: Any) -> None:
        if not getattr(settings, "local_files_only", False):
            raise ValueError("the GLiNER sensitivity detector is offline only (local_files_only)")
        if "entities" not in tuple(getattr(settings, "tasks", ())):
            raise ValueError("the bound GLiNER profile must include task 'entities'")
        self._settings = settings
        self._model = pool.binding(settings)

    @property
    def detector_id(self) -> str:
        s = self._settings
        return f"gliner2:{s.model}@{s.revision or 'default'}"

    async def detect(self, text: str) -> Sequence[LabelHit]:
        from memspine.derive.gliner_mentions import parse_gliner_mentions

        labels = list(self.LABELS)
        raw = await self._model.call(
            lambda model: model.extract_entities(
                text, labels, include_confidence=True, include_spans=True
            )
        )
        detector = f"model:{self.detector_id}"
        # A mention without a confidence is kept at 0.5: labels only tighten, so an
        # unscored finding is not discarded (the default threshold keeps it).
        return [
            LabelHit(self.LABELS[m.label], 0.5 if m.confidence is None else m.confidence, detector)
            for m in parse_gliner_mentions(text, raw, labels)
        ]


# --------------------------------------------------------------------------- labeller


class SensitivityLabeller:
    """Deterministic by default; the optional model only adds labels (RM-99).

    ``min_confidence`` drops a hit below it (deterministic detectors all score at least 0.6;
    the threshold matters for model hits)."""

    def __init__(
        self,
        *,
        registry: SemanticRegistry | None = None,
        model: SensitivityModel | None = None,
        min_confidence: float = 0.5,
    ) -> None:
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence must lie in [0, 1]")
        self._defaults = _packaged_defaults() if registry is None else _registry_defaults(registry)
        self._model = model
        self._min = min_confidence

    @property
    def model(self) -> SensitivityModel | None:
        return self._model

    @property
    def labeller_id(self) -> str:
        return LABELLER_ID

    @property
    def version(self) -> str:
        """Rules version + vocabulary digest (+ model id): changes whenever labels could."""
        base = f"rules-{RULES_VERSION}+vocab-{self._defaults.digest[:12]}"
        if self._model is not None:
            base += f"+{self._model.detector_id}"
        if self._min != 0.5:
            base += f"+min-{self._min:g}"
        return base

    def deterministic_hits(self, claim: ClaimText) -> list[LabelHit]:
        hits = _vocabulary_hits(claim, self._defaults)
        for text in claim.values:
            if text:
                hits += detect_patterns(text)
                hits += detect_terms(text)
        hits += detect_write_tags(claim.tags)
        return hits

    def label(
        self,
        claim: ClaimText,
        *,
        model_hits: Sequence[LabelHit] | None = None,
        model_failed: bool = False,
    ) -> SensitivityResult:
        """Label ``claim``. With a model configured, ``model_hits=None`` (the model did not
        run) or ``model_failed`` makes the check incomplete."""
        hits = self.deterministic_hits(claim)
        complete = True
        if self._model is not None:
            if model_hits is None or model_failed:
                complete = False
            hits += list(model_hits or ())
        return self._result(hits, complete)

    async def alabel(self, claim: ClaimText) -> SensitivityResult:
        """Label ``claim``, running the optional model detector over its value texts."""
        if self._model is None:
            return self.label(claim)
        found: list[LabelHit] = []
        failed = False
        for text in claim.values:
            if not text:
                continue
            try:
                found += list(await self._model.detect(text))
            except Exception as exc:  # an unavailable model never fakes a complete check
                failed = True
                _log.warning("sensitivity.model_failed", error=type(exc).__name__)
                break
        return self.label(claim, model_hits=found, model_failed=failed)

    def _result(self, hits: Iterable[LabelHit], complete: bool) -> SensitivityResult:
        confidence: dict[SensitivityLabel, float] = {}
        detectors: dict[SensitivityLabel, set[str]] = {}
        for hit in hits:
            if hit.label not in SENSITIVITY_LABELS or hit.confidence < self._min:
                continue
            confidence[hit.label] = max(confidence.get(hit.label, 0.0), min(hit.confidence, 1.0))
            detectors.setdefault(hit.label, set()).add(hit.detector)
        labels = sorted(confidence)
        return SensitivityResult(
            labels=frozenset(labels),
            checked=complete,
            labeller_id=self.labeller_id,
            labeller_version=self.version,
            confidence={label: round(confidence[label], 4) for label in labels},
            detectors={label: tuple(sorted(detectors[label])) for label in labels},
        )
