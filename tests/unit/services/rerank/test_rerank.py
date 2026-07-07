"""E8 rerank port (D-42 §5/D-51): strategy text + adapter guards."""

from __future__ import annotations

import pytest

from memspine.core.records import MemoryRecord, SourceInfo
from memspine.exceptions import MissingServiceError
from memspine.services.rerank.base import Reranker, concat_background


def test_concat_background_prepends_structural_context() -> None:
    record = MemoryRecord(
        namespace="n",
        memory_type="semantic",
        content="it rotates monthly",
        entity="deploy-password",
        attribute="rotation",
        source=SourceInfo(channel="web", doc_path="ops/runbook.md"),
    )
    text = concat_background(record)
    assert text.endswith("it rotates monthly")
    header = text.split("\n", 1)[0]
    for expected in (
        "type: semantic",
        "entity: deploy-password",
        "attribute: rotation",
        "channel: web",
        "doc: ops/runbook.md",
    ):
        assert expected in header


def test_concat_background_minimal_record_still_carries_type() -> None:
    record = MemoryRecord(namespace="n", memory_type="episodic", content="plain")
    assert concat_background(record) == "[type: episodic]\nplain"


def test_flashrank_adapter_names_the_extra_when_absent() -> None:
    try:
        import flashrank  # noqa: F401
    except ImportError:
        pass
    else:  # pragma: no cover - env with the extra installed
        pytest.skip("flashrank installed — the import-gate branch is unreachable")
    from memspine.services.rerank.flashrank_rerank import FlashrankReranker

    with pytest.raises(MissingServiceError, match=r"memspine\[rerank\]"):
        FlashrankReranker()


async def test_a_duck_typed_fake_satisfies_the_port() -> None:
    class Fake:
        reranker_id = "fake"

        async def rerank(self, query: str, documents: list[str]) -> list[float]:
            return [float(len(doc)) for doc in documents]

    fake = Fake()
    assert isinstance(fake, Reranker)
    assert await fake.rerank("q", ["a", "bbb"]) == [1.0, 3.0]
