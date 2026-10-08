"""I4: ``vector.isolation: per_namespace`` keeps one LanceDB table per namespace."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("lancedb")

from memspine import Engine
from memspine.config.schema import VectorConfig
from memspine.services.vector.per_namespace import PerNamespaceVectorStore


def _engine(path: Path, isolation: str) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": str(path / "m.db")},
        embedding={"provider": "hash"},
        vector={"isolation": isolation},
        read={"record_access": False},
    )


def test_shared_is_the_default() -> None:
    assert VectorConfig().isolation == "shared"


async def test_each_namespace_gets_its_own_table_and_results_match(tmp_path: Path) -> None:
    shared = _engine(tmp_path / "shared", "shared")
    split = _engine(tmp_path / "split", "per_namespace")
    for eng in (shared, split):
        await eng.start()
    try:
        for eng in (shared, split):
            for ns, text in (("alice", "camping by the lake"), ("bob", "skiing in the alps")):
                await eng.write(text, namespace=ns)
        assert isinstance(split._vector, PerNamespaceVectorStore)
        names = [n for n in split._lance.db.table_names() if "__ns_" in n]
        assert len(names) == 2  # one table per user
        for ns in ("alice", "bob"):
            a = await shared.search("camping lake skiing alps", namespace=ns, top_k=5)
            b = await split.search("camping lake skiing alps", namespace=ns, top_k=5)
            assert [r.content for r, _ in a] == [r.content for r, _ in b]
            assert all(r.namespace == ns for r, _ in b)
    finally:
        for eng in (shared, split):
            await eng.stop()


async def test_forget_and_erase_namespace_reach_the_right_table(tmp_path: Path) -> None:
    eng = _engine(tmp_path, "per_namespace")
    await eng.start()
    try:
        rec = await eng.write("camping by the lake", namespace="alice")
        await eng.write("skiing in the alps", namespace="bob")
        assert await eng._vector.exists(rec.record_id)
        await eng.forget(rec.record_id, namespace="alice", hard=True)
        assert not await eng._vector.exists(rec.record_id)
        await eng.erase_namespace("bob")
        names = [n for n in eng._lance.db.table_names() if "__ns_" in n]
        assert len(names) == 1  # bob's table was dropped
        assert await eng.search("skiing", namespace="bob") == []
    finally:
        await eng.stop()
