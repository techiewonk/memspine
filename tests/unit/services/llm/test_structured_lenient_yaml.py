"""A YAML reply with an unquoted ': ' in a value must not lose every item."""

from __future__ import annotations

from memspine.prompts.base import PromptFormat
from memspine.prompts.models import ExtractedFacts
from memspine.services.llm.structured import _parse_payload

# Shape seen from Qwen3-32B on LoCoMo (pilot, 2026-10-03): strict YAML rejects
# line 3 ("mapping values are not allowed here").
REPLY = """```yaml
facts:
  - entity: Caroline
    attribute: book_recommendation
    value: Caroline recommended "Becoming Nicole": a true story about a trans girl.
    date: 2023-07-12
    confidence: 0.9
  - entity: Melanie
    attribute: hobby
    value: pottery
    date: ""
    confidence: 1.0
```"""


def test_unquoted_colon_value_is_salvaged() -> None:
    payload = _parse_payload(REPLY, PromptFormat.YAML)
    facts = ExtractedFacts.model_validate(payload)
    assert len(facts.facts) == 2
    assert facts.facts[0].value.startswith('Caroline recommended "Becoming Nicole": a true')
    assert facts.facts[1].entity == "Melanie"


def test_valid_yaml_is_unchanged() -> None:
    ok = "facts:\n  - entity: A\n    attribute: city\n    value: Lyon\n    confidence: 1.0\n"
    payload = _parse_payload(ok, PromptFormat.YAML)
    assert payload == {
        "facts": [{"entity": "A", "attribute": "city", "value": "Lyon", "confidence": 1.0}]
    }
