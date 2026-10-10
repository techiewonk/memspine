"""E04: attachment identity, the controlled adapter (fake fetcher, fake vision), the cue.

No network, no model."""

from __future__ import annotations

import hashlib
import urllib.request

import pytest
from memspine.services.assets import (
    AssetAdapter,
    AssetEntry,
    AssetFetchError,
    AssetRegistry,
    FetchedAsset,
    HttpAssetFetcher,
    NoopVision,
    VisionResult,
    asset_id_for,
    needs_visual_detail,
    parse_attachments,
    sniff_mime,
)
from memspine.services.assets.fetch import _SameHostRedirect

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
        caption="a photo of a book",
        search_hint="children's book cover",  # a hint, never evidence
    )


def build(tmp_path, fetcher, vision):
    registry = AssetRegistry(tmp_path)
    return AssetAdapter(registry, fetcher, vision, tmp_path), registry


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


def test_registry_is_idempotent_and_keeps_resolved_state(tmp_path) -> None:
    reg = AssetRegistry(tmp_path)
    e = entry()
    reg.register(e)
    e.evidence_status, e.evidence = "ok", "seen"
    reg.update(e)
    again = reg.register(entry())  # re-ingest of the same turn
    assert again.evidence == "seen"
    reg2 = AssetRegistry(tmp_path)  # persisted
    assert reg2.get(e.asset_id).evidence == "seen"


# ── adapter ──────────────────────────────────────────────────────────────────


async def test_resolve_downloads_once_caches_by_hash_and_records_provenance(tmp_path) -> None:
    fetcher, vision = FakeFetcher({"https://img.test/a.jpg": JPEG}), FakeVision()
    adapter, reg = build(tmp_path, fetcher, vision)
    e = reg.register(entry())
    got = await adapter.resolve(e.asset_id, allow_network=True)
    again = await adapter.resolve(e.asset_id, allow_network=True)
    digest = hashlib.sha256(JPEG).hexdigest()
    assert got.availability == "cached" and got.content_hash == digest
    assert got.evidence_status == "ok" and "Charlotte" in got.evidence
    assert got.provenance == f"asset:{digest[:16]}"
    assert again.evidence == got.evidence
    assert fetcher.calls == ["https://img.test/a.jpg"] and vision.calls == 1
    assert (tmp_path / "blobs" / digest[:2] / digest).read_bytes() == JPEG


async def test_same_bytes_from_two_turns_are_described_once(tmp_path) -> None:
    fetcher = FakeFetcher({"https://img.test/a.jpg": JPEG, "https://img.test/b.jpg": JPEG})
    vision = FakeVision()
    adapter, reg = build(tmp_path, fetcher, vision)
    a = reg.register(entry("https://img.test/a.jpg", "D1:5"))
    b = reg.register(entry("https://img.test/b.jpg", "D2:3"))
    await adapter.resolve(a.asset_id, allow_network=True)
    got = await adapter.resolve(b.asset_id, allow_network=True)
    assert got.evidence_status == "ok" and vision.calls == 1


async def test_cached_only_mode_makes_no_network_call(tmp_path) -> None:
    fetcher, vision = FakeFetcher({"https://img.test/a.jpg": JPEG}), FakeVision()
    adapter, reg = build(tmp_path, fetcher, vision)
    e = reg.register(entry())
    got = await adapter.resolve(e.asset_id, allow_network=False)
    assert got.availability == "unfetched" and got.evidence_status == "none"
    assert fetcher.calls == [] and vision.calls == 0


@pytest.mark.parametrize(
    "reason", ["http_404", "timeout", "too_large", "mime_mismatch", "redirected_offsite"]
)
async def test_failed_download_is_explicit_and_final(tmp_path, reason) -> None:
    fetcher = FakeFetcher(fail={"https://img.test/a.jpg": reason})
    adapter, reg = build(tmp_path, fetcher, FakeVision())
    e = reg.register(entry())
    got = await adapter.resolve(e.asset_id, allow_network=True)
    again = await adapter.resolve(e.asset_id, allow_network=True)
    assert got.availability == "unavailable" and got.error.startswith(reason)
    assert got.evidence_status == "none" and got.evidence is None
    assert again.availability == "unavailable"
    assert fetcher.calls == ["https://img.test/a.jpg"]  # not retried, nothing substituted
    assert adapter.stats.fetch_failures == 1


async def test_no_vision_backend_is_skipped_explicitly(tmp_path) -> None:
    adapter, reg = build(tmp_path, FakeFetcher({"https://img.test/a.jpg": JPEG}), NoopVision())
    e = reg.register(entry())
    got = await adapter.resolve(e.asset_id, allow_network=True)
    assert got.availability == "cached" and got.evidence_status == "skipped"
    assert got.error == "no_vision_backend" and got.evidence is None


async def test_vision_outage_is_recorded_and_retried_from_the_cached_bytes(tmp_path) -> None:
    fetcher = FakeFetcher({"https://img.test/a.jpg": JPEG})
    vision = FakeVision(fail=True)
    adapter, reg = build(tmp_path, fetcher, vision)
    e = reg.register(entry())
    first = await adapter.resolve(e.asset_id, allow_network=True)
    assert first.evidence_status == "failed" and "vision_error" in first.error
    vision.fail = False
    second = await adapter.resolve(e.asset_id, allow_network=True)
    assert second.evidence_status == "ok"
    assert fetcher.calls == ["https://img.test/a.jpg"]  # bytes came from the cache


async def test_empty_description_is_a_failure_not_evidence(tmp_path) -> None:
    adapter, reg = build(tmp_path, FakeFetcher({"https://img.test/a.jpg": JPEG}), FakeVision(" "))
    e = reg.register(entry())
    got = await adapter.resolve(e.asset_id, allow_network=True)
    assert got.evidence_status == "failed" and got.error == "empty_description"


async def test_search_hint_is_stored_but_never_evidence(tmp_path) -> None:
    adapter, reg = build(
        tmp_path, FakeFetcher({"https://img.test/a.jpg": JPEG}), FakeVision("a desk")
    )
    e = reg.register(entry())
    got = await adapter.resolve(e.asset_id, allow_network=True)
    assert got.search_hint == "children's book cover"
    assert "children" not in got.evidence and "children" not in (got.error or "")


async def test_unknown_asset_resolves_to_none(tmp_path) -> None:
    adapter, _ = build(tmp_path, FakeFetcher(), FakeVision())
    assert await adapter.resolve("nope", allow_network=True) is None


# ── the real fetcher's local guards (no network) ─────────────────────────────


def test_sniff_mime() -> None:
    assert sniff_mime(JPEG) == "image/jpeg" and sniff_mime(PNG) == "image/png"
    assert sniff_mime(b"RIFF\x00\x00\x00\x00WEBPxxxx") == "image/webp"
    assert sniff_mime(b"<html>") is None


def test_unsupported_scheme_is_refused_before_any_io() -> None:
    fetcher = HttpAssetFetcher(("image/jpeg",))
    with pytest.raises(AssetFetchError) as err:
        fetcher._get("file:///etc/passwd", 100, 1.0)
    assert err.value.reason == "unsupported_scheme"


def test_cross_host_redirect_is_refused() -> None:
    handler = _SameHostRedirect()
    req = urllib.request.Request("https://img.test/a.jpg")
    with pytest.raises(AssetFetchError) as err:
        handler.redirect_request(req, None, 302, "Found", {}, "https://placeholder.other/gone.png")
    assert err.value.reason == "redirected_offsite"


# ── cue ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "question",
    [
        "What is the title of the book Melanie showed?",
        "Which painting did Caroline share?",
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
        "When did Caroline go to the support group?",
        "How many children does Melanie have?",
        "Would Caroline enjoy classical music?",
        "Did John move to Seattle?",
    ],
)
def test_text_questions_do_not_fire(question: str) -> None:
    assert not needs_visual_detail(question)
