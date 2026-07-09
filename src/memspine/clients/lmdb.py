"""LMDB connection client (D-24), ``[lmdb]``.

Owns the ``lmdb.Environment`` handle; the cache service receives this client
injected and never opens the environment itself. Import of the lmdb package is
deferred to ``connect()`` so constructing the client without the extra fails
with the actionable D-10 error, and a core install never imports it at all.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from memspine.clients.base import Client
from memspine.config import constants
from memspine.exceptions import MissingServiceError, StorageError

__all__ = ["LmdbClient"]


class LmdbClient(Client):
    def __init__(self, path: str | Path, *, map_size: int = constants.LMDB_MAP_SIZE_BYTES) -> None:
        self._path = str(path)
        self._map_size = map_size
        self._env: Any = None

    @property
    def env(self) -> Any:
        if self._env is None:
            raise StorageError("LmdbClient is not connected — call connect() first")
        return self._env

    async def connect(self) -> None:
        if self._env is not None:
            return
        try:
            import lmdb
        except ImportError as exc:
            raise MissingServiceError("cache:lmdb", extra="lmdb") from exc
        self._env = await asyncio.to_thread(self._open_env, lmdb)

    def _open_env(self, lmdb: Any) -> Any:
        # lmdb.open treats the path as a directory holding data.mdb + lock.mdb;
        # create it up front so a fresh deployment does not need a manual mkdir.
        Path(self._path).mkdir(parents=True, exist_ok=True)
        return lmdb.open(self._path, map_size=self._map_size)

    async def close(self) -> None:
        if self._env is not None:
            env, self._env = self._env, None
            await asyncio.to_thread(env.close)

    async def health(self) -> bool:
        return self._env is not None
