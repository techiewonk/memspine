# ruff: noqa: E501
"""W2-11: the asset describer (W-23). ``write.asset_describer`` off | ingest | on_read, default OFF.

Model-free: the hash embedder and stub describers. All text is invented (Ilse, Bram, Noor, Tomasz).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from memspine.api.engine import Engine
from memspine.exceptions import ConfigError
from memspine.ingest import asset_describer as ad
from memspine.store import lineage as lineage_store

NS = "acme/ilse"
PNG = b"\x89PNG\r\n\x1a\n" + b"hive-picture-bytes" * 50


class StubDescriber:
    key = "stub-vision:abc123"

    def __init__(self, gate: asyncio.Event | None = None) -> None:
        self.calls = 0
        self.gate = gate

    async def describe(self, info: ad.AssetInfo) -> ad.Description:
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        return ad.Description(
            f"A beehive painted green, file {info.filename}.", "stub-vision", "abc123"
        )


async def build(db: Path, **write: Any) -> Engine:
    cfg: dict[str, Any] = {"intake_v2": True, "lineage_table": True, **write}
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": str(db)},
        embedding={"provider": "hash"},
        memories={
            "episodic": {"enabled": True},
            "semantic": {"enabled": True},
            "resource": {"enabled": True},
        },
        write=cfg,
    )
    await eng.start()
    return eng


@pytest.fixture
def png(tmp_path: Path) -> Path:
    p = tmp_path / "hive.png"
    p.write_bytes(PNG)
    return p


async def resources(eng: Engine) -> list[Any]:
    return await eng._require_started().list_records(NS, "resource")


def descriptions(recs: list[Any]) -> list[Any]:
    return [r for r in recs if ad.DESC_TAG in r.tags]


async def test_flag_off_is_unchanged(tmp_path: Path, png: Path) -> None:
    eng = await build(tmp_path / "a.db")
    try:
        assert eng._config().write.asset_describer == "off"
        with pytest.raises(ConfigError, match="asset_describer"):
            await eng.ingest_asset(png, NS)
        assert await resources(eng) == []
        await eng.write("Ilse keeps bees behind the mill", namespace=NS)
        hits = await eng.search("bees", namespace=NS)
        assert hits and all(ad.ASSET_TAG not in r.tags for r, _ in hits)
    finally:
        await eng.stop()


async def test_needs_intake_v2(tmp_path: Path, png: Path) -> None:
    # FU-REQS: the missing prerequisite is now a config error at load, not a late verb refusal
    with pytest.raises(ConfigError, match="intake_v2"):
        await build(tmp_path / "b.db", asset_describer="ingest", intake_v2=False)


async def test_ingest_trigger_writes_description_with_lineage(tmp_path: Path, png: Path) -> None:
    eng = await build(tmp_path / "c.db", asset_describer="ingest")
    stub = StubDescriber()
    eng._asset_describer = stub
    try:
        asset = await eng.ingest_asset(png, NS)
        assert asset is not None and ad.ASSET_TAG in asset.tags
        assert asset.source.doc_path == str(png)  # the reference, not a copy
        assert PNG[:16].decode("latin-1") not in asset.content  # never the bytes
        assert any(t.startswith("asset_hash:") and len(t) > 20 for t in asset.tags)
        descs = descriptions(await resources(eng))
        assert len(descs) == 1 and stub.calls == 1
        d = descs[0]
        assert d.source.parents == [asset.record_id]
        assert d.source.channel == ad.DESCRIPTION_CHANNEL
        assert d.trust <= ad.DESCRIPTION_TRUST
        edges = await lineage_store.parents_of(eng._require_started()._client.engine, [d.record_id])
        assert asset.record_id in str(edges)
        # the same asset again: stored once, described once
        assert await eng.ingest_asset(png, NS) is None
        assert len(descriptions(await resources(eng))) == 1 and stub.calls == 1
    finally:
        await eng.stop()


async def test_on_read_describes_once_and_caches(tmp_path: Path, png: Path) -> None:
    eng = await build(tmp_path / "d.db", asset_describer="on_read")
    stub = StubDescriber()
    eng._asset_describer = stub
    try:
        asset = await eng.ingest_asset(png, NS)
        assert asset is not None
        assert descriptions(await resources(eng)) == [] and stub.calls == 0  # nothing at ingest
        hits = await eng.search("asset hive.png", namespace=NS)
        ids = [r.record_id for r, _ in hits]
        assert asset.record_id in ids
        assert stub.calls == 1
        descs = descriptions(await resources(eng))
        assert len(descs) == 1 and descs[0].record_id in ids  # served with the read
        await eng.search("asset hive.png", namespace=NS)
        await eng.search("asset hive.png", namespace=NS)
        assert stub.calls == 1 and len(descriptions(await resources(eng))) == 1
    finally:
        await eng.stop()


async def test_ingest_caption_wins_over_read(tmp_path: Path, png: Path) -> None:
    eng = await build(tmp_path / "e.db", asset_describer="ingest")
    eng._asset_describer = StubDescriber()
    try:
        await eng.ingest_asset(png, NS)
        asset = next(r for r in await resources(eng) if ad.ASSET_TAG in r.tags)
        other = StubDescriber()  # a read on an already described asset never calls the model
        eng._asset_describer = other
        assert await ad.describe_and_deposit(eng, NS, asset, trigger="on_read") is None
        assert other.calls == 0
    finally:
        await eng.stop()


async def test_timeout_does_not_block_the_read(tmp_path: Path, png: Path) -> None:
    eng = await build(tmp_path / "f.db", asset_describer="on_read", asset_describe_timeout_s=0.1)
    gate = asyncio.Event()
    stub = StubDescriber(gate)
    eng._asset_describer = stub
    try:
        asset = await eng.ingest_asset(png, NS)
        assert asset is not None
        t0 = time.perf_counter()
        hits = await eng.search("asset hive.png", namespace=NS)
        assert time.perf_counter() - t0 < 2.0
        assert asset.record_id in [r.record_id for r, _ in hits]
        assert descriptions([r for r, _ in hits]) == []  # returned without a description
        await eng.search("asset hive.png", namespace=NS)  # queued once, not twice
        assert stub.calls == 1
        gate.set()  # the queued work finishes later
        await asyncio.gather(*list(eng._asset_tasks.values()))
        assert len(descriptions(await resources(eng))) == 1
    finally:
        gate.set()
        await eng.stop()


async def test_metadata_fallback_makes_no_model_call(tmp_path: Path, png: Path) -> None:
    eng = await build(tmp_path / "g.db", asset_describer="ingest")
    try:
        assert isinstance(ad.resolve_describer(eng), ad.MetadataDescriber)
        asset = await eng.ingest_asset(png, NS, alt_text="Noor green hive")
        assert asset is not None
        (d,) = descriptions(await resources(eng))
        assert "hive.png" in d.content and "image/png" in d.content
        assert str(len(PNG)) in d.content and "Noor green hive" in d.content
        assert f"{ad.KEY_PREFIX}{ad.METADATA_KEY}" in d.tags
    finally:
        await eng.stop()


async def test_llm_describer_uses_role_and_falls_back_on_empty() -> None:
    class Chat:
        provider_id = "fake"

        def __init__(self, reply: str) -> None:
            self.reply = reply
            self.seen: list[Any] = []

        async def chat(self, messages: list[Any], **_: Any) -> str:
            self.seen.append(messages)
            return self.reply

    class Prompt:
        id = "describe_asset"
        prompt_version = "describe_asset@1"

        def render(self, ctx: dict[str, Any]) -> list[dict[str, str]]:
            return [
                {"role": "system", "content": "s"},
                {"role": "user", "content": ctx["filename"]},
            ]

    info = ad.AssetInfo("h" * 64, "https://example.org/hive.png", "image/png", 10)
    chat = Chat("A hive.")
    d = await ad.LLMDescriber(chat, Prompt(), "vision-x").describe(info)
    assert d.text == "A hive." and d.key.startswith("vision-x:")
    part = chat.seen[0][-1]["content"]
    assert part[1]["image_url"]["url"] == info.uri  # a remote image goes by URL
    empty = await ad.LLMDescriber(Chat("  "), Prompt(), "vision-x").describe(info)
    assert empty.model == "metadata"


async def test_erasing_the_asset_erases_its_description(tmp_path: Path, png: Path) -> None:
    eng = await build(tmp_path / "h.db", asset_describer="ingest")
    eng._asset_describer = StubDescriber()
    try:
        asset = await eng.ingest_asset(png, NS)
        assert asset is not None
        (d,) = descriptions(await resources(eng))
        await eng.forget(asset.record_id, namespace=NS, hard=True)
        storage = eng._require_started()
        assert await storage.get_record(asset.record_id) is None
        assert await storage.get_record(d.record_id) is None
        assert descriptions(await resources(eng)) == []
    finally:
        await eng.stop()
