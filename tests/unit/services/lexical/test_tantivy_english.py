"""N58: the ``english`` lexical analyzer (stop words + stemmer), off by default."""

from __future__ import annotations

from pathlib import Path

import pytest

from memspine import Engine
from memspine.config.schema import ReadConfig
from memspine.core.records import MemoryRecord, SourceInfo
from memspine.services.lexical.tantivy import TantivyLexical


def rec(record_id: str, content: str, namespace: str = "a") -> MemoryRecord:
    return MemoryRecord(
        record_id=record_id,
        namespace=namespace,
        memory_type="semantic",
        content=content,
        source=SourceInfo(role="user"),
    )


def test_default_analyzer_is_unchanged() -> None:
    assert ReadConfig().lexical_analyzer == "default"


async def test_english_matches_inflected_forms() -> None:
    store = TantivyLexical(analyzer="english")
    await store.index(rec("r1", "Melanie went camping and painted a lake"))
    hits = await store.search("a", "does she camp or paint?")
    assert [h.record_id for h in hits] == ["r1"]


async def test_default_does_not_stem() -> None:
    store = TantivyLexical()
    await store.index(rec("r1", "Melanie went camping"))
    assert await store.search("a", "camp") == []


async def test_stop_words_alone_match_nothing() -> None:
    store = TantivyLexical(analyzer="english")
    await store.index(rec("r1", "the kids and the dog"))
    assert await store.search("a", "the and") == []


async def test_namespace_isolation_holds() -> None:
    store = TantivyLexical(analyzer="english")
    await store.index(rec("r1", "camping trip", namespace="a"))
    await store.index(rec("r2", "camping trip", namespace="b"))
    assert [h.record_id for h in await store.search("b", "camp")] == ["r2"]


def test_unknown_analyzer_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown lexical analyzer"):
        TantivyLexical(analyzer="klingon")


def _engine(path: Path, analyzer: str) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": str(path / "m.db")},
        embedding={"provider": "hash"},
        read={"hybrid": True, "lexical_analyzer": analyzer, "record_access": False},
    )


async def test_switching_on_rebuilds_the_english_index_from_the_log(tmp_path: Path) -> None:
    eng = _engine(tmp_path, "default")
    await eng.start()
    try:
        await eng.write("Melanie went camping by the lake", namespace="a")
    finally:
        await eng.stop()
    eng = _engine(tmp_path, "english")
    await eng.start()
    try:
        assert "lexical:english" in [p.name for p in eng._projectors]
        hits = await eng._lexical.search("a", "camp")  # type: ignore[union-attr]
    finally:
        await eng.stop()
    assert len(hits) == 1  # caught up from offset 0 under its own projector name
    assert (tmp_path / "m.db.tantivy").exists()  # the default index is left alone
