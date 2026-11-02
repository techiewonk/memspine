"""DS-04 (ADR-090 item 6): the model-backed ``window_extract`` path attaches one annotation batch
per source whatever the fact yield, like the rules-only path. Model-free: a fake window LLM and
a fake GLiNER pre-pass finder (the same ``aentities`` interface the GLiNER hint client has)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.derive.gliner_mentions import MentionSpan
from memspine.derive.prepass import Prepass
from memspine.schemas.durable import AnnotationBatch
from memspine.services.llm.base import LLMRouter

LINE = re.compile(r"^\[(\d+)\] \[[\d-]+\] (.+)$", re.M)


class LineLLM:
    """A window LLM that cites only the lines in ``cite`` (none: zero facts)."""

    def __init__(self, cite: set[int] | None = None) -> None:
        self.cite = cite or set()
        self.calls = 0

    @property
    def provider_id(self) -> str:
        return "fake:window"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        user = messages[-1]["content"]
        if re.search(r"sections: (.+)\.$", user.strip(), re.M | re.I) is None:
            return "[]"
        self.calls += 1
        facts = [
            {
                "s": text.split(":")[0],
                "r": "event",
                "o": None,
                "text": f"{text.split(':')[0]} said {text.split(': ', 1)[1]}",
                "kind": "event",
                "mode": "asserted",
                "neg": False,
                "lines": [int(n)],
                "span": text.split(": ", 1)[1],
            }
            for n, text in LINE.findall(user)
            if int(n) in self.cite
        ]
        return json.dumps({"facts": facts})


class Finder:
    """A fake GLiNER pre-pass finder: every capitalised word after the speaker is a person."""

    producer_id = "fake-gliner:1"

    def __init__(self) -> None:
        self.calls = 0

    async def aentities(self, text: str) -> tuple[MentionSpan, ...]:
        self.calls += 1
        out = []
        for match in re.finditer(r"\b(Noor|Bram|Lisbon)\b", text):
            out.append(
                MentionSpan(
                    text=match.group(0),
                    label="location" if match.group(0) == "Lisbon" else "person",
                    start=match.start(),
                    end=match.end(),
                    confidence=0.9,
                )
            )
        return tuple(out)


async def _engine(monkeypatch: pytest.MonkeyPatch, path: Path, llm: LineLLM) -> Engine:
    async def router(
        config: MemspineConfig, hooks: object = None, wiring: object = None
    ) -> LLMRouter:
        return LLMRouter({"window_extract": llm})  # type: ignore[dict-item]

    monkeypatch.setattr("memspine.runtime.container.build_llm_router", router)
    eng = Engine(
        template="core",
        dotenv_path=None,
        deployment={"id": "test-deployment"},  # RM-155: shared LLM
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": str(path), "busy_timeout_s": 5.0},
            }
        },
        model={
            "embedding": {"provider": "hash", "settings": {}},
            "llm": {
                "window_extract": {
                    "provider": "openai_compatible",
                    "timeout_s": 60.0,
                    "settings": {"api_base": "http://test.invalid/v1", "model": "fake/window"},
                }
            },
        },
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        write={
            "window_extract": "facts",
            "window_async": False,
            "window_idle_s": 0.0,
            "window_max_turns": 3,
            "segment_max_turns": 3,
            "segment_gap_s": 0,
            "batch_max_windows": 1,
            "prepass": "rules",
        },
        workers={"outbox": True},
    )
    # The shared pre-pass the window pipeline picks up at start, with a fake GLiNER finder.
    eng._window_prepass = Prepass("gliner", gliner=Finder())
    await eng.start()
    return eng


MESSAGES = [
    {
        "message_id": "m-1",
        "role": "user",
        "sender": {"id": "Mira"},
        "content": "Mira: I had tea with Noor today.",
        "created_at": "2026-10-24T10:00:00+00:00",
    },
    {
        "message_id": "m-2",
        "role": "user",
        "sender": {"id": "Mira"},
        "content": "Mira: Bram is flying to Lisbon.",
        "created_at": "2026-10-24T10:01:00+00:00",
    },
    {
        "message_id": "m-3",
        "role": "user",
        "sender": {"id": "Mira"},
        "content": "Mira: ok, thanks.",
        "created_at": "2026-10-24T10:02:00+00:00",
    },
]


def _batches(eng: Engine, ns: str = "a") -> dict[str, AnnotationBatch]:
    assert eng._windows is not None
    out: dict[str, AnnotationBatch] = {}
    for row in eng._windows.derivation_effect_status(ns):
        if row["layer"] != "annotations" or row.get("state") != "complete":
            continue
        for raw in row["artifact"]["annotation_batches"]:
            batch = AnnotationBatch.model_validate(raw)
            out[batch.source.record_id] = batch
    return out


async def _ingest(eng: Engine) -> list[str]:
    records = await eng.ingest_messages(
        MESSAGES, namespace="a", session_id="s1", conversation_id="s1"
    )
    await eng.flush_windows("a")
    return [r.record_id for r in records]


async def test_zero_fact_window_still_annotates_every_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    llm = LineLLM()
    eng = await _engine(monkeypatch, tmp_path / "zero.db", llm)
    try:
        ids = await _ingest(eng)
        assert llm.calls >= 1
        batches = _batches(eng)
        assert set(batches) == set(ids)
        first, second, third = (batches[i] for i in ids)
        assert [m.text for m in first.mentions] == ["Noor"]
        assert [(m.text, m.label) for m in second.mentions] == [
            ("Bram", "person"),
            ("Lisbon", "location"),
        ]
        assert third.mentions == ()  # an explicit successful empty result
        for batch in batches.values():
            assert batch.producer_revision == "prepass:fake-gliner:1"
            assert all(m.asserted is False for m in batch.mentions)
            assert all(m.source_record_id == batch.source.record_id for m in batch.mentions)
            assert all(m.sender_id == "Mira" for m in batch.mentions)
        content = MESSAGES[1]["content"]
        assert all(content[m.start : m.end] == m.text for m in second.mentions)
    finally:
        await eng.stop()


async def test_uncited_source_keeps_its_annotations_beside_a_fact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    llm = LineLLM(cite={1})
    eng = await _engine(monkeypatch, tmp_path / "cited.db", llm)
    try:
        ids = await _ingest(eng)
        batches = _batches(eng)
        assert set(batches) == set(ids)
        # Line 2 is cited by no fact, its mentions are still recorded.
        assert [m.text for m in batches[ids[1]].mentions] == ["Bram", "Lisbon"]
    finally:
        await eng.stop()


async def test_annotations_survive_restart_and_go_with_an_erased_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "restart.db"
    eng = await _engine(monkeypatch, db, LineLLM())
    try:
        ids = await _ingest(eng)
        assert set(_batches(eng)) == set(ids)
    finally:
        await eng.stop()

    eng = await _engine(monkeypatch, db, LineLLM())
    try:
        restored = _batches(eng)
        assert set(restored) == set(ids)
        assert [m.text for m in restored[ids[1]].mentions] == ["Bram", "Lisbon"]
        await eng.forget(ids[1], namespace="a", hard=True)
        remaining = _batches(eng)
        assert ids[1] not in remaining
        assert not any(
            m.source_record_id == ids[1] for batch in remaining.values() for m in batch.mentions
        )
    finally:
        await eng.stop()
