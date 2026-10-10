"""I3 grounded_generic prompt, I10 generic query instruction arm, I32 no-record hint."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from memspine_evals.contracts import ReaderAnswer
from memspine_evals.no_record import (
    NO_RECORD_NOTE,
    NoRecordHintReader,
    asserts_past_event,
    event_supported,
)
from memspine_evals.readers import QA_PROMPTS

ARMS = Path(__file__).resolve().parents[1] / "arms"


def test_grounded_generic_renders_with_question_date() -> None:
    text = QA_PROMPTS["grounded_generic"].format(
        context="[2024-01-02] I moved to Oslo.", question="Where do I live?", question_date="2024-03-01"
    )
    assert "2024-03-01" in text and "Where do I live?" in text and "Oslo" in text
    assert "Not mentioned in the conversation" in text
    assert "most recent" in text and "optional context" in text


def test_grounded_generic_has_no_benchmark_names() -> None:
    low = QA_PROMPTS["grounded_generic"].lower()
    for name in ("locomo", "longmemeval", "op-bench", "opbench", "beam", "prefeval", "lamp"):
        assert name not in low
    assert "best-supported answer even if" not in low  # not the anti-abstention wording


def test_generic_instruction_arm() -> None:
    from memspine.config.constants import GENERIC_QWEN_QUERY_INSTRUCTION

    arm = json.loads((ARMS / "best-generic-prompt.json").read_text(encoding="utf-8"))
    best = json.loads((ARMS / "BEST_dev_2026-10-10.json").read_text(encoding="utf-8"))
    assert arm["embedding"]["query_instruction"] == GENERIC_QWEN_QUERY_INSTRUCTION
    assert "past conversations" not in GENERIC_QWEN_QUERY_INSTRUCTION
    assert arm["read"] == best["read"] and arm["storage"] == best["storage"]
    assert best["embedding"]["query_instruction"] != GENERIC_QWEN_QUERY_INSTRUCTION
    assert "grounded_generic" in (ARMS / "best-generic-prompt.flags").read_text(encoding="utf-8")


def test_detector_and_support() -> None:
    assert asserts_past_event("Do you remember when I told you about my trip to Lisbon?")
    assert asserts_past_event("Remember the time we discussed the garden plans?")
    assert not asserts_past_event("What is my favourite colour?")
    assert event_supported("Do you remember my trip to Lisbon?", "[2024-01-02] My trip to Lisbon was fun")
    assert not event_supported("Do you remember my trip to Lisbon?", "[2024-01-02] I like tea")


class _Echo:
    reader_id = "echo"
    model = "none"
    makes_model_calls = False

    def __init__(self) -> None:
        self.seen: list[str] = []

    def describe(self) -> dict[str, object]:
        return {"reader_id": "echo"}

    async def answer(self, question: str, context: str, question_date: str | None = None) -> ReaderAnswer:
        self.seen.append(context)
        return ReaderAnswer(text="ok")


def test_hint_added_only_for_unsupported_asserted_event() -> None:
    inner = _Echo()
    reader = NoRecordHintReader(inner)
    q = "Do you remember when I climbed Everest?"
    flagged = asyncio.run(reader.answer(q, "[2024-01-02] I like tea"))
    assert inner.seen[-1].startswith(NO_RECORD_NOTE) and flagged.extra_meta["no_record_hint"]
    asyncio.run(reader.answer(q, "[2024-01-02] I climbed Everest last year"))
    assert NO_RECORD_NOTE not in inner.seen[-1]
    asyncio.run(reader.answer("What do I like?", "[2024-01-02] I like tea"))
    assert NO_RECORD_NOTE not in inner.seen[-1]
    assert (reader.asserted, reader.flagged) == (2, 1)
    assert reader.describe()["no_record_hint"] is True


def test_detector_is_swappable() -> None:
    inner = _Echo()
    reader = NoRecordHintReader(inner, detector=lambda q: True, support=lambda q, c: False)
    asyncio.run(reader.answer("anything", "ctx"))
    assert NO_RECORD_NOTE in inner.seen[0]
