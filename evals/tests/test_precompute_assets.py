"""E04 pre-compute pass: fakes only (no network, no vision model)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import precompute_assets as pa
from memspine.services.assets import (
    AssetAdapter,
    AssetFetchError,
    AssetRegistry,
    FetchedAsset,
    VisionResult,
    asset_id_for,
)

JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-bytes"


class FakeFetcher:
    def __init__(self, fail: set[str] = frozenset()) -> None:  # type: ignore[assignment]
        self.fail = set(fail)
        self.calls: list[str] = []

    async def fetch(self, uri: str, *, max_bytes: int, timeout_s: float) -> FetchedAsset:
        self.calls.append(uri)
        if uri in self.fail:
            raise AssetFetchError("http_404", uri)
        return FetchedAsset(data=JPEG + uri.encode(), mime="image/jpeg")


class FakeVision:
    name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def describe(self, data: bytes, mime: str) -> VisionResult:
        self.calls += 1
        return VisionResult(text="a book titled Charlotte's Web")


def _turn(tid: str, *uris: str) -> SimpleNamespace:
    return SimpleNamespace(turn_id=tid, meta={"attachments": [{"uri": u} for u in uris]})


def _items() -> list[SimpleNamespace]:
    plain = SimpleNamespace(turn_id="D1:1", meta={})
    return [
        SimpleNamespace(
            history=(plain, _turn("D1:2", "https://i/a.jpg", "https://i/b.jpg")),
        ),
        SimpleNamespace(history=(_turn("D2:1", "https://i/dead.jpg"),)),
    ]


def _adapter(tmp: Path, fetcher: FakeFetcher, vision: FakeVision) -> AssetAdapter:
    return AssetAdapter(AssetRegistry(tmp), fetcher, vision, tmp)


def test_iter_assets_uses_engine_asset_ids() -> None:
    ids = [e.asset_id for e in pa.iter_assets(_items())]
    assert ids[0] == asset_id_for("D1:2", "https://i/a.jpg") and len(ids) == 3


def test_download_then_describe_is_resumable_and_idempotent(tmp_path: Path) -> None:
    fetcher, vision = FakeFetcher({"https://i/dead.jpg"}), FakeVision()
    adapter = _adapter(tmp_path, fetcher, vision)
    r1 = asyncio.run(pa.precompute(pa.iter_assets(_items()), adapter, phase="download"))
    assert (r1["images"], r1["downloaded"], r1["failed"], r1["described"]) == (3, 2, 1, 0)
    assert r1["failure_reasons"] == {"http_404": 1} and vision.calls == 0
    # second run, new adapter on the same dir: no fetch at all (failure is final)
    fetcher2 = FakeFetcher()
    r2 = asyncio.run(
        pa.precompute(pa.iter_assets(_items()), _adapter(tmp_path, fetcher2, vision), phase="all")
    )
    assert fetcher2.calls == [] and (r2["downloaded"], r2["failed"], r2["described"]) == (2, 1, 2)
    assert vision.calls == 2
    r3 = asyncio.run(
        pa.precompute(pa.iter_assets(_items()), _adapter(tmp_path, fetcher2, vision), phase="all")
    )
    assert vision.calls == 2 and r3["described"] == 2 and "total_s" in r3["timing_s"]
    entry = AssetRegistry(tmp_path).get(asset_id_for("D1:2", "https://i/a.jpg"))
    assert entry and entry.evidence_status == "ok" and entry.content_hash


def test_retry_failed_refetches_only_failures(tmp_path: Path) -> None:
    f = FakeFetcher({"https://i/dead.jpg"})
    asyncio.run(pa.precompute(pa.iter_assets(_items()), _adapter(tmp_path, f, FakeVision()), phase="download"))
    f2 = FakeFetcher()
    r = asyncio.run(
        pa.precompute(
            pa.iter_assets(_items()),
            _adapter(tmp_path, f2, FakeVision()),
            phase="download",
            retry_failed=True,
        )
    )
    assert f2.calls == ["https://i/dead.jpg"] and r["failed"] == 0 and r["downloaded"] == 3


def test_limit_and_describe_without_download_does_nothing(tmp_path: Path) -> None:
    vision = FakeVision()
    r = asyncio.run(
        pa.precompute(
            pa.iter_assets(_items()), _adapter(tmp_path, FakeFetcher(), vision), phase="describe", limit=2
        )
    )
    assert r["images"] == 2 and r["described"] == 0 and vision.calls == 0