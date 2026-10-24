"""RM-113 item 18: the asset registry in the records database, bytes in ``storage.documents``.

Through the Engine: ingest registers the attachment rows in the records database, a ``fetch``
read downloads through the real fetcher (an in-process HTTP transport) into the documents slot
and describes it, a restart finds the evidence (``cached``), erasure removes rows and bytes, and
a pre-item-18 ``registry.sqlite`` + ``blobs/`` directory is imported once. No network, no model.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from memspine import Engine
from memspine.clients.http import HttpConnector, HttpSpec
from memspine.config.schema import MemspineConfig
from memspine.exceptions import ConfigError
from memspine.services.assets import SqlAssetRegistry, asset_id_for
from memspine.services.vision import VisionResult

JPEG = b"\xff\xd8\xff\xe0" + b"9" * 64
DIGEST = hashlib.sha256(JPEG).hexdigest()
URL = "https://img.test/book.jpg"
QUESTION = "What is the title of the book Melanie shared?"

TURNS = [
    {"role": "user", "content": "Melanie: I love reading before bed", "turn_id": "D1:1"},
    {
        "role": "user",
        "content": "Melanie: my favourite bedtime read lately [image: a photo of a book]",
        "turn_id": "D1:2",
        "attachments": [{"uri": URL, "caption": "a photo of a book"}],
    },
    {"role": "user", "content": "Caroline: pottery class was relaxing", "turn_id": "D1:3"},
]


class Vision:
    name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def describe(self, data: bytes, mime: str) -> VisionResult:
        self.calls += 1
        return VisionResult("A hardcover titled 'Charlotte's Web'.")


def _config(tmp_path: Path, mode: str, *, path: str, documents: bool = True) -> dict[str, Any]:
    storage: dict[str, Any] = {
        "records": {"provider": "sqlite", "settings": {"path": path, "busy_timeout_s": 5.0}}
    }
    if documents:
        storage["documents"] = {
            "provider": "filesystem",
            "settings": {"root": str(tmp_path / "documents"), "fsync": False},
        }
    return {
        "template": "core",
        "dotenv_path": None,
        "storage": storage,
        "model": {"embedding": {"provider": "hash", "settings": {}}},
        "memories": {"episodic": {"enabled": True}},
        "ingest": {"assets": "on"},
        "read": {"hybrid": False, "record_access": False, "asset_evidence": mode},
    }


async def _engine(
    tmp_path: Path, mode: str = "fetch", *, path: str, calls: list[str] | None = None
) -> tuple[Engine, Vision]:
    eng = Engine(**_config(tmp_path, mode, path=path))
    await eng.start()
    seen = calls if calls is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=JPEG)

    # the real fetcher over an in-process transport; the vision port is a fake
    eng._http = HttpConnector(HttpSpec(transport=httpx.MockTransport(handler)))
    vision = Vision()
    eng._asset_adapter().vision = vision
    return eng, vision


def _evidence(out: Any) -> list[str]:
    return [r.content for r in out.context.records if r.content.startswith("[image evidence")]


def _db_rows(path: Path) -> list[tuple[Any, ...]]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as con:
            return list(
                con.execute(
                    sa.text(
                        "SELECT namespace, asset_id, availability, content_hash FROM asset_registry"
                    )
                )
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize("store", ["memory", "file"])
async def test_ingest_fetch_describe_and_read_evidence(tmp_path: Path, store: str) -> None:
    path = ":memory:" if store == "memory" else str(tmp_path / "records.db")
    calls: list[str] = []
    eng, vision = await _engine(tmp_path, path=path, calls=calls)
    try:
        assert isinstance(eng._asset_registry(), SqlAssetRegistry)
        await eng.ingest_messages(TURNS, namespace="a", session_id="s1")
        rows = await eng._asset_registry().all("a")
        assert [(r.source_turn_id, r.uri, r.availability) for r in rows] == [
            ("D1:2", URL, "unfetched")
        ]
        out = await eng.read(QUESTION, namespace="a", mode="retrieve", top_k=4)
        lines = _evidence(out)
        assert (
            len(lines) == 1 and "Charlotte's Web" in lines[0] and f"asset:{DIGEST[:16]}" in lines[0]
        )
        assert calls == [URL] and vision.calls == 1
        assert await eng.documents.get("a", DIGEST) == JPEG  # bytes in storage.documents
        head = await eng.documents.head("a", DIGEST)
        assert head is not None and head.record_ids == (rows[0].record_id,)
    finally:
        await eng.stop()
    if store == "file":  # the rows are a table of the records database, not a sidecar
        assert _db_rows(tmp_path / "records.db") == [
            ("a", asset_id_for("D1:2", URL), "cached", DIGEST)
        ]
        assert not list(tmp_path.glob("**/registry.sqlite"))


async def test_restart_reads_cached_evidence_without_network(tmp_path: Path) -> None:
    path = str(tmp_path / "records.db")
    eng, _ = await _engine(tmp_path, path=path)
    try:
        await eng.ingest_messages(TURNS, namespace="a", session_id="s1")
        await eng.read(QUESTION, namespace="a", mode="retrieve", top_k=4)
    finally:
        await eng.stop()
    calls: list[str] = []
    again, vision = await _engine(tmp_path, "cached", path=path, calls=calls)
    try:
        out = await again.read(QUESTION, namespace="a", mode="retrieve", top_k=4)
        assert len(_evidence(out)) == 1 and "Charlotte's Web" in _evidence(out)[0]
        assert calls == [] and vision.calls == 0
    finally:
        await again.stop()


async def test_hard_forget_and_namespace_erasure_remove_rows_and_bytes(tmp_path: Path) -> None:
    eng, _ = await _engine(tmp_path, path=str(tmp_path / "records.db"))
    try:
        for ns in ("a", "b"):
            await eng.ingest_messages(TURNS, namespace=ns, session_id="s1")
            await eng.read(QUESTION, namespace=ns, mode="retrieve", top_k=4)
        registry = eng._asset_registry()
        (row,) = await registry.all("a")
        assert await eng.documents.get("a", DIGEST) == JPEG
        await eng.forget(row.record_id, namespace="a", hard=True)
        assert await registry.all("a") == []
        assert await eng.documents.get("a", DIGEST) is None  # the record's bytes went too
        assert len(await registry.all("b")) == 1  # another namespace is untouched
        assert await eng.documents.get("b", DIGEST) == JPEG
        await eng.erase_namespace("b")
        assert await registry.all("b") == []
        assert await eng.documents.get("b", DIGEST) is None
    finally:
        await eng.stop()


async def test_soft_forget_keeps_the_rows(tmp_path: Path) -> None:
    eng, _ = await _engine(tmp_path, path=":memory:")
    try:
        await eng.ingest_messages(TURNS, namespace="a", session_id="s1")
        (row,) = await eng._asset_registry().all("a")
        await eng.forget(row.record_id, namespace="a")
        assert len(await eng._asset_registry().all("a")) == 1
    finally:
        await eng.stop()


def _pg_url() -> str | None:
    return os.environ.get("MEMSPINE_TEST_PG_URL") or os.environ.get("MEMSPINE_TEST_POSTGRES_URL")


@pytest.mark.skipif(_pg_url() is None, reason="set MEMSPINE_TEST_PG_URL to a scratch Postgres")
async def test_postgres_registry_rows_and_erasure(tmp_path: Path) -> None:
    """The registry is a table of the Postgres records database (Alembic head), shared by two
    Engines on it; hard forget and namespace erasure remove rows and bytes."""
    ns = f"pg-{uuid.uuid4().hex[:8]}"
    config = _config(tmp_path, "fetch", path=":memory:")
    config["storage"]["records"] = {
        "provider": "postgres",
        "settings": {"url": _pg_url(), "connect_timeout_s": 5.0, "statement_timeout_s": 30.0},
    }
    config["deployment"] = {"id": "rm113-assets", "data_dir": str(tmp_path / "derived")}
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=JPEG)

    eng = Engine(**config)
    other = Engine(**config)
    await eng.start()
    await other.start()
    try:
        eng._http = HttpConnector(HttpSpec(transport=httpx.MockTransport(handler)))
        eng._asset_adapter().vision = Vision()
        assert isinstance(eng._asset_registry(), SqlAssetRegistry)
        await eng.ingest_messages(TURNS, namespace=ns, session_id="s1")
        out = await eng.read(QUESTION, namespace=ns, mode="retrieve", top_k=4)
        assert "Charlotte's Web" in _evidence(out)[0] and calls == [URL]
        (row,) = await other._asset_registry().all(ns)  # shared on the database
        assert row.availability == "cached" and row.content_hash == DIGEST
        await eng.forget(row.record_id, namespace=ns, hard=True)
        assert await other._asset_registry().all(ns) == []
        assert await eng.documents.get(ns, DIGEST) is None
        await eng.ingest_messages(TURNS, namespace=ns, session_id="s2")
        assert len(await eng._asset_registry().all(ns)) == 1
        await eng.erase_namespace(ns)
        assert await eng._asset_registry().all(ns) == []
    finally:
        await other.stop()
        await eng.stop()


# ---------------------------------------------------------------------------- the import


# ---------------------------------------------------------------------------- config


def _cfg(**kwargs: Any) -> MemspineConfig:
    return MemspineConfig(
        runtime={
            "provider_services": {
                "health_timeout_s": 1.0,
                "close_timeout_s": 1.0,
                "startup_timeout_s": None,
            }
        },
        **kwargs,
    )


def test_fetch_needs_the_documents_slot() -> None:
    with pytest.raises(ConfigError, match="storage.documents"):
        _cfg(ingest={"assets": "on"}, read={"asset_evidence": "fetch"})
    cached = _cfg(ingest={"assets": "on"}, read={"asset_evidence": "cached"})
    assert cached.read.asset_evidence == "cached"
