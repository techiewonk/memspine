"""Filesystem connector: one root directory on this host (ADR-081 ``storage.documents``).

The connector owns every file operation (RM-113 standard: adapters never open files): staging
with a size cap and a running SHA-256, atomic promotion (write to ``.tmp``, flush, fsync,
``os.replace``), reads, streamed reads, removals and listings, all confined to the root. Paths
are relative POSIX strings; an absolute path, a drive or ``..`` is refused. ``exclusive()``
serialises read-modify-write sequences of every Engine in this process that shares the
connector (a threading lock, so Engines on different event loops share it). Across processes
the store is single-writer (ADR-081 WP6, ``deployment.writer``).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import threading
import uuid
from collections.abc import AsyncIterable, AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, ClassVar

from memspine.clients._common.deadlines import probe
from memspine.clients.base import ConnectorIdentity, Locality, Scope
from memspine.exceptions import StorageError

__all__ = ["FileLimitError", "FilesystemConnector", "FilesystemSpec", "StagedFile"]

_TMP = ".tmp"


class FileLimitError(StorageError):
    """A staged file exceeded the caller's byte limit (nothing was kept)."""


@dataclass(frozen=True, slots=True)
class FilesystemSpec:
    """Transport spec: the root directory and whether writes are fsynced before promotion."""

    root: str
    fsync: bool = True


@dataclass(frozen=True, slots=True)
class StagedFile:
    """Bytes written under ``.tmp`` and not yet visible: promote or discard it."""

    temp: str
    size: int
    sha256: str


def _canonical_root(root: str) -> str:
    return os.path.normcase(str(Path(root).expanduser().resolve(strict=False)))


class FilesystemConnector:
    """Connector protocol (``clients/base.py``) over one root directory."""

    kind: ClassVar[str] = "documents.filesystem"
    scope: ClassVar[Scope] = "thread"

    def __init__(self, spec: FilesystemSpec) -> None:
        if not spec.root:
            raise ValueError("filesystem root must be a non-empty path")
        self._spec = spec
        self._root = Path(spec.root).expanduser().resolve(strict=False)
        self._open = False
        self._lock = threading.Lock()

    # -- connector protocol ----------------------------------------------------------------
    @classmethod
    def identity(cls, spec: FilesystemSpec) -> ConnectorIdentity:
        root = _canonical_root(spec.root)
        return ConnectorIdentity(
            cls.kind,
            {"root": root},
            knobs={"fsync": spec.fsync},
            canonical={"kind": "filesystem", "root": root},
        )

    @classmethod
    def locality(cls, spec: FilesystemSpec) -> Locality:
        return "host"

    @property
    def spec(self) -> FilesystemSpec:
        return self._spec

    @property
    def handle(self) -> Path:
        if not self._open:
            raise StorageError("filesystem connector is not open — call open() first")
        return self._root

    async def open(self) -> None:
        """Create the root (and its staging directory) and prove it is writable."""
        if self._open:
            return
        try:
            await asyncio.to_thread(self._open_sync)
        except OSError as exc:
            raise StorageError(
                f"documents root is not a writable directory ({type(exc).__name__})"
            ) from None
        self._open = True

    def _open_sync(self) -> None:
        (self._root / _TMP).mkdir(parents=True, exist_ok=True)
        self._write_probe()

    def _write_probe(self) -> None:
        probe_path = self._root / _TMP / f"probe-{uuid.uuid4().hex}"
        probe_path.write_bytes(b"ok")
        probe_path.unlink()

    async def health(self, deadline_s: float) -> bool:
        """A real round trip: write and remove a probe file under the root."""

        async def check() -> bool:
            if not self._open:
                return False
            await asyncio.to_thread(self._write_probe)
            return True

        return await probe(deadline_s, check)

    async def close(self) -> None:
        self._open = False

    # -- path safety -----------------------------------------------------------------------
    def _path(self, rel: str) -> Path:
        pure = PurePosixPath(rel)
        if (
            not rel
            or pure.is_absolute()
            or ".." in pure.parts
            or any(":" in part or "\\" in part for part in pure.parts)
        ):
            raise StorageError("refused a path outside the documents root")
        return self._root.joinpath(*pure.parts)

    def _require_open(self) -> None:
        if not self._open:
            raise StorageError("filesystem connector is not open — call open() first")

    # -- exclusive section -----------------------------------------------------------------
    @asynccontextmanager
    async def exclusive(self) -> AsyncIterator[None]:
        """Serialise a read-modify-write sequence across every holder in this process."""
        await asyncio.to_thread(self._lock.acquire)
        try:
            yield
        finally:
            self._lock.release()

    # -- writes ----------------------------------------------------------------------------
    async def stage(
        self, chunks: AsyncIterable[bytes] | Iterable[bytes], *, max_bytes: int
    ) -> StagedFile:
        """Write ``chunks`` to a private staging file, hashing as it goes. More than
        ``max_bytes`` raises :class:`FileLimitError` and removes the partial file."""
        self._require_open()
        temp_rel = f"{_TMP}/{uuid.uuid4().hex}"
        path = self._path(temp_rel)
        digest = hashlib.sha256()
        size = 0
        handle = await asyncio.to_thread(path.open, "wb")
        try:
            if isinstance(chunks, AsyncIterable):
                async for chunk in chunks:
                    size = await self._stage_chunk(handle, digest, chunk, size, max_bytes)
            else:
                for chunk in chunks:
                    size = await self._stage_chunk(handle, digest, chunk, size, max_bytes)
            await asyncio.to_thread(self._flush, handle)
        except BaseException:
            await asyncio.to_thread(handle.close)
            await asyncio.to_thread(path.unlink, True)
            raise
        await asyncio.to_thread(handle.close)
        return StagedFile(temp_rel, size, digest.hexdigest())

    async def _stage_chunk(
        self, handle: BinaryIO, digest: Any, chunk: bytes, size: int, max_bytes: int
    ) -> int:
        if not isinstance(chunk, bytes | bytearray | memoryview):
            raise TypeError("document chunks must be bytes")
        size += len(chunk)
        if size > max_bytes:
            raise FileLimitError(f"document exceeds the {max_bytes}-byte limit")
        digest.update(chunk)
        await asyncio.to_thread(handle.write, chunk)
        return size

    def _flush(self, handle: BinaryIO) -> None:
        handle.flush()
        if self._spec.fsync:
            os.fsync(handle.fileno())

    async def promote(self, staged: StagedFile, rel: str) -> None:
        """Atomically move a staged file to ``rel`` (replacing what is there)."""
        self._require_open()
        source, target = self._path(staged.temp), self._path(rel)

        def move() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)

        await asyncio.to_thread(move)

    async def discard(self, staged: StagedFile) -> None:
        await asyncio.to_thread(self._path(staged.temp).unlink, True)

    async def write_atomic(self, rel: str, data: bytes) -> None:
        """Write ``data`` to ``rel`` through a staging file and an atomic replace."""
        staged = await self.stage((data,), max_bytes=max(1, len(data)))
        try:
            await self.promote(staged, rel)
        except BaseException:
            await self.discard(staged)
            raise

    # -- reads -----------------------------------------------------------------------------
    async def read_bytes(self, rel: str) -> bytes | None:
        self._require_open()
        path = self._path(rel)

        def read() -> bytes | None:
            try:
                return path.read_bytes()
            except FileNotFoundError:
                return None

        return await asyncio.to_thread(read)

    async def iter_bytes(self, rel: str, chunk_size: int) -> AsyncIterator[bytes]:
        """Stream ``rel`` in chunks; a missing file raises :class:`StorageError`."""
        self._require_open()
        path = self._path(rel)
        try:
            handle = await asyncio.to_thread(path.open, "rb")
        except FileNotFoundError:
            raise StorageError("no such file under the documents root") from None
        try:
            while chunk := await asyncio.to_thread(handle.read, chunk_size):
                yield chunk
        finally:
            await asyncio.to_thread(handle.close)

    async def exists(self, rel: str) -> bool:
        self._require_open()
        return await asyncio.to_thread(self._path(rel).is_file)

    async def list_files(self, rel_dir: str, *, suffix: str = "") -> list[str]:
        """Relative paths of the files under ``rel_dir`` (recursive) ending in ``suffix``."""
        self._require_open()
        base = self._path(rel_dir)

        def walk() -> list[str]:
            if not base.is_dir():
                return []
            return sorted(
                path.relative_to(self._root).as_posix()
                for path in base.rglob(f"*{suffix}")
                if path.is_file()
            )

        return await asyncio.to_thread(walk)

    # -- removals --------------------------------------------------------------------------
    async def remove(self, rel: str) -> bool:
        self._require_open()
        path = self._path(rel)

        def unlink() -> bool:
            try:
                path.unlink()
            except FileNotFoundError:
                return False
            return True

        return await asyncio.to_thread(unlink)

    async def remove_tree(self, rel_dir: str) -> None:
        self._require_open()
        path = self._path(rel_dir)
        await asyncio.to_thread(shutil.rmtree, path, True)
