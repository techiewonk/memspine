"""RM-113 connector protocol for the filesystem connector (``storage.documents`` filesystem)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from memspine.clients.base import Connector
from memspine.clients.fs import FileLimitError, FilesystemConnector, FilesystemSpec
from memspine.exceptions import StorageError
from memspine.providers import shared
from memspine.providers.connectors import acquire
from memspine.providers.contexts import SharedBuildContext
from memspine.providers.lifecycle import ProviderLifecycleManager
from memspine.providers.shared import ResourceRegistry


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shared, "_REGISTRY", ResourceRegistry())


async def test_protocol_round_trip(tmp_path: Path) -> None:
    connector = FilesystemConnector(FilesystemSpec(str(tmp_path / "root")))
    assert isinstance(connector, Connector)
    assert FilesystemConnector.locality(connector.spec) == "host"
    assert not await connector.health(1.0)  # not open: False, never raises
    with pytest.raises(StorageError):
        _ = connector.handle
    await connector.open()
    await connector.open()  # idempotent
    assert connector.handle == (tmp_path / "root").resolve()
    assert await connector.health(5.0)
    await connector.close()
    await connector.close()
    assert not await connector.health(1.0)


def test_identity_is_the_canonical_root(tmp_path: Path) -> None:
    a = FilesystemConnector.identity(FilesystemSpec(str(tmp_path / "r")))
    b = FilesystemConnector.identity(FilesystemSpec(str(tmp_path / "." / "r")))
    c = FilesystemConnector.identity(FilesystemSpec(str(tmp_path / "other")))
    assert a == b and a != c
    assert a.descriptor_target()["canonical_target"]["kind"] == "filesystem"


async def test_open_fails_clearly_on_an_unwritable_root(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_bytes(b"not a directory")
    connector = FilesystemConnector(FilesystemSpec(str(blocker / "root")))
    with pytest.raises(StorageError, match="not a writable directory"):
        await connector.open()


async def test_stage_promote_and_size_limit(tmp_path: Path) -> None:
    connector = FilesystemConnector(FilesystemSpec(str(tmp_path), fsync=False))
    await connector.open()
    staged = await connector.stage((b"ab", b"cd"), max_bytes=4)
    assert (staged.size, len(staged.sha256)) == (4, 64)
    await connector.promote(staged, "a/b/file.bin")
    assert await connector.read_bytes("a/b/file.bin") == b"abcd"
    assert [c async for c in connector.iter_bytes("a/b/file.bin", 3)] == [b"abc", b"d"]
    with pytest.raises(FileLimitError):
        await connector.stage((b"abc", b"de"), max_bytes=4)
    assert os.listdir(tmp_path / ".tmp") == []  # the partial file is gone
    assert await connector.list_files("a", suffix=".bin") == ["a/b/file.bin"]
    assert await connector.remove("a/b/file.bin")
    assert not await connector.remove("a/b/file.bin")


@pytest.mark.parametrize("rel", ["/abs", "../escape", "a/../../b", "c:/x", "a\\b", ""])
async def test_paths_outside_the_root_are_refused(tmp_path: Path, rel: str) -> None:
    connector = FilesystemConnector(FilesystemSpec(str(tmp_path)))
    await connector.open()
    with pytest.raises(StorageError, match="outside the documents root"):
        await connector.read_bytes(rel)


async def test_acquire_shares_one_connector_per_root(tmp_path: Path) -> None:
    first = ProviderLifecycleManager(close_timeout_s=5.0)
    second = ProviderLifecycleManager(close_timeout_s=5.0)
    spec = FilesystemSpec(str(tmp_path / "docs"))
    a, _ = await acquire(
        SharedBuildContext(first, "o", 1.0), FilesystemConnector, spec, resource_id="d"
    )
    b, _ = await acquire(
        SharedBuildContext(second, "o", 1.0),
        FilesystemConnector,
        FilesystemSpec(str(tmp_path / "." / "docs")),
        resource_id="d",
    )
    assert a is b
    assert (await first.close_all()).complete
    assert await b.health(5.0)
    assert (await second.close_all()).complete
    assert not await b.health(1.0)
