"""E04 + E03 through the engine: opt-in, off by default, never proves a private fact.

Fakes only (fetcher, vision, provider): no network, no model."""

from __future__ import annotations

from typing import Any

import pytest
from memspine import Engine
from memspine.config.schema import IngestConfig, MemspineConfig, ReadConfig
from memspine.exceptions import ConfigError
from memspine.services.assets import (
    AssetAdapter,
    AssetFetchError,
    AssetRegistry,
    FetchedAsset,
    VisionResult,
)
from memspine.services.external import PUBLIC_MARKER, ExternalSnippet

JPEG = b"\xff\xd8\xff\xe0" + b"7" * 64
URL = "https://img.test/book.jpg"

TURNS = [
    {
        "role": "user",
        "content": "Melanie: I love reading before bed with my kids",
        "turn_id": "D1:1",
    },
    {
        "role": "user",
        "content": (
            "Melanie: Look at this, my favourite bedtime read lately "
            "[image: a photo of a book]"
        ),
        "turn_id": "D1:2",
        "attachments": [
            {"uri": URL, "caption": "a photo of a book", "search_hint": "children's classic cover"}
        ],
    },
    {
        "role": "user",
        "content": "Caroline: pottery class was relaxing on Friday",
        "turn_id": "D1:3",
    },
    {
        "role": "user",
        "content": "Caroline: I listen to classical music while painting",
        "turn_id": "D1:4",
    },
]


class Fetcher:
    def __init__(self, fail: str | None = None) -> None:
        self.calls: list[str] = []
        self.fail = fail

    async def fetch(self, uri: str, *, max_bytes: int, timeout_s: float) -> FetchedAsset:
        self.calls.append(uri)
        if self.fail:
            raise AssetFetchError(self.fail, uri)
        return FetchedAsset(JPEG, "image/jpeg")


class Vision:
    name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def describe(self, data: bytes, mime: str) -> VisionResult:
        self.calls += 1
        return VisionResult("A hardcover titled 'Charlotte's Web' lying on a blanket.")


class Provider:
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def search(self, query: str, max_results: int) -> list[ExternalSnippet]:
        self.calls.append(query)
        return [ExternalSnippet("Classical music", "Caroline owns a violin and visited Paris.", "")]


async def make(tmp_path, *, ingest: dict[str, Any] | None = None, **read: Any) -> Engine:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        ingest=ingest or {},
        read={"hybrid": False, "record_access": False, **read},
    )
    await eng.start()
    await eng.write_messages(TURNS, namespace="a", session_id="s1")
    return eng


def contents(result: Any) -> list[str]:
    return [r.content for r in result.context.records]


# ── defaults ─────────────────────────────────────────────────────────────────


def test_everything_is_off_by_default() -> None:
    cfg = MemspineConfig()
    assert cfg.ingest.assets == "off" and cfg.read.asset_evidence == "off"
    assert cfg.read.external_evidence == "off" and cfg.read.external_provider == "none"
    assert IngestConfig().asset_vision == "none"


def test_config_rejects_inconsistent_asset_keys() -> None:
    with pytest.raises(ConfigError):
        MemspineConfig(read=ReadConfig(asset_evidence="cached"))
    with pytest.raises(ConfigError):
        MemspineConfig(ingest=IngestConfig(assets="on"), read=ReadConfig(asset_evidence="fetch"))


async def test_attachments_are_ignored_when_ingest_assets_is_off(tmp_path) -> None:
    eng = await make(tmp_path)
    try:
        records = await eng._records("a")
        assert not any(t.startswith("asset:") for r in records for t in r.tags)
        assert eng.asset_stats() == {}
    finally:
        await eng.stop()


# ── E04 ──────────────────────────────────────────────────────────────────────


async def asset_engine(tmp_path, fetcher: Fetcher, vision: Vision, mode: str = "fetch") -> Engine:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        ingest={"assets": "on", "asset_dir": str(tmp_path)},
        read={"hybrid": False, "record_access": False, "asset_evidence": mode},
    )
    await eng.start()
    eng.set_asset_adapter(AssetAdapter(AssetRegistry(tmp_path), fetcher, vision, tmp_path))
    await eng.write_messages(TURNS, namespace="a", session_id="s1")
    return eng


async def test_ingest_registers_identity_without_downloading(tmp_path) -> None:
    fetcher = Fetcher()
    eng = await asset_engine(tmp_path, fetcher, Vision())
    try:
        tagged = [r for r in await eng._records("a") if any(t.startswith("asset:") for t in r.tags)]
        assert len(tagged) == 1
        entries = eng._asset_adapter().registry.all()
        assert len(entries) == 1
        e = entries[0]
        assert (e.source_turn_id, e.uri, e.caption) == ("D1:2", URL, "a photo of a book")
        assert e.availability == "unfetched" and e.record_id == tagged[0].record_id
        assert fetcher.calls == []
    finally:
        await eng.stop()


async def test_visual_question_gets_image_evidence_with_provenance(tmp_path) -> None:
    fetcher, vision = Fetcher(), Vision()
    eng = await asset_engine(tmp_path, fetcher, vision)
    try:
        out = await eng.read(
            "What is the title of the book Melanie shared?", namespace="a", mode="retrieve", top_k=4
        )
        lines = [c for c in contents(out) if c.startswith("[image evidence")]
        assert len(lines) == 1
        assert "Charlotte's Web" in lines[0] and "asset:" in lines[0] and "turn D1:2" in lines[0]
        assert "children's classic" not in lines[0]  # the search hint is never evidence
        # the evidence line sits right after the turn it belongs to
        texts = contents(out)
        assert "bedtime read" in texts[texts.index(lines[0]) - 1]
        again = await eng.read(
            "What is the title of the book Melanie shared?", namespace="a", mode="retrieve", top_k=4
        )
        assert fetcher.calls == [URL] and vision.calls == 1  # once
        assert eng.asset_stats()["fetches"] == 1
        assert len(again.context.records) == len(out.context.records)
    finally:
        await eng.stop()


async def test_text_question_makes_no_image_call(tmp_path) -> None:
    fetcher, vision = Fetcher(), Vision()
    eng = await asset_engine(tmp_path, fetcher, vision)
    try:
        out = await eng.read("When was the pottery class?", namespace="a", mode="retrieve", top_k=4)
        assert not any(c.startswith("[image evidence") for c in contents(out))
        assert fetcher.calls == [] and vision.calls == 0
    finally:
        await eng.stop()


async def test_cached_mode_never_downloads_and_says_so(tmp_path) -> None:
    fetcher, vision = Fetcher(), Vision()
    eng = await asset_engine(tmp_path, fetcher, vision, mode="cached")
    try:
        out = await eng.read(
            "What is the title of the book Melanie shared?", namespace="a", mode="retrieve", top_k=4
        )
        notes = [c for c in contents(out) if c.startswith("[image evidence unavailable")]
        assert len(notes) == 1 and "do not guess" in notes[0]
        assert fetcher.calls == [] and vision.calls == 0
    finally:
        await eng.stop()


async def test_failed_download_stays_explicit_in_the_context(tmp_path) -> None:
    fetcher = Fetcher(fail="http_404")
    eng = await asset_engine(tmp_path, fetcher, Vision())
    try:
        out = await eng.read(
            "What is the title of the book Melanie shared?", namespace="a", mode="retrieve", top_k=4
        )
        notes = [c for c in contents(out) if "image evidence unavailable" in c]
        assert len(notes) == 1 and "http_404" in notes[0] and "do not guess" in notes[0]
        assert not any(c.startswith("[image evidence asset") for c in contents(out))
    finally:
        await eng.stop()


# ── E03 ──────────────────────────────────────────────────────────────────────


async def external_engine(tmp_path, mode: str = "web", provider: Provider | None = None) -> Engine:
    eng = await make(tmp_path, external_evidence=mode, external_max_calls=2)
    if provider is not None:
        eng.set_external_provider(provider)
    return eng


QUESTION = "Would Caroline likely enjoy classical music?"


async def test_inference_question_gets_a_cited_public_block_and_a_generic_query(tmp_path) -> None:
    provider = Provider()
    eng = await external_engine(tmp_path, provider=provider)
    try:
        before = len(await eng._records("a"))
        out = await eng.read(QUESTION, namespace="a", mode="retrieve", top_k=4)
        blocks = [c for c in contents(out) if c.startswith(PUBLIC_MARKER)]
        assert len(blocks) == 1
        assert "never shows that any person in the memories did" in blocks[0]
        assert provider.calls == ["classical music"]  # no name, no private word
        assert len(await eng._records("a")) == before  # never persisted as a fact
        assert eng.external_stats()["network_calls"] == 1
    finally:
        await eng.stop()


async def test_no_private_conversation_text_is_ever_sent(tmp_path) -> None:
    provider = Provider()
    eng = await external_engine(tmp_path, provider=provider)
    try:
        await eng.read(
            "Would Melanie like a pottery class near the kids and Caroline?",
            namespace="a",
            mode="retrieve",
            top_k=4,
        )
        assert provider.calls and "pottery" in provider.calls[0]
        for sent in provider.calls:
            for private in ("melanie", "caroline", "kids", "reading", "bedtime"):
                assert private not in sent
    finally:
        await eng.stop()


@pytest.mark.parametrize(
    "question",
    [
        "Does Caroline own a violin?",
        "Did Caroline visit Paris?",
        "When did Caroline listen to classical music?",
    ],
)
async def test_external_evidence_never_answers_a_private_fact_question(tmp_path, question) -> None:
    provider = Provider()  # its snippet claims "Caroline owns a violin and visited Paris"
    eng = await external_engine(tmp_path, provider=provider)
    try:
        out = await eng.read(question, namespace="a", mode="retrieve", top_k=4)
        assert provider.calls == []
        assert not any("violin" in c or c.startswith(PUBLIC_MARKER) for c in contents(out))
    finally:
        await eng.stop()


async def test_cache_mode_and_repeat_reads_use_no_network(tmp_path) -> None:
    provider = Provider()
    eng = await external_engine(tmp_path, provider=provider)
    try:
        await eng.read(QUESTION, namespace="a", mode="retrieve", top_k=4)
        await eng.read(QUESTION, namespace="a", mode="retrieve", top_k=4)
        assert len(provider.calls) == 1 and eng.external_stats()["cache_hits"] == 1
    finally:
        await eng.stop()
    cold = Provider()
    eng2 = await external_engine(tmp_path, mode="cache", provider=cold)
    try:
        out = await eng2.read(QUESTION, namespace="a", mode="retrieve", top_k=4)
        assert cold.calls == [] and not any(c.startswith(PUBLIC_MARKER) for c in contents(out))
    finally:
        await eng2.stop()


async def test_unconfigured_provider_fails_explicit_and_changes_nothing(tmp_path) -> None:
    eng = await external_engine(tmp_path)  # web mode, no provider
    off = await make(tmp_path)
    try:
        a = await eng.read(QUESTION, namespace="a", mode="retrieve", top_k=4)
        b = await off.read(QUESTION, namespace="a", mode="retrieve", top_k=4)
        assert contents(a) == contents(b)
        assert eng.external_stats()["network_calls"] == 0
    finally:
        await eng.stop()
        await off.stop()


async def test_off_is_byte_identical_to_before(tmp_path) -> None:
    a = await make(tmp_path)
    try:
        out = await a.read(QUESTION, namespace="a", mode="retrieve", top_k=4)
        assert not any(c.startswith(PUBLIC_MARKER) or c.startswith("[image") for c in contents(out))
        assert a.external_stats() == {} and a.asset_stats() == {}
    finally:
        await a.stop()
