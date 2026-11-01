"""RM-154 (ADR-090 item 6, ADR-095): the window annotation paths.

* The model path runs the GLiNER pre-pass only on live snapshot inputs: a stale source's text
  never reaches the finder, so it is never cached either; a forgotten source's cached hints go.
* The producer revision derives from configuration, never from what the extractor found: every
  batch of an item (an empty result and an excluded source too) carries the same revision.

Model-free: the DS-04 fake window LLM and fake pre-pass finder, and the DS-18 fake GLiNER2."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "derive"))
import test_ds04_model_annotations as ds04
import test_ds18_gliner_fact_extractor as ds18
from gliner_fake import FakeGliner

from memspine.derive.gliner_mentions import MentionSpan


class RecordingFinder(ds04.Finder):
    """The DS-04 fake finder, recording every text it was run on."""

    texts: list[str] = []

    async def aentities(self, text: str) -> tuple[MentionSpan, ...]:
        RecordingFinder.texts.append(text)
        return await super().aentities(text)


def _stale(eng: Any, content: str) -> list[str]:
    """Make the source whose content is ``content`` stale: the worker now reads it changed, so it
    no longer matches its membership snapshot; returns the ids it was applied to."""
    assert eng._windows is not None
    worker = eng._windows
    original: Callable[..., Any] = worker._get
    hit: list[str] = []

    async def get(record_id: str) -> Any:
        record = await original(record_id)
        if record is not None and record.content == content:
            hit.append(record_id)
            return record.model_copy(update={"content": content + " (edited)"})
        return record

    worker._get = get
    return hit


async def test_model_path_runs_gliner_only_on_live_sources_and_evicts_forgotten_hints(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    RecordingFinder.texts = []
    monkeypatch.setattr(ds04, "Finder", RecordingFinder)
    eng = await ds04._engine(monkeypatch, tmp_path / "stale.db", ds04.LineLLM())
    try:
        stale_text = str(ds04.MESSAGES[1]["content"])
        records = await eng.ingest_messages(
            ds04.MESSAGES, namespace="a", session_id="s1", conversation_id="s1"
        )
        ids = [r.record_id for r in records]
        hit = _stale(eng, stale_text)
        await eng.flush_windows("a")
        assert hit and set(hit) == {ids[1]}
        # the stale source's text never reached the GLiNER finder (0 calls)
        assert RecordingFinder.texts
        assert sum(stale_text in text for text in RecordingFinder.texts) == 0
        batches = ds04._batches(eng)
        assert batches[ids[1]].mentions == ()  # the explicit empty batch of the excluded source
        assert {b.producer_revision for b in batches.values()} == {"prepass:fake-gliner:1"}

        pre = eng._window_prepass
        assert pre is not None and pre._cache
        assert ("a", ids[0]) in pre._keys_by_record
        await eng.forget(ids[0], namespace="a", hard=True)
        # the forgotten source's cached hints are gone; no cached hint set was computed from it
        assert ("a", ids[0]) not in pre._keys_by_record
        assert all(("a", ids[0]) not in held for held in pre._records_by_key.values())
        assert set(pre._cache) == set(pre._records_by_key)
    finally:
        await eng.stop()


MESSAGES = [
    {
        "message_id": "r-1",
        "role": "user",
        "sender": {"id": "Mira"},
        "content": "ok, thanks.",
        "created_at": "2026-10-24T10:00:00+00:00",
    },
    {
        "message_id": "r-2",
        "role": "user",
        "sender": {"id": "Mira"},
        "content": "I live in Lisbon.",
        "created_at": "2026-10-24T10:01:00+00:00",
    },
    {
        "message_id": "r-3",
        "role": "user",
        "sender": {"id": "Mira"},
        "content": "Bram works for Acme.",
        "created_at": "2026-10-24T10:02:00+00:00",
    },
]


async def test_rules_revision_is_configuration_derived_for_every_batch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await ds18._engine(monkeypatch, tmp_path / "rev.db", FakeGliner())
    try:
        records = await eng.ingest_messages(
            MESSAGES, namespace="a", session_id="s1", conversation_id="s1"
        )
        ids = [r.record_id for r in records]
        hit = _stale(eng, str(MESSAGES[2]["content"]))
        await eng.flush_windows("a")
        assert set(hit) == {ids[2]}
        batches = ds18._batches(eng)
        assert set(batches) == set(ids)
        assert batches[ids[0]].mentions == ()  # GLiNER found nothing here
        assert batches[ids[2]].mentions == ()  # excluded (stale) source
        revisions = {b.producer_revision for b in batches.values()}
        assert len(revisions) == 1  # an empty result is still the configured extractor's
        [revision] = revisions
        assert revision.startswith("personal-rules:1+gliner2:")
    finally:
        await eng.stop()
