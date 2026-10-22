"""YAML scalars in structured replies: unquoted dates and numbers stay text.

A model following the session-mining prompt writes ``date: 2023-05-08`` unquoted;
``yaml.safe_load`` turns that into a ``datetime.date`` and validation used to reject
every fact of the session.
"""

from __future__ import annotations

import yaml

from memspine.prompts.base import PromptFormat
from memspine.prompts.models import ExtractedEdges, ExtractedFacts
from memspine.services.llm.structured import _parse_payload


def test_unquoted_dates_and_numbers_validate_as_text() -> None:
    raw = (
        "facts:\n"
        "  - entity: Caroline\n    attribute: age\n    value: 32\n    date: 2023-05-08\n"
        "  - entity: Melanie\n    attribute: event\n    value: painted a lake\n    date: 2022\n"
    )
    facts = ExtractedFacts.model_validate(_parse_payload(raw, PromptFormat.YAML)).facts
    assert [(f.value, f.date) for f in facts] == [("32", "2023-05-08"), ("painted a lake", "2022")]


def test_edge_valid_from_date_stays_text() -> None:
    raw = "edges:\n  - {src_entity: a, rel: r, dst_entity: b, fact: f, valid_from: 2023-05-08}\n"
    edges = ExtractedEdges.model_validate(yaml.safe_load(raw)).edges
    assert edges[0].valid_from == "2023-05-08"
