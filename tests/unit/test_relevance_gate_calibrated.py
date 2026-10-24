"""Current relevance probes, optional gate defaults and raw-score abstention."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine.api.engine import Engine
from memspine.config.schema import ReadConfig
from memspine.core.relevance_probes import OFF_TOPIC_PROBES
from memspine.observability.trace import search_forensics
from memspine.providers.builtin.models import register as register_models
from memspine.providers.contracts import BuildResult, ProviderRef
from memspine.providers.registry import EMBEDDERS
from memspine.services.embedding.base import EmbeddingService
from memspine.services.embedding.hash import HashEmbedding

T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)
_STOP = frozenset(
    [
        "the",
        "a",
        "an",
        "of",
        "to",
        "is",
        "are",
        "was",
        "in",
        "on",
        "at",
        "and",
        "or",
        "what",
        "how",
        "do",
        "does",
        "did",
        "i",
        "it",
        "for",
        "me",
    ]
)

_MEMORIES = [
    "Caroline: I moved from my home country Sweden four years ago",
    "Caroline: I am researching adoption agencies for my family",
    "Melanie: I painted a sunrise over the lake last week",
    "Melanie: my pottery class meets every Tuesday evening",
    "Caroline: the support group helped me feel accepted",
]
_RELATED = "Caroline moved from her home country Sweden to adopt a family"
_UNRELATED = "What is the weather forecast for the football match tomorrow?"


class TopicEmbedder(HashEmbedding):
    """Content-token hash embedder; ``offset`` adds a shared component so every pair of texts
    has a high baseline cosine (a different 'embedder' with a different irrelevant level)."""

    def __init__(self, dim: int = 2048, offset: float = 0.0) -> None:
        super().__init__(dim=dim)
        self._offset = offset

    @property
    def embedder_id(self) -> str:
        return f"topic:{self._dim}:{self._offset}"

    def _embed_one(self, text: str) -> list[float]:
        words = [w.strip("?.,:!").lower() for w in text.split()]
        vec = super()._embed_one(" ".join(w for w in words if w and w not in _STOP))
        vec[0] += self._offset
        norm = math.sqrt(sum(c * c for c in vec))
        return [c / norm for c in vec]


async def _engine(
    monkeypatch: pytest.MonkeyPatch,
    embedder: EmbeddingService | None = None,
    reranker: str | None = None,
    **read: Any,
) -> Engine:
    emb = embedder or TopicEmbedder()
    chain = [{"provider": reranker, "settings": {}}] if reranker else []

    class Builder:
        async def build(self, settings: Any, context: Any) -> BuildResult[EmbeddingService]:
            return BuildResult(
                service=emb,
                provider=ProviderRef("embedding", "hash"),
                resources=(),
                effective_capabilities=frozenset({"embedding"}),
            )

    register_models()  # the built-in descriptors, so the file also passes when run alone
    current = EMBEDDERS.get("hash")
    monkeypatch.setitem(
        EMBEDDERS._items,
        "hash",
        replace(
            current,
            builder=Builder(),
            target_identity=lambda _settings: {
                "embedder_id": emb.embedder_id,
                "dimension": emb.dim,
            },
        ),
    )
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}, "rerank": {"chain": chain}},
        memories={"episodic": {"enabled": True}},
        read={"record_access": False, "relevance_gate_bypass": "none", **read},
    )
    await eng.start()
    return eng


async def _seed(eng: Engine, texts: list[str], ns: str = "a") -> None:
    for i, text in enumerate(texts):
        await eng.write(
            text, namespace=ns, memory_type="episodic", valid_from=T0 + timedelta(minutes=i)
        )


# -- arithmetic -------------------------------------------------------------------------------


def test_probe_set_is_fixed_and_generic() -> None:
    assert len(OFF_TOPIC_PROBES) == 20 and len(set(OFF_TOPIC_PROBES)) == 20


def test_defaults_are_off_and_portable() -> None:
    read = ReadConfig()
    assert read.gate is False
    assert read.relevance_gate_margin_sd == 1.0
    assert read.relevance_gate_leg == "auto"
    assert read.abstain_on_raw is False


# -- the gate ---------------------------------------------------------------------------------


async def test_gate_off_by_default_injects_always(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = await _engine(monkeypatch)
    try:
        await _seed(eng, _MEMORIES)
        with search_forensics() as fx:
            out = await eng.read(_UNRELATED, namespace="a", mode="replay")
        assert out.context.records
        assert "relevance_calibration" not in fx
    finally:
        await eng.stop()


# -- I30: raw-score abstention ----------------------------------------------------------------


class FakeReranker:
    """A reranker whose raw score is the share of query words found in the document."""

    reranker_id = "fake-raw"

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        q = {w.strip("?.,").lower() for w in query.split()} - _STOP
        return [
            len(q & {w.strip("?.,:").lower() for w in d.split()}) / max(len(q), 1)
            for d in documents
        ]


async def _rerank_engine(monkeypatch: pytest.MonkeyPatch, **read: Any) -> Engine:
    eng = await _engine(monkeypatch, reranker="flashrank", **read)
    eng._reranker = FakeReranker()  # type: ignore[assignment]
    return eng


async def test_abstain_on_raw_fires_under_rerank(monkeypatch: pytest.MonkeyPatch) -> None:
    """Min-max makes the top reranked candidate 1.0, so the abstain test never fires; on the
    raw scores an unrelated query does abstain."""
    base = await _rerank_engine(monkeypatch)
    try:
        await _seed(base, _MEMORIES)
        out = await base.read(_UNRELATED, namespace="a", mode="retrieve")
        assert out.context.records and not out.context.abstained  # the dead-theta defect
    finally:
        await base.stop()
    eng = await _rerank_engine(monkeypatch, abstain_on_raw=True)
    try:
        await _seed(eng, _MEMORIES)
        with search_forensics() as fx:
            out = await eng.read(_UNRELATED, namespace="a", mode="retrieve")
        assert out.context.abstained and not out.context.records
        assert fx["abstain_on_raw"]["raw_top"] < 0.25
        out = await eng.read(_RELATED, namespace="a", mode="retrieve")
        assert out.context.records and not out.context.abstained
    finally:
        await eng.stop()
