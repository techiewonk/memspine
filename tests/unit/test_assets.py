"""E04: attachment identity, the controlled adapter (fake fetcher, fake vision), the cue.

RM-113 item 18: the adapter runs over the registry port (in-process and SQL tables of a SQLite
records file) and keeps the bytes in a ``storage.documents`` store (filesystem). No network,
no model."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from memspine.clients.fs import FilesystemConnector, FilesystemSpec
from memspine.clients.http import HttpConnector, HttpSpec
from memspine.services.assets import (
    AssetAdapter,
    AssetEntry,
    AssetFetchError,
    FetchedAsset,
    HttpAssetFetcher,
    MemoryAssetRegistry,
    SqlAssetRegistry,
    asset_id_for,
    needs_visual_detail,
    parse_attachments,
    sniff_mime,
)
from memspine.services.assets.fetch import check_redirect
from memspine.services.documents.filesystem import FilesystemDocumentStore
from memspine.services.vision import NoopVision, VisionResult

NS = "a"

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"1" * 64


class FakeFetcher:
    def __init__(self, blobs: dict[str, bytes] | None = None, fail: dict[str, str] | None = None):
        self.blobs = blobs or {}
        self.fail = fail or {}
        self.calls: list[str] = []

    async def fetch(self, uri: str, *, max_bytes: int, timeout_s: float) -> FetchedAsset:
        self.calls.append(uri)
        if uri in self.fail:
            raise AssetFetchError(self.fail[uri], uri)
        data = self.blobs[uri]
        return FetchedAsset(data=data, mime=sniff_mime(data) or "image/jpeg")


class FakeVision:
    name = "fake-vision"

    def __init__(self, text: str = "A paperback titled Charlotte's Web on a desk.", fail=False):
        self.text = text
        self.fail = fail
        self.calls = 0

    async def describe(self, data: bytes, mime: str) -> VisionResult:
        self.calls += 1
        if self.fail:
            raise RuntimeError("vision down")
        return VisionResult(text=self.text)


def entry(uri: str = "https://img.test/a.jpg", turn: str = "D1:5") -> AssetEntry:
    return AssetEntry(
        asset_id=asset_id_for(turn, uri),
        source_turn_id=turn,
        uri=uri,
        namespace=NS,
        caption="a photo of a book",
        search_hint="children's book cover",  # a hint, never evidence
        record_id="r1",
    )


async def _documents(root: Path) -> FilesystemDocumentStore:
    connector = FilesystemConnector(FilesystemSpec(str(root), fsync=False))
    await connector.open()
    return FilesystemDocumentStore(connector)


@pytest.fixture(params=["memory", "sqlite"])
async def registry(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Any]:
    if request.param == "memory":
        yield MemoryAssetRegistry()
        return
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'records.db'}")
    try:
        yield SqlAssetRegistry(engine)
    finally:
        await engine.dispose()


@pytest.fixture
def build(registry: Any, tmp_path: Path) -> Any:
    async def make(fetcher: Any, vision: Any) -> tuple[AssetAdapter, Any]:
        documents = await _documents(tmp_path / "documents")
        return AssetAdapter(registry, fetcher, vision, documents), registry

    return make


# ── identity ─────────────────────────────────────────────────────────────────


def test_parse_attachments_keeps_identity_and_skips_garbage() -> None:
    refs = parse_attachments(
        [
            {
                "uri": "https://a.test/1.jpg",
                "caption": "c",
                "search_hint": "h",
                "re_download": True,
            },
            {"url": "https://a.test/2.jpg"},
            {"uri": "  "},
            "nope",
            {"caption": "no uri"},
        ]
    )
    assert [r.uri for r in refs] == ["https://a.test/1.jpg", "https://a.test/2.jpg"]
    assert refs[0].caption == "c" and refs[0].search_hint == "h"
    assert refs[0].meta == {"re_download": True}
    assert parse_attachments(None) == [] and parse_attachments("x") == []


def test_asset_id_is_stable_and_per_turn() -> None:
    assert asset_id_for("D1:5", "u") == asset_id_for("D1:5", "u")
    assert asset_id_for("D1:5", "u") != asset_id_for("D1:6", "u")


async def test_registry_is_idempotent_and_keeps_resolved_state(registry) -> None:
    e = entry()
    e.record_id = None
    await registry.register(e)
    e.evidence_status, e.evidence = "ok", "seen"
    await registry.update(e)
    again = await registry.register(entry())  # re-ingest of the same turn
    assert again.evidence == "seen" and again.record_id == "r1"  # only the record id filled
    assert (await registry.get(NS, e.asset_id)).evidence == "seen"
    assert await registry.get("other", e.asset_id) is None  # namespaces never share rows
    assert [x.asset_id for x in await registry.all(NS)] == [e.asset_id]


async def test_sql_registry_persists_in_the_records_file(tmp_path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'records.db'}"
    first = create_async_engine(url)
    try:
        e = entry()
        e.evidence_status, e.evidence = "ok", "seen"
        await SqlAssetRegistry(first).update(e)
    finally:
        await first.dispose()
    second = create_async_engine(url)
    try:
        assert (await SqlAssetRegistry(second).get(NS, e.asset_id)).evidence == "seen"
    finally:
        await second.dispose()


async def test_registry_erasure_by_record_and_namespace(registry) -> None:
    a = entry("https://img.test/a.jpg", "D1:5")
    b = entry("https://img.test/b.jpg", "D1:6")
    b.record_id = "r2"
    for e in (a, b):
        await registry.register(e)
    gone = await registry.erase_records(NS, ["r1"])
    assert [g.asset_id for g in gone] == [a.asset_id]
    assert [x.asset_id for x in await registry.all(NS)] == [b.asset_id]
    assert [g.asset_id for g in await registry.erase_namespace(NS)] == [b.asset_id]
    assert await registry.all(NS) == []


# ── adapter ──────────────────────────────────────────────────────────────────


async def test_resolve_downloads_once_caches_by_hash_and_records_provenance(build) -> None:
    fetcher, vision = FakeFetcher({"https://img.test/a.jpg": JPEG}), FakeVision()
    adapter, reg = await build(fetcher, vision)
    e = await reg.register(entry())
    got = await adapter.resolve(NS, e.asset_id, allow_network=True)
    again = await adapter.resolve(NS, e.asset_id, allow_network=True)
    digest = hashlib.sha256(JPEG).hexdigest()
    assert got.availability == "cached" and got.content_hash == digest
    assert got.evidence_status == "ok" and "Charlotte" in got.evidence
    assert got.provenance == f"asset:{digest[:16]}"
    assert again.evidence == got.evidence
    assert fetcher.calls == ["https://img.test/a.jpg"] and vision.calls == 1
    assert await adapter.documents.get(NS, digest) == JPEG  # storage.documents, by hash
    head = await adapter.documents.head(NS, digest)
    assert head.record_ids == ("r1",) and head.media_type == "image/jpeg"


async def test_same_bytes_from_two_turns_are_described_once(build) -> None:
    fetcher = FakeFetcher({"https://img.test/a.jpg": JPEG, "https://img.test/b.jpg": JPEG})
    vision = FakeVision()
    adapter, reg = await build(fetcher, vision)
    a = await reg.register(entry("https://img.test/a.jpg", "D1:5"))
    b = await reg.register(entry("https://img.test/b.jpg", "D2:3"))
    await adapter.resolve(NS, a.asset_id, allow_network=True)
    got = await adapter.resolve(NS, b.asset_id, allow_network=True)
    assert got.evidence_status == "ok" and vision.calls == 1


async def test_cached_only_mode_makes_no_network_call(build) -> None:
    fetcher, vision = FakeFetcher({"https://img.test/a.jpg": JPEG}), FakeVision()
    adapter, reg = await build(fetcher, vision)
    e = await reg.register(entry())
    got = await adapter.resolve(NS, e.asset_id, allow_network=False)
    assert got.availability == "unfetched" and got.evidence_status == "none"
    assert fetcher.calls == [] and vision.calls == 0


@pytest.mark.parametrize(
    "reason", ["http_404", "timeout", "too_large", "mime_mismatch", "redirected_offsite"]
)
async def test_failed_download_is_explicit_and_final(build, reason) -> None:
    fetcher = FakeFetcher(fail={"https://img.test/a.jpg": reason})
    adapter, reg = await build(fetcher, FakeVision())
    e = await reg.register(entry())
    got = await adapter.resolve(NS, e.asset_id, allow_network=True)
    again = await adapter.resolve(NS, e.asset_id, allow_network=True)
    assert got.availability == "unavailable" and got.error.startswith(reason)
    assert got.evidence_status == "none" and got.evidence is None
    assert again.availability == "unavailable"
    assert fetcher.calls == ["https://img.test/a.jpg"]  # not retried, nothing substituted
    assert adapter.stats.fetch_failures == 1


async def test_no_vision_backend_is_skipped_explicitly(build) -> None:
    adapter, reg = await build(FakeFetcher({"https://img.test/a.jpg": JPEG}), NoopVision())
    e = await reg.register(entry())
    got = await adapter.resolve(NS, e.asset_id, allow_network=True)
    assert got.availability == "cached" and got.evidence_status == "skipped"
    assert got.error == "no_vision_backend" and got.evidence is None


async def test_vision_outage_is_recorded_and_retried_from_the_cached_bytes(build) -> None:
    fetcher = FakeFetcher({"https://img.test/a.jpg": JPEG})
    vision = FakeVision(fail=True)
    adapter, reg = await build(fetcher, vision)
    e = await reg.register(entry())
    first = await adapter.resolve(NS, e.asset_id, allow_network=True)
    assert first.evidence_status == "failed" and "vision_error" in first.error
    vision.fail = False
    second = await adapter.resolve(NS, e.asset_id, allow_network=True)
    assert second.evidence_status == "ok"
    assert fetcher.calls == ["https://img.test/a.jpg"]  # bytes came from the cache


async def test_empty_description_is_a_failure_not_evidence(build) -> None:
    adapter, reg = await build(FakeFetcher({"https://img.test/a.jpg": JPEG}), FakeVision(" "))
    e = await reg.register(entry())
    got = await adapter.resolve(NS, e.asset_id, allow_network=True)
    assert got.evidence_status == "failed" and got.error == "empty_description"


async def test_search_hint_is_stored_but_never_evidence(build) -> None:
    adapter, reg = await build(FakeFetcher({"https://img.test/a.jpg": JPEG}), FakeVision("a desk"))
    e = await reg.register(entry())
    got = await adapter.resolve(NS, e.asset_id, allow_network=True)
    assert got.search_hint == "children's book cover"
    assert "children" not in got.evidence and "children" not in (got.error or "")


async def test_unknown_asset_resolves_to_none(build) -> None:
    adapter, _ = await build(FakeFetcher(), FakeVision())
    assert await adapter.resolve(NS, "nope", allow_network=True) is None


# ── the real fetcher's local guards (no network) ─────────────────────────────


def test_sniff_mime() -> None:
    assert sniff_mime(JPEG) == "image/jpeg" and sniff_mime(PNG) == "image/png"
    assert sniff_mime(b"RIFF\x00\x00\x00\x00WEBPxxxx") == "image/webp"
    assert sniff_mime(b"<html>") is None


def _fetcher(handler) -> HttpAssetFetcher:  # type: ignore[no-untyped-def]
    """The real fetcher over the shared HTTP connector with an in-process transport."""
    return HttpAssetFetcher(
        ("image/jpeg",), HttpConnector(HttpSpec(transport=httpx.MockTransport(handler)))
    )


async def test_unsupported_scheme_is_refused_before_any_io() -> None:
    calls: list[str] = []
    fetcher = _fetcher(lambda request: calls.append(str(request.url)))
    with pytest.raises(AssetFetchError) as err:
        await fetcher.fetch("file:///etc/passwd", max_bytes=100, timeout_s=1.0)
    assert err.value.reason == "unsupported_scheme" and not calls


def test_cross_host_redirect_is_refused() -> None:
    with pytest.raises(AssetFetchError) as err:
        check_redirect("https://img.test/a.jpg", "https://placeholder.other/gone.png")
    assert err.value.reason == "redirected_offsite"
    assert check_redirect("https://img.test/a.jpg", "/b.jpg") == "https://img.test/b.jpg"


async def test_fetch_follows_a_same_host_redirect_and_checks_the_bytes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a.jpg":
            return httpx.Response(302, headers={"location": "/b.jpg"})
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=JPEG)

    got = await _fetcher(handler).fetch("https://img.test/a.jpg", max_bytes=1000, timeout_s=1.0)
    assert got.mime == "image/jpeg" and got.data == JPEG


async def test_fetch_refuses_an_offsite_redirect_too_large_and_http_errors() -> None:
    def offsite(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"location": "https://placeholder.other/x.png"})

    def large(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=JPEG * 50)

    def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    for handler, reason in (
        (offsite, "redirected_offsite"),
        (large, "too_large"),
        (missing, "http_404"),
    ):
        with pytest.raises(AssetFetchError) as err:
            await _fetcher(handler).fetch("https://img.test/a.jpg", max_bytes=100, timeout_s=1.0)
        assert err.value.reason == reason


# ── cue ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "question",
    [
        "What is the title of the book Bram showed?",
        "Which painting did Noor share?",
        "What does the sign in the photo say?",
        "Where is the beach in the picture?",
        "What kind of flower is in the image?",
    ],
)
def test_visual_questions_fire(question: str) -> None:
    assert needs_visual_detail(question)


@pytest.mark.parametrize(
    "question",
    [
        "When did Noor go to the support group?",
        "How many children does Bram have?",
        "Would Noor enjoy classical music?",
        "Did Tomasz move to Seattle?",
    ],
)
def test_text_questions_do_not_fire(question: str) -> None:
    assert not needs_visual_detail(question)
