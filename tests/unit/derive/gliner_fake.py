"""A fake GLiNER2 model with the SDK surface the GLiNER clients call: ``extract`` (structured
entities + relations with exact spans) and ``extract_entities`` (typed spans). Pattern-based and
deliberately naive: it reports a relation whatever the modality, like a span model would, so the
grounding gate is what keeps negated, hypothetical or reported statements out."""

from __future__ import annotations

import re
from typing import Any

PEOPLE = ("I", "Mira", "Bram", "Daniel")
PLACES = ("Lisbon", "Porto")
ORGS = ("Acme", "Globex")
_RELATIONS = (
    (
        "lives_in",
        re.compile(
            r"\b(?P<head>I|Mira|Bram)\b[^.?!;]*?\blive[sd]?\s+in\s+(?P<tail>Lisbon|Porto)\b"
        ),
    ),
    (
        "works_for",
        re.compile(
            r"\b(?P<head>I|Mira|Bram)\b[^.?!;]*?\bwork(?:s|ed)?\s+for\s+(?P<tail>Acme|Globex)\b"
        ),
    ),
)


def _span(text: str, start: int, end: int) -> dict[str, Any]:
    return {"text": text[start:end], "confidence": 0.9, "start": start, "end": end}


def _entities(text: str) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for label, words in (("person", PEOPLE), ("location", PLACES), ("organization", ORGS)):
        for word in words:
            for match in re.finditer(rf"\b{re.escape(word)}\b", text):
                out.setdefault(label, []).append(_span(text, match.start(), match.end()))
    return out


class FakeGliner:
    def __init__(self, *, broken: bool = False) -> None:
        self.broken = broken
        self.calls = 0

    def extract(
        self,
        text: str,
        schema: Any,
        *,
        threshold: float,
        include_confidence: bool,
        include_spans: bool,
    ) -> dict[str, Any]:
        self.calls += 1
        assert include_confidence and include_spans
        if self.broken:  # offsets that do not match the source text
            return {
                "entities": {"person": [{"text": "Zed", "confidence": 0.9, "start": 0, "end": 3}]}
            }
        relations: dict[str, list[dict[str, Any]]] = {}
        for name, pattern in _RELATIONS:
            for match in pattern.finditer(text):
                relations.setdefault(name, []).append(
                    {
                        "head": _span(text, match.start("head"), match.end("head")),
                        "tail": _span(text, match.start("tail"), match.end("tail")),
                    }
                )
        return {"entities": _entities(text), "relation_extraction": relations}

    def extract_entities(
        self,
        text: str,
        labels: list[str],
        *,
        include_confidence: bool = False,
        include_spans: bool = False,
    ) -> dict[str, Any]:
        found = {k: v for k, v in _entities(text).items() if k in labels}
        return {"entities": found}


class FakeBinding:
    """The GLiNER pool binding's surface: ``call`` runs one SDK operation on the model."""

    def __init__(self, model: FakeGliner) -> None:
        self.model = model

    async def call(self, callback: Any) -> Any:
        return callback(self.model)
