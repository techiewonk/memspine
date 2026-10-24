"""FU-ASSET: ``write.asset_describer: on_read`` fires from every read verb, once per asset.

The hook sits in ``scope_guard.viewer_guarded`` (via ``Engine._after_read``), so it sees the output
after the viewer filter. Model-free: hash embedder, stub describer. Names are invented.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from memspine.api.engine import Engine
from memspine.ingest import asset_describer as ad
from memspine.read.pipeline import scope_guard

NS = "acme/ilse"
Q = "asset hive.png"
PNG = b"\x89PNG\r\n\x1a\n" + b"hive-picture-bytes" * 50


class Stub:
    key = "stub-vision:abc123"

    def __init__(self) -> None:
        self.calls = 0

    async def describe(self, info: ad.AssetInfo) -> ad.Description:
        self.calls += 1
        return ad.Description(f"A green beehive, file {info.filename}.", "stub-vision", "abc123")


async def build(db: Path, **write: Any) -> Engine:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {"provider": "sqlite", "settings": {"path": str(db), "busy_timeout_s": 5.0}}
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={
            "episodic": {"enabled": True},
            "semantic": {"enabled": True},
            "resource": {"enabled": True},
            "shared": {"enabled": True},
        },
        write={"lineage_table": True, **write},
    )
    await eng.start()
    return eng


async def with_asset(tmp_path: Path, name: str, **write: Any) -> tuple[Engine, Stub, Any]:
    png = tmp_path / "hive.png"
    png.write_bytes(PNG)
    eng = await build(tmp_path / f"{name}.db", **write)
    stub = Stub()
    eng._asset_describer = stub
    asset = await eng.ingest_asset(png, NS)
    assert asset is not None
    return eng, stub, asset


async def descs(eng: Engine) -> list[Any]:
    return [
        r
        for r in await eng._require_started().list_records(NS, "resource")
        if ad.DESC_TAG in r.tags
    ]


VERBS: dict[str, Any] = {
    "search": lambda e, a: e.search(Q, namespace=NS),
    "retrieve": lambda e, a: e.retrieve(NS),
    "get_record": lambda e, a: e.get_record(a.record_id, NS),
    "list_records": lambda e, a: e.list_records(NS),
    "shared_search": lambda e, a: e.shared_search(Q, namespace=NS),
    "assemble": lambda e, a: e.assemble(Q, namespace=NS),
    "read": lambda e, a: e.read(Q, namespace=NS),
}


@pytest.mark.parametrize("verb", sorted(VERBS))
async def test_each_read_verb_describes_once(tmp_path: Path, verb: str) -> None:
    eng, stub, asset = await with_asset(tmp_path, verb, asset_describer="on_read")
    try:
        assert stub.calls == 0 and await descs(eng) == []
        await VERBS[verb](eng, asset)
        assert stub.calls == 1, verb
        assert len(await descs(eng)) == 1
        await VERBS[verb](eng, asset)  # the cache: a second read never re-describes
        await eng.search(Q, namespace=NS)
        assert stub.calls == 1 and len(await descs(eng)) == 1
    finally:
        await eng.stop()


async def test_hook_runs_once_per_outermost_verb_for_every_guarded_verb(tmp_path: Path) -> None:
    eng, _stub, asset = await with_asset(tmp_path, "spy", asset_describer="on_read")
    seen: list[str] = []
    real = eng._after_read

    async def spy(verb: str, namespace: str, result: Any) -> Any:
        seen.append(verb)
        return await real(verb, namespace, result)

    eng._after_read = spy
    try:
        await eng.brief(NS)
        await eng.conversation(NS, "s1")
        await eng.timeline(NS)
        await eng.session_memories(NS, [asset.record_id])
        await eng.sessions(NS)
        await eng.namespace_stats(NS)
        await eng.assemble(Q, namespace=NS)  # nested search/shared_search must not fire again
        await eng.read(Q, namespace=NS)
        assert seen == [
            "brief",
            "conversation",
            "timeline",
            "session_memories",
            "sessions",
            "namespace_stats",
            "assemble",
            "read",
        ]
    finally:
        await eng.stop()


async def test_invisible_asset_is_not_described(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng, stub, asset = await with_asset(tmp_path, "inv", asset_describer="on_read")
    try:
        monkeypatch.setattr(
            scope_guard, "visible_to", lambda record, viewer: ad.ASSET_TAG not in record.tags
        )
        assert await eng.retrieve(NS, viewer="bram") == []  # the guard filters it out
        assert await eng.list_records(NS, viewer="bram") == []
        assert stub.calls == 0 and await descs(eng) == []
        got = await eng.retrieve(NS)  # no viewer: visible, so described
        assert [r.record_id for r in got] == [asset.record_id]
        assert stub.calls == 1
    finally:
        await eng.stop()


async def test_erased_asset_is_not_described(tmp_path: Path) -> None:
    eng, stub, asset = await with_asset(tmp_path, "erased", asset_describer="on_read")
    try:
        await eng.forget(asset.record_id, namespace=NS, hard=True)
        await eng.search(Q, namespace=NS)
        await eng.retrieve(NS)
        await eng.list_records(NS)
        assert await eng.get_record(asset.record_id, NS) is None
        assert stub.calls == 0 and await descs(eng) == []
        # a stale handle on an erased asset (read raced the erasure) is skipped before the model
        assert await ad.describe_and_deposit(eng, NS, asset, trigger="on_read") is None
        assert stub.calls == 0
    finally:
        await eng.stop()


async def test_other_namespace_asset_is_not_described(tmp_path: Path) -> None:
    eng, stub, asset = await with_asset(tmp_path, "scope", asset_describer="on_read")
    try:
        assert await ad.after_read(eng, "acme/noor", [(asset, 1.0)]) == [(asset, 1.0)]
        assert stub.calls == 0
    finally:
        await eng.stop()


async def test_flag_off_and_ingest_mode_never_describe_on_read(tmp_path: Path) -> None:
    eng, stub, asset = await with_asset(tmp_path, "ing", asset_describer="ingest")
    try:
        before = stub.calls  # the ingest caption
        for verb in VERBS:
            await VERBS[verb](eng, asset)
        assert stub.calls == before == 1
    finally:
        await eng.stop()
    eng2 = await build(tmp_path / "off.db")
    try:
        assert eng2._config().write.asset_describer == "off"
        await eng2.write("Ilse keeps bees behind the mill", namespace=NS)
        sentinel = [(r, s) for r, s in await eng2.search("bees", namespace=NS)]
        assert sentinel and await eng2._after_read("search", NS, sentinel) is sentinel
    finally:
        await eng2.stop()
