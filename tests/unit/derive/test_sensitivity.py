"""RM-99: the sensitivity labelling layer (deterministic detectors, three states, opt-in model).

The fixtures double as the RM-98 evaluation set: positives per detector, false-positive
guards (checksums, anchored ambiguous terms) and documented misses / known false positives.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from memspine.derive.sensitivity import (
    LABELLER_ID,
    NOT_CHECKED,
    ClaimText,
    GlinerSensitivityDetector,
    LabelHit,
    SensitivityLabeller,
    detect_patterns,
    detect_terms,
    detect_write_tags,
    vocabulary_defaults,
)


def _labels(hits: Sequence[LabelHit]) -> set[str]:
    return {h.label for h in hits}


# ------------------------------------------------------------------ 1. vocabulary defaults


@pytest.mark.parametrize(
    ("predicate", "key", "expected"),
    [
        ("resides_in", None, {"location"}),
        ("home_country", None, {"location"}),
        ("temporarily_present_in", None, {"location"}),
        ("born_in", None, {"identity"}),
        ("citizen_of", None, {"identity"}),
        ("has_attribute", "contact_channel", {"contact"}),
        ("has_attribute", "nationality", {"identity"}),
        ("has_attribute", "pronouns", {"identity"}),
        ("has_condition", None, {"health"}),  # proposed pack: labels only tighten
        ("allergic_to", None, {"health"}),
        ("purchased", None, {"financial"}),
        ("likes", None, set()),
        ("has_attribute", "colour", set()),
        ("lives_in", None, set()),  # not a vocabulary name: no default (never aliased)
        (None, None, set()),
    ],
)
def test_vocabulary_defaults(predicate: str | None, key: str | None, expected: set[str]) -> None:
    hits = vocabulary_defaults(ClaimText(predicate=predicate, attribute_key=key))
    assert _labels(hits) == expected
    assert all(h.confidence == 1.0 and h.detector == "vocabulary" for h in hits)


# ------------------------------------------------------------------ 2. rule / pattern detectors

PATTERN_POSITIVE = [
    ("write to ana.k@example.org", "contact", "pattern:email"),
    ("call me on +44 20 7946 0958", "contact", "pattern:phone"),
    ("my number is (415) 555-0123", "contact", "pattern:phone"),
    ("IBAN GB82 WEST 1234 5698 7654 32", "financial", "pattern:iban"),
    ("card 4111 1111 1111 1111", "financial", "pattern:payment_card"),
    ("Bank account number: 12345678", "financial", "pattern:bank_account"),
    ("SSN 123-45-6789", "identity", "pattern:us_ssn"),
    ("Passport number: X1234567", "identity", "pattern:passport"),
    ("lives at 42 Palm Grove Street", "location", "pattern:street_address"),
    ("postcode SW1A 1AA", "location", "pattern:postcode"),
    ("my zip code is 94110", "location", "pattern:postcode"),
    ("reach her at [REDACTED:email]", "contact", "pattern:email"),
    ("paid from [REDACTED:iban]", "financial", "pattern:iban"),
]


@pytest.mark.parametrize(("text", "label", "detector"), PATTERN_POSITIVE)
def test_pattern_positives(text: str, label: str, detector: str) -> None:
    hits = detect_patterns(text)
    assert any(h.label == label and h.detector == detector for h in hits), hits


PATTERN_NEGATIVE = [
    "order number 4111 1111 1111 1112",  # fails Luhn: not a card
    "IBAN GB82 WEST 1234 5698 7654 33",  # fails mod-97
    "upgrade to version 1.2.3.4 tonight",  # version string, not an address
    "server at 10.0.0.1 is down",  # IP addresses are deliberately unmapped
    "the meeting is on 2023-05-07 at 10:30",
    "room 101 on floor 3",
    "the M25 was busy",  # road name, not a full postcode
    "api_key = sk-proj-abcdefghijklmnopqrstuvwx",  # a secret: the firewall's job, no label
]


@pytest.mark.parametrize("text", PATTERN_NEGATIVE)
def test_pattern_false_positive_guards(text: str) -> None:
    assert detect_patterns(text) == []


TERM_POSITIVE = [
    ("She was diagnosed with asthma last year", "health"),
    ("He takes metformin every morning", "health"),
    ("my doctor moved the appointment", "health"),
    ("Her mother survived breast cancer", "health"),
    ("He has had migraines since college", "health"),
    ("They are pregnant with twins", "health"),
    ("I don't have diabetes", "health"),  # negated: still health information
    ("my mortgage payment went up", "financial"),
    ("we filed for bankruptcy", "financial"),
]


@pytest.mark.parametrize(("text", "label"), TERM_POSITIVE)
def test_term_positives(text: str, label: str) -> None:
    assert label in _labels(detect_terms(text))


TERM_NEGATIVE = [
    "She is a Cancer and loves astrology",  # star sign
    "The Great Depression shaped his parents",  # economic term
    "Relax, it's not brain surgery",  # figurative
    "the reading list prescribed by the course",  # 'prescribed' without a medical cue
    "He feels anxious before talks",  # ordinary feeling, not a disorder
    "I paid for the tickets",  # a payment is not first-person financial hardship
]


@pytest.mark.parametrize("text", TERM_NEGATIVE)
def test_term_false_positive_guards(text: str) -> None:
    assert detect_terms(text) == []


def test_documented_known_false_positive_and_miss() -> None:
    """RM-98 fixture: behaviour we accept and document rather than hide."""
    # Known false positive: figurative allergy still labels health (labels only tighten).
    assert _labels(detect_terms("I'm allergic to Monday meetings")) == {"health"}
    # Known miss: a condition outside the small term list is not found by the rules
    # (the vocabulary default of has_condition, or the opt-in model, covers it).
    assert detect_terms("She has a heart murmur") == []
    labeller = SensitivityLabeller()
    claim = ClaimText(predicate="has_condition", values=("She has a heart murmur",))
    assert labeller.label(claim).labels == {"health"}


def test_write_tags() -> None:
    hits = detect_write_tags(["sensitive:health", "sensc:finance", "sensc:religion", "rel:x"])
    assert _labels(hits) == {"health", "financial"}  # religion is outside the five labels
    assert detect_write_tags(["sens:high"]) == []


# ------------------------------------------------------------------ labeller: three states


def test_labelled_state_with_several_labels_and_confidence() -> None:
    labeller = SensitivityLabeller()
    claim = ClaimText(
        predicate="resides_in",
        values=("Ana's GP surgery line is +44 20 7946 0958, my doctor says hi",),
    )
    result = labeller.label(claim)
    assert result.state == "labelled" and result.checked is True
    assert result.labels == {"location", "contact", "health"}
    assert result.confidence["location"] == 1.0  # vocabulary default
    assert result.confidence["contact"] == 0.75
    assert result.detectors["contact"] == ("pattern:phone",)
    assert result.labeller_id == LABELLER_ID
    assert result.labeller_version is not None
    assert result.labeller_version.startswith("rules-1+vocab-")


def test_checked_none_state() -> None:
    result = SensitivityLabeller().label(ClaimText(predicate="likes", values=("Ana likes jazz",)))
    assert result.state == "checked_none"
    assert result.checked is True and result.labels == frozenset() and result.confidence == {}


def test_not_checked_state() -> None:
    assert NOT_CHECKED.state == "not_checked" and NOT_CHECKED.checked is None


def test_labelling_is_deterministic() -> None:
    claim = ClaimText(predicate="purchased", values=("card 4111 1111 1111 1111",))
    assert SensitivityLabeller().label(claim) == SensitivityLabeller().label(claim)


# ------------------------------------------------------------------ opt-in model detector


class _FakeModel:
    detector_id = "fake@1"

    def __init__(self, hits: Sequence[LabelHit] = (), fail: bool = False) -> None:
        self._hits = list(hits)
        self._fail = fail

    async def detect(self, text: str) -> Sequence[LabelHit]:
        if self._fail:
            raise RuntimeError("model unavailable")
        return self._hits


async def test_model_adds_labels_and_changes_version() -> None:
    model = _FakeModel([LabelHit("health", 0.8, "model:fake"), LabelHit("identity", 0.2, "m")])
    labeller = SensitivityLabeller(model=model)
    result = await labeller.alabel(ClaimText(predicate="likes", values=("Ana mentioned X",)))
    assert result.state == "labelled"
    assert result.labels == {"health"}  # the 0.2 hit is below the 0.5 threshold
    assert result.labeller_version is not None and result.labeller_version.endswith("+fake@1")


async def test_failed_model_makes_the_check_incomplete() -> None:
    labeller = SensitivityLabeller(model=_FakeModel(fail=True))
    claim = ClaimText(predicate="resides_in", values=("Ana lives in Porto",))
    result = await labeller.alabel(claim)
    assert result.state == "incomplete" and result.checked is False
    assert result.labels == {"location"}  # deterministic labels are kept
    none_found = await labeller.alabel(ClaimText(predicate="likes", values=("jazz",)))
    assert none_found.state == "incomplete" and none_found.labels == frozenset()
    # the sync path with a configured model that did not run is incomplete, never complete
    assert labeller.label(claim).checked is False


class _Settings:
    model = "/models/gliner-pii"
    revision = None
    device = "cpu"
    local_files_only = True
    tasks = ("entities",)
    inference_concurrency = 1


class _Binding:
    def __init__(self, raw: Any) -> None:
        self._raw = raw

    async def call(self, callback: Any) -> Any:
        class _Model:
            def extract_entities(_self, text: str, labels: list[str], **_: Any) -> Any:
                return self._raw

        return callback(_Model())


class _Pool:
    def __init__(self, raw: Any) -> None:
        self.raw = raw

    def binding(self, settings: Any) -> _Binding:
        return _Binding(self.raw)


async def test_gliner_detector_maps_entities_to_labels() -> None:
    text = "takes lisinopril daily"
    raw = {"entities": {"medication": [{"text": "lisinopril", "confidence": 0.91}]}}
    detector = GlinerSensitivityDetector(_Pool(raw), _Settings())
    hits = await detector.detect(text)
    assert [(h.label, h.confidence) for h in hits] == [("health", 0.91)]
    assert detector.detector_id == "gliner2:/models/gliner-pii@default"


def test_gliner_detector_is_offline_only() -> None:
    online = type("S", (_Settings,), {"local_files_only": False})()
    with pytest.raises(ValueError, match="offline only"):
        GlinerSensitivityDetector(_Pool({}), online)


async def test_gliner_malformed_output_fails_closed_to_incomplete() -> None:
    detector = GlinerSensitivityDetector(_Pool({"entities": {"weapon": ["x"]}}), _Settings())
    labeller = SensitivityLabeller(model=detector)
    result = await labeller.alabel(ClaimText(predicate="likes", values=("x marks it",)))
    assert result.state == "incomplete"
