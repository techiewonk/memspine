"""G9: ``ingest_messages`` embeds its turns in chunked batches.

The turns that reach the door are embedded up front, in calls of at most
``embedding.batch_size`` texts; the per-record firewall and vector projection
then read the vectors from the embedding cache. A failed up-front embedding
raises before any turn is written.
"""

from __future__ import annotations

from typing import Any

import pytest

from memspine.api.engine import Engine
from memspine.providers.builtin.models import HashEmbeddingSettings
from memspine.providers.contracts import BuildResult, ProviderRef
from memspine.providers.registry import EMBEDDERS, ProviderDescriptor, get_registry
from memspine.services.embedding.base import EmbeddingService
from memspine.services.embedding.hash import HashEmbedding

_TURNS = [
    "Caroline: I went to the LGBTQ support group yesterday.",
    "Melanie: That sounds meaningful, how did it go?",
    "Caroline: It was powerful, I felt accepted.",
    "Melanie: I painted a sunrise over the lake last week.",
    "Caroline: I am researching adoption agencies now.",
]


class SpyEmbedder(HashEmbedding):
    """Hash embedder that records every batch it is asked to embed."""

    def __init__(self, dim: int = 64, fail_on_batch: bool = False) -> None:
        super().__init__(dim=dim)
        self.calls: list[list[str]] = []
        self._fail_on_batch = fail_on_batch

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self._fail_on_batch and len(texts) > 1:
            raise RuntimeError("embedding provider unavailable")
        return await super().embed(texts)


async def _engine(
    monkeypatch: pytest.MonkeyPatch,
    spy: SpyEmbedder,
    embedding: dict[str, Any] | None = None,
    **overrides: Any,
) -> Engine:
    class SpyBuilder:
        async def build(self, settings: Any, ctx: Any) -> BuildResult[EmbeddingService]:
            return BuildResult(
                spy, ProviderRef("embedding", "test_spy"), (), frozenset({"embedding"})
            )

    descriptor = ProviderDescriptor(
        ref=ProviderRef("embedding", "test_spy"),
        settings_model=HashEmbeddingSettings,
        declared_capabilities=frozenset({"embedding"}),
        requirements=(),
        builder=SpyBuilder(),
        target_identity=lambda _settings: {
            "embedder_id": spy.embedder_id,
            "dimension": spy.dim,
        },
    )
    get_registry(
        "embedders"
    )  # ensure built-ins are loaded before temporarily adding the test adapter
    monkeypatch.setitem(EMBEDDERS._items, "test_spy", descriptor)
    embedding_config: dict[str, Any] = {"provider": "test_spy", "settings": {}}
    embedding_config.update(embedding or {})
    engine = Engine(
        template="core",
        dotenv_path=None,
        storage={"provider": "sqlite", "settings": {"path": ":memory:", "busy_timeout_s": 5.0}},
        embedding=embedding_config,
        memories={"episodic": {"enabled": True}},
        **overrides,
    )
    await engine.start()
    return engine


def _messages() -> list[dict[str, str]]:
    # The comparison uses two independent engines; stable identities ensure their
    # generated identity tags are part of the comparison rather than noise.
    return [
        {
            "role": "user",
            "content": text,
            "message_id": f"batch-message-{i}",
            "session_id": "batch-session",
            "conversation_id": "batch-conversation",
        }
        for i, text in enumerate(_TURNS)
    ]


async def _neighbours(engine: Engine, ns: str) -> list[tuple[str, float]]:
    """Content and score of every stored vector against a fixed probe."""
    assert engine._embedder is not None and engine._vector is not None
    [probe] = await HashEmbedding(dim=64).embed(["adoption support painting"])
    hits = await engine._vector.query(
        ns, probe, embedder_id=engine._embedder.embedder_id, top_k=len(_TURNS)
    )
    out = []
    for hit in hits:
        record = await engine._require_started().get_record(hit.record_id)
        assert record is not None
        out.append((record.content, round(hit.score, 6)))
    return sorted(out)


async def test_ingest_messages_makes_one_embedding_call(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = SpyEmbedder()
    engine = await _engine(monkeypatch, spy)
    try:
        records = await engine.ingest_messages(_messages(), namespace="chat/batch")
        assert len(records) == len(_TURNS)
        assert spy.calls == [_TURNS]  # one call; firewall + projector hit the cache
    finally:
        await engine.stop()


async def test_batched_records_and_vectors_match_single_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batched_spy, single_spy = SpyEmbedder(), SpyEmbedder()
    batched = await _engine(monkeypatch, batched_spy)
    single = await _engine(monkeypatch, single_spy)
    try:
        got = await batched.ingest_messages(_messages(), namespace="chat/x")
        want = []
        for message in _messages():
            want.extend(await single.ingest_messages([message], namespace="chat/x"))
        assert len(single_spy.calls) == len(_TURNS)  # unbatched: one call per turn

        def shape(records: list[Any]) -> list[tuple[Any, ...]]:
            return [
                (
                    r.content,
                    r.memory_type,
                    r.trust,
                    r.quarantined,
                    [tag for tag in r.tags if not tag.startswith("idem:")],
                    r.source.role,
                )
                for r in records
            ]

        assert shape(got) == shape(want)
        assert await _neighbours(batched, "chat/x") == await _neighbours(single, "chat/x")
    finally:
        await batched.stop()
        await single.stop()


async def test_batch_larger_than_batch_size_is_chunked(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = SpyEmbedder()
    engine = await _engine(monkeypatch, spy, embedding={"batch_size": 2})
    try:
        await engine.ingest_messages(_messages(), namespace="chat/chunk")
        assert [len(call) for call in spy.calls] == [2, 2, 1]
        assert [text for call in spy.calls for text in call] == _TURNS
    finally:
        await engine.stop()


async def test_single_write_embeds_exactly_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = SpyEmbedder()
    engine = await _engine(monkeypatch, spy)
    try:
        await engine.write(_TURNS[0], namespace="chat/one", memory_type="episodic")
        await engine.ingest_messages(_messages()[1:2], namespace="chat/one")
        assert spy.calls == [[_TURNS[0]], [_TURNS[1]]]
    finally:
        await engine.stop()


async def test_skipped_turns_are_not_embedded(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = SpyEmbedder()
    engine = await _engine(monkeypatch, spy, firewall={"skip_message_roles": ["system"]})
    try:
        messages = [{"role": "system", "content": "You are helpful."}, *_messages()[:2]]
        records = await engine.ingest_messages(messages, namespace="chat/skip")
        assert len(records) == 2
        assert spy.calls == [_TURNS[:2]]
    finally:
        await engine.stop()


async def test_failed_batch_embedding_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Documented choice: the batch fails cleanly. The up-front embedding runs
    before the first deposit, so a failure leaves no partial transcript that a
    retry would duplicate."""
    spy = SpyEmbedder(fail_on_batch=True)
    engine = await _engine(monkeypatch, spy)
    try:
        with pytest.raises(RuntimeError, match="embedding provider unavailable"):
            await engine.ingest_messages(_messages(), namespace="chat/fail")
        assert await engine.retrieve("chat/fail") == []
    finally:
        await engine.stop()
