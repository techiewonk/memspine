"""Real SQLite/zstd and Engine assembly compression acceptance.

The lossy compressor is a deterministic test double at the optional model
boundary. Token accounting uses the Engine's documented chars/4 estimator;
budgets are derived from the exact expected output count, with no padding.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from memspine import Engine
from memspine.compose import compression as compression_module
from memspine.compose.pack import estimate_tokens
from memspine.derive.pipeline.stages.maintenance import compress as compress_stage
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import SourceInfo

EVENT_TIME = datetime(2025, 4, 2, 9, 30, tzinfo=UTC)


def _engine(path: Path, *, read: dict[str, Any] | None = None) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        deployment={"id": f"compression-acceptance-{path.stem}"},
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": str(path), "busy_timeout_s": 5.0},
            },
            "vector": {"provider": "lancedb", "settings": {"isolation": "shared"}},
            "cache": {"provider": "memory", "settings": {}},
        },
        model={"embedding": {"provider": "hash", "settings": {"dim": 64}}},
        read=read or {"hybrid": True},
        memories={
            "semantic": {"enabled": True},
            "episodic": {"enabled": True},
        },
    )


@pytest.mark.asyncio
async def test_sqlite_cold_tier_zstd_roundtrip_preserves_content_and_metadata(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path / "cold-tier.sqlite")
    content = "Quarterly access review evidence " * 80
    source = SourceInfo(
        role="user",
        channel="messages",
        message_id="message-2025-04-02-42",
        principal="sender-42",
        parents=["source-turn-41"],
    )
    await engine.start()
    try:
        written = await engine.write(
            content,
            namespace="audit-team",
            memory_type="episodic",
            source=source,
            tags=["sender_id:sender-42", "spk:sender-42"],
            valid_from=EVENT_TIME,
        )
        await engine._append_and_project(
            MemoryEvent(
                kind=EventKind.DECAY_TRANSITION,
                namespace="audit-team",
                actor="system",
                payload={
                    "record_id": written.record_id,
                    "set": {"tier": "dormant"},
                    "transition": "hot->dormant",
                    "reason": "acceptance_setup",
                },
            )
        )

        result = await compress_stage(engine._pipeline_ctx())
        assert result["compressed"] == 1

        assert engine._storage is not None
        packed = await engine._storage.get_record(written.record_id)
        assert packed is not None
        assert packed.content == ""
        assert packed.content_zstd is not None
        assert packed.content_fingerprint == written.content_fingerprint

        # The normal public read inflates the durable representation, byte for
        # byte at the UTF-8 text level, while preserving provenance and times.
        restored = next(
            record
            for record in await engine.retrieve("audit-team", memory_type="episodic")
            if record.record_id == written.record_id
        )
        assert restored.content == content
        assert restored.content_zstd is None
        assert restored.content_fingerprint == written.content_fingerprint
        assert restored.valid_from == EVENT_TIME
        assert restored.source == source
        assert restored.tags == written.tags
        assert restored.axes == written.axes
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_engine_assembly_compresses_text_without_dropping_evidence_or_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read = {
        "hybrid": True,
        "render": "plain",
        "reply_reserve_tokens": 0,
        "assembly": {"theta_abstain": 0.0, "mmr_lambda": 1.0},
        "compression": {"assembly": True, "assembly_stage": ["llmlingua"]},
    }
    engine = _engine(tmp_path / "assembly.sqlite", read=read)
    evidence_line = (
        "Evidence: Acme renewal date 2025-04-02; speaker Mira; message msg-42; source sender-42."
    )
    background = "routine context " * 240
    content = f"{evidence_line}\nBackground: {background}"
    compressed_text = f"{evidence_line}\nBackground summary: routine context"
    source = SourceInfo(
        role="user",
        channel="messages",
        message_id="msg-42",
        principal="sender-42",
        parents=["source-turn-41"],
    )

    calls: list[str] = []

    def deterministic_compressor(text: str) -> str:
        calls.append(text)
        head, _tail = text.split("\nBackground: ", 1)
        # This boundary stub represents deterministic lossy block compression:
        # retain the complete evidence line and collapse repetitive background.
        return f"{head}\nBackground summary: routine context"

    monkeypatch.setattr(
        compression_module,
        "_load_llmlingua",
        lambda rate=0.5, **kwargs: deterministic_compressor,
    )

    await engine.start()
    try:
        original = await engine.write(
            content,
            namespace="audit-team",
            memory_type="episodic",
            source=source,
            tags=["sender_id:sender-42", "spk:sender-42"],
            valid_from=EVENT_TIME,
        )
        assert estimate_tokens(content) > estimate_tokens(compressed_text)

        # The configured production fallback uses estimate_tokens (chars/4 +
        # one). Set the budget to the exact count of the deterministic output.
        # There is no hand-added slack or post-hoc record deletion.
        budget = estimate_tokens(compressed_text)
        context = await engine.assemble(
            "Acme renewal date 2025-04-02",
            namespace="audit-team",
            budget_tokens=budget,
            top_k=1,
        )

        assert calls == [content]
        assert len(context.records) == 1
        included = context.records[0]
        assert included.record_id == original.record_id
        assert included.content == compressed_text
        assert included.content_fingerprint == original.content_fingerprint
        assert included.valid_from == EVENT_TIME
        assert included.source == source
        assert included.tags == original.tags
        assert included.axes == original.axes
        assert included.source.parents == ["source-turn-41"]
        assert "Acme renewal date 2025-04-02" in included.content
        assert "speaker Mira" in included.content
        assert "message msg-42" in included.content
        assert "source sender-42" in included.content
        assert context.tokens_used == budget
        assert context.tokens_used <= budget
        assert (await engine.retrieve("audit-team", memory_type="episodic"))[0].content == content
    finally:
        await engine.stop()
