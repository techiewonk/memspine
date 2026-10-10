"""I29 / I30 / I37: the store-calibrated relevance gate and raw-score abstention."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine.config.schema import MemspineConfig, ReadConfig
from memspine.core.relevance_probes import OFF_TOPIC_PROBES, calibrate, percentile
from memspine.engine import Engine, search_forensics
from memspine.services.embedding.base import EmbeddingService
from memspine.services.embedding.hash_local import HashEmbedding

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
    monkeypatch: pytest.MonkeyPatch, embedder: EmbeddingService | None = None, **read: Any
) -> Engine:
    emb = embedder or TopicEmbedder()

    def build(self: Engine, config: MemspineConfig) -> EmbeddingService:
        return emb

    monkeypatch.setattr(Engine, "_build_embedder", build)
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
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


def test_percentile_and_calibrate() -> None:
    assert percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50) == 3.0
    assert percentile([0.0, 10.0], 95) == pytest.approx(9.5)
    cal = calibrate("vector", [0.2, 0.4], 7)
    assert cal.mu == pytest.approx(0.3) and cal.sigma == pytest.approx(0.1)
    assert cal.threshold(1.0) == pytest.approx(cal.p95 + 0.1)
    assert cal.n_records == 7 and cal.n_probes == 2


def test_probe_set_is_fixed_and_generic() -> None:
    assert len(OFF_TOPIC_PROBES) == 20 and len(set(OFF_TOPIC_PROBES)) == 20


def test_defaults_are_off_and_portable() -> None:
    read = ReadConfig()
    assert read.relevance_gate == "off"
    assert read.relevance_gate_margin_sd == 1.0
    assert read.relevance_gate_leg == "auto"
    assert read.abstain_on_raw is False


# -- the gate ---------------------------------------------------------------------------------


async def test_unrelated_query_gets_an_empty_context_and_related_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng = await _engine(monkeypatch, relevance_gate="store_calibrated")
    try:
        await _seed(eng, _MEMORIES)
        with search_forensics() as fx:
            out = await eng.read(_UNRELATED, namespace="a", mode="replay")
        assert out.context.abstained and not out.context.records
        entry = fx["relevance_calibration"]
        assert entry["decision"] == "empty" and entry["margin_sd"] == 1.0
        leg = entry["legs"]["vector"]
        assert leg["n_probes"] == 20 and leg["raw_top"] <= leg["threshold"]
        assert {"mu", "sigma", "p95", "n_records"} <= set(leg)
        with search_forensics() as fx:
            out = await eng.read(_RELATED, namespace="a", mode="replay")
        assert out.context.records and not out.context.abstained
        assert fx["relevance_calibration"]["decision"] == "inject"
        assert fx["relevance_calibration"]["recalibrated"] is False  # cached
    finally:
        await eng.stop()


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


@pytest.mark.parametrize("offset", [0.0, 3.0])
@pytest.mark.parametrize("size", [5, 60])
async def test_self_calibrates_across_embedders_and_store_sizes(
    monkeypatch: pytest.MonkeyPatch, offset: float, size: int
) -> None:
    """The same fixed margin works when the embedder's irrelevant level shifts (offset) and
    when the store is larger: no per-store number."""
    eng = await _engine(
        monkeypatch, TopicEmbedder(offset=offset), relevance_gate="store_calibrated"
    )
    try:
        filler = [
            f"Dana: logged invoice number {i} for the carpentry workshop" for i in range(size - 5)
        ]
        await _seed(eng, [*_MEMORIES, *filler])
        with search_forensics() as fx:
            bad = await eng.read(_UNRELATED, namespace="a", mode="replay")
        assert bad.context.abstained and not bad.context.records
        mu = fx["relevance_calibration"]["legs"]["vector"]["mu"]
        with search_forensics():
            good = await eng.read(_RELATED, namespace="a", mode="replay")
        assert good.context.records
        if offset:
            assert mu > 0.5  # the shifted embedder's irrelevant level was learned, not assumed
    finally:
        await eng.stop()


async def test_recalibrates_when_the_store_more_than_doubles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng = await _engine(monkeypatch, relevance_gate="store_calibrated")
    try:
        await _seed(eng, _MEMORIES)
        with search_forensics() as fx:
            await eng.read(_RELATED, namespace="a", mode="replay")
        assert fx["relevance_calibration"]["recalibrated"] is True
        await _seed(eng, [f"Dana: invoice {i} for the carpentry shop" for i in range(3)])  # 8 <= 10
        with search_forensics() as fx:
            await eng.read(_RELATED, namespace="a", mode="replay")
        assert fx["relevance_calibration"]["recalibrated"] is False
        await _seed(eng, [f"Dana: invoice {i} for the plumbing shop" for i in range(10)])  # 18 > 10
        with search_forensics() as fx:
            await eng.read(_RELATED, namespace="a", mode="replay")
        entry = fx["relevance_calibration"]
        assert entry["recalibrated"] is True
        assert entry["legs"]["vector"]["n_records"] >= 18
    finally:
        await eng.stop()


async def test_calibration_is_per_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = await _engine(monkeypatch, relevance_gate="store_calibrated")
    try:
        await _seed(eng, _MEMORIES, "a")
        await _seed(eng, ["Zed: the garage door opener needs new batteries"], "b")
        with search_forensics() as fx:
            await eng.read(_RELATED, namespace="a", mode="replay")
        assert fx["relevance_calibration"]["legs"]["vector"]["n_records"] == len(_MEMORIES)
        with search_forensics() as fx:
            await eng.read("Zed garage door opener batteries", namespace="b", mode="replay")
        assert fx["relevance_calibration"]["legs"]["vector"]["n_records"] == 1
    finally:
        await eng.stop()


async def test_a_stricter_margin_blocks_a_weak_match(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = await _engine(monkeypatch, relevance_gate="store_calibrated")
    try:
        await _seed(eng, _MEMORIES)
        with search_forensics() as fx:
            await eng.read(_RELATED, namespace="a", mode="replay")
        leg = fx["relevance_calibration"]["legs"]["vector"]
        eng2 = await _engine(
            monkeypatch, relevance_gate="store_calibrated", relevance_gate_margin_sd=10.0
        )
        try:
            await _seed(eng2, _MEMORIES)
            with search_forensics() as fx2:
                await eng2.read(_RELATED, namespace="a", mode="replay")
            assert fx2["relevance_calibration"]["legs"]["vector"]["threshold"] > leg["threshold"]
        finally:
            await eng2.stop()
    finally:
        await eng.stop()


async def test_failure_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = await _engine(monkeypatch, relevance_gate="store_calibrated")
    try:
        await _seed(eng, _MEMORIES)

        async def boom(*_a: Any, **_k: Any) -> dict[str, float]:
            raise RuntimeError("scoring down")

        monkeypatch.setattr(eng, "_gate_raw_tops", boom)
        with search_forensics() as fx:
            out = await eng.read(_UNRELATED, namespace="a", mode="replay")
        assert out.context.records
        assert fx["relevance_calibration"]["decision"] == "inject"
        assert "scoring down" in fx["relevance_calibration"]["error"]
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
    eng = await _engine(monkeypatch, rerank="flashrank", **read)
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


async def test_calibrated_gate_reads_the_rerank_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = await _rerank_engine(monkeypatch, relevance_gate="store_calibrated")
    try:
        await _seed(eng, _MEMORIES)
        with search_forensics() as fx:
            bad = await eng.read(_UNRELATED, namespace="a", mode="retrieve")
        assert "rerank" in fx["relevance_calibration"]["legs"]
        assert bad.context.abstained
        good = await eng.read(_RELATED, namespace="a", mode="retrieve")
        assert good.context.records
    finally:
        await eng.stop()
