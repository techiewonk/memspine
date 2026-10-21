"""GLiNER output is mapped to exact, unasserted source annotations."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from memspine.derive.gliner_mentions import (
    GlinerMentionError,
    MentionSpan,
    parse_gliner_mentions,
)


def test_parses_every_sdk_span_with_exact_unicode_offsets_and_scores() -> None:
    text = "Mira met Mira at 東京."
    raw = {
        "entities": {
            "person": [
                {"text": "Mira", "confidence": 0.91, "start": 0, "end": 4},
                {"text": "Mira", "confidence": 0.72, "start": 9, "end": 13},
            ],
            "location": [{"text": "東京", "confidence": 0.84, "start": 17, "end": 19}],
        }
    }

    mentions = parse_gliner_mentions(text, raw, ("person", "location"))

    assert [(m.text, m.label, m.start, m.end, m.confidence) for m in mentions] == [
        ("Mira", "person", 0, 4, 0.91),
        ("Mira", "person", 9, 13, 0.72),
        ("東京", "location", 17, 19, 0.84),
    ]
    assert all(m.asserted is False for m in mentions)


def test_legacy_strings_are_grounded_only_when_the_occurrence_is_unique() -> None:
    mention = parse_gliner_mentions(
        "Acme hired Mira", {"organization": ["Acme"]}, ["organization"]
    )[0]
    assert (mention.start, mention.end, mention.confidence) == (0, 4, None)

    with pytest.raises(GlinerMentionError, match="ambiguous"):
        parse_gliner_mentions("Mira met Mira", {"person": ["Mira"]}, ["person"])


def test_results_are_ordered_by_source_offsets_when_allowlist_order_differs() -> None:
    text = "Acme hired Mira"
    raw = {
        "organization": [{"text": "Acme", "start": 0, "end": 4}],
        "person": [{"text": "Mira", "start": 11, "end": 15}],
    }

    mentions = parse_gliner_mentions(text, raw, ("person", "organization"))

    assert [(mention.text, mention.start) for mention in mentions] == [("Acme", 0), ("Mira", 11)]


@pytest.mark.parametrize(
    ("text", "raw", "message"),
    [
        ("Mira", {"person": [{"text": "Mira", "start": 1, "end": 5}]}, "outside"),
        ("Mira", {"person": [{"text": "Mira", "start": 0, "end": 3}]}, "exactly match"),
        ("Mira", {"person": [{"text": "Mira", "start": True, "end": 4}]}, "integer"),
        ("Mira", {"person": [{"text": "Mira", "start": 0}]}, "both start and end"),
        ("Mira", {"person": [{"text": "Mira", "confidence": math.nan}]}, "finite"),
        ("Mira", {"person": [{"text": "Mira", "confidence": True}]}, "numeric"),
        ("Mira", {"person": [{"text": "Mira", "extra": "unexpected"}]}, "unexpected"),
        ("Mira", {"animal": ["Mira"]}, "unsupported entity labels"),
        ("Mira", {"entities": {"person": ["Mira"]}, "records": []}, "top-level"),
    ],
)
def test_malformed_or_spoofed_sdk_results_fail_closed(text: str, raw: object, message: str) -> None:
    with pytest.raises(GlinerMentionError, match=message):
        parse_gliner_mentions(text, raw, ["person"])


def test_one_bad_item_rejects_entire_result_instead_of_partial_mentions() -> None:
    raw = {
        "person": [
            {"text": "Mira", "start": 0, "end": 4},
            {"text": "Acme", "start": 5, "end": 9},
        ]
    }
    with pytest.raises(GlinerMentionError):
        parse_gliner_mentions("Mira met Acme", raw, ["person"])


def test_mention_model_rejects_assertions_and_invalid_scores() -> None:
    with pytest.raises(ValidationError):
        MentionSpan(text="Mira", label="person", start=0, end=4, asserted=True)
    with pytest.raises(ValidationError):
        MentionSpan(text="Mira", label="person", start=0, end=4, confidence=float("inf"))


def test_label_argument_must_be_an_actual_unique_allowlist() -> None:
    with pytest.raises(GlinerMentionError, match="unique"):
        parse_gliner_mentions("Mira", {"person": ["Mira"]}, ["person", "person"])
