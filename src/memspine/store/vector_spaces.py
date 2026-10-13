"""F-03 (P-25 C, M-26 B): versioned vector spaces, shadow re-embed, selective re-derivation.

A *vector space* is the thing two vectors must share to be comparable: the embedder model, its
dimension, dtype, query instruction and normalisation. ``VectorSpace.id`` hashes exactly those, so a
model swap, a changed instruction or a changed dtype is a new space, and vectors of different spaces
are never compared. Every stored vector carries its space id through the identity of the table that
holds it (the backend factory is given the space, and names its table from ``qualified_id``).

Behind ``embedding.versioned_spaces`` (off by default: nothing here is wired, storage and search are
what they were). With it on:

* the first write of a namespace makes the current space its *active* space;
* after an embedder change, new writes go to the new (current) space while reads still query only
  the namespace's active space (a query vector from another space is refused, not compared);
* ``reembed`` builds the new space from the namespace's records in batches (resumable, with a batch
  budget) and switches the active space atomically when the last batch lands;
* the old space is kept until ``drop_space``.

Records whose derivation (model id / prompt hash) differs from the current extractor are listed by
``stale_derivations``; ``rederive_plan`` turns that into a batch plan with a hook the offline runner
calls later. No model call is made for re-derivation here.

Pure Python over a small backend protocol; no I/O except the optional JSON state file.
"""

from __future__ import annotations

import inspect
import json
import os
import tempfile
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from memspine.observability import tap
from memspine.store.events import fingerprint_payload
from memspine.store.records import MemoryRecord

__all__ = [
    "InMemorySpaceState",
    "JsonSpaceState",
    "RederivePlan",
    "ReembedReport",
    "SpaceBackend",
    "SpaceStateStore",
    "StaleReport",
    "VectorSpace",
    "VersionedSpaceStore",
    "derivation_digest",
    "rederive_plan",
    "stale_derivations",
]


# ------------------------------------------------------------------ identity


@dataclass(frozen=True)
class VectorSpace:
    """What makes two vectors comparable. ``version`` orders spaces of one model (re-builds)."""

    model_id: str
    dim: int
    dtype: str = "float32"
    query_instruction: str | None = None
    normalisation: str = "none"
    version: int = 1

    @property
    def id(self) -> str:
        """``hash(model id, dim, dtype, query instruction, normalisation)``; not the version."""
        digest = fingerprint_payload(
            {
                "model": self.model_id,
                "dim": self.dim,
                "dtype": self.dtype,
                "query_instruction": self.query_instruction or "",
                "normalisation": self.normalisation,
            }
        )
        return "vs_" + digest

    @property
    def qualified_id(self) -> str:
        """The id a backend names its table from: model id plus space id."""
        return f"{self.model_id}@{self.id}"

    @property
    def cache_version(self) -> str:
        """The ``vector_space_version`` of a LIB-19 embedding cache key."""
        return f"{self.id}.v{self.version}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "dim": self.dim,
            "dtype": self.dtype,
            "query_instruction": self.query_instruction,
            "normalisation": self.normalisation,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> VectorSpace:
        return cls(
            model_id=str(data["model_id"]),
            dim=int(data["dim"]),
            dtype=str(data.get("dtype", "float32")),
            query_instruction=data.get("query_instruction"),
            normalisation=str(data.get("normalisation", "none")),
            version=int(data.get("version", 1)),
        )


# ------------------------------------------------------------------ state


class SpaceStateStore(Protocol):
    def load(self) -> dict[str, Any]: ...
    def save(self, state: dict[str, Any]) -> None: ...


class InMemorySpaceState:
    def __init__(self) -> None:
        self._state: dict[str, Any] = {}

    def load(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._state))

    def save(self, state: dict[str, Any]) -> None:
        self._state = json.loads(json.dumps(state))


class JsonSpaceState:
    """State in one JSON file, replaced atomically (write temp, rename)."""

    def __init__(self, path: str) -> None:
        self._path = path

    def load(self) -> dict[str, Any]:
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, state: dict[str, Any]) -> None:
        directory = os.path.dirname(os.path.abspath(self._path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, sort_keys=True)
            os.replace(tmp, self._path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise


# ------------------------------------------------------------------ store


class SpaceBackend(Protocol):
    """The slice of ``VectorStore`` a space needs (one backend instance per space)."""

    async def upsert(
        self, record_id: str, namespace: str, embedder_id: str, vector: list[float]
    ) -> None: ...

    async def query(
        self, namespace: str, vector: list[float], embedder_id: str, top_k: int = 8
    ) -> list[Any]: ...

    async def delete(self, record_id: str) -> None: ...

    async def delete_all(self) -> None: ...

    async def exists(self, record_id: str) -> bool: ...


class _Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


#: ``source(offset, limit)`` lists ``limit`` (record_id, text) pairs of a namespace from ``offset``,
#: in a stable order. Sync or async.
ReembedSource = Callable[
    [int, int], "Iterable[tuple[str, str]] | Awaitable[Iterable[tuple[str, str]]]"
]


@dataclass(frozen=True)
class ReembedReport:
    namespace: str
    target: str  # space id being built
    batches: int  # batches run by THIS call
    records: int  # records embedded by this call
    offset: int  # cursor after this call
    done: bool
    switched: bool


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class VersionedSpaceStore:
    """A ``VectorStore`` over one backend per vector space, with per-namespace active-space routing.

    ``factory(space)`` returns the backend that stores that space's vectors (its table is named from
    ``space.qualified_id``). ``state`` persists the registered spaces, each namespace's active space
    and any in-flight re-embed cursor.
    """

    def __init__(
        self,
        factory: Callable[[VectorSpace], SpaceBackend],
        current: VectorSpace,
        state: SpaceStateStore | None = None,
    ) -> None:
        self._factory = factory
        self._state_store = state or InMemorySpaceState()
        self._backends: dict[str, SpaceBackend] = {}
        self._spaces: dict[str, VectorSpace] = {}
        self._active: dict[str, str] = {}
        self._cursors: dict[str, dict[str, Any]] = {}
        self._load()
        self._current = current
        self.register_space(current)

    # -- spaces ---------------------------------------------------------

    def _load(self) -> None:
        data = self._state_store.load()
        self._spaces = {
            sid: VectorSpace.from_dict(raw) for sid, raw in (data.get("spaces") or {}).items()
        }
        self._active = {str(k): str(v) for k, v in (data.get("active") or {}).items()}
        self._cursors = {str(k): dict(v) for k, v in (data.get("cursors") or {}).items()}

    def _save(self) -> None:
        self._state_store.save(
            {
                "spaces": {sid: sp.to_dict() for sid, sp in self._spaces.items()},
                "active": self._active,
                "cursors": self._cursors,
            }
        )

    @property
    def current(self) -> VectorSpace:
        return self._current

    def register_space(self, space: VectorSpace) -> VectorSpace:
        """Make ``space`` known (idempotent). It does not become anyone's active space."""
        if space.id not in self._spaces:
            self._spaces[space.id] = space
            self._save()
        return self._spaces[space.id]

    def set_current(self, space: VectorSpace) -> None:
        """The embedder changed: new writes go to ``space``; reads stay on each active space."""
        self._current = self.register_space(space)

    def spaces(self) -> list[VectorSpace]:
        return list(self._spaces.values())

    def active_space(self, namespace: str) -> VectorSpace | None:
        sid = self._active.get(namespace)
        return self._spaces.get(sid) if sid is not None else None

    def _backend(self, space: VectorSpace) -> SpaceBackend:
        backend = self._backends.get(space.id)
        if backend is None:
            backend = self._backends[space.id] = self._factory(space)
        return backend

    def _space_for_model(self, embedder_id: str) -> VectorSpace | None:
        """The space of this embedder id: the current one if it matches, else the latest known."""
        if self._current.model_id == embedder_id:
            return self._current
        found = [sp for sp in self._spaces.values() if sp.model_id == embedder_id]
        return max(found, key=lambda sp: sp.version) if found else None

    # -- VectorStore ----------------------------------------------------

    async def upsert(
        self, record_id: str, namespace: str, embedder_id: str, vector: list[float]
    ) -> None:
        space = self._space_for_model(embedder_id)
        if space is None:
            raise ValueError(f"no vector space registered for embedder {embedder_id!r}")
        if len(vector) != space.dim:
            raise ValueError(
                f"vector of dim {len(vector)} does not belong to space {space.id} (dim {space.dim})"
            )
        if namespace not in self._active:
            self._active[namespace] = space.id
            self._save()
        await self._backend(space).upsert(record_id, namespace, space.qualified_id, vector)
        tap.event(
            "store.vector.space",
            op="write",
            space=space.id,
            active=self._active[namespace],
            to_active=space.id == self._active[namespace],
        )

    async def query(
        self, namespace: str, vector: list[float], embedder_id: str, top_k: int = 8
    ) -> list[Any]:
        """Search the namespace's active space. A query from another space returns nothing: vectors
        of different spaces are never compared (embed the query with the active space's embedder, or
        use ``search_text``)."""
        active = self.active_space(namespace)
        if active is None:
            tap.event("store.vector.space", op="read", active="none", hit=False, mismatch=False)
            return []
        asker = self._space_for_model(embedder_id)
        if asker is None or asker.id != active.id or len(vector) != active.dim:
            tap.event(
                "store.vector.space",
                op="read",
                active=active.id,
                asker=asker.id if asker else "unknown",
                mismatch=True,
            )
            return []
        hits = await self._backend(active).query(namespace, vector, active.qualified_id, top_k)
        tap.event("store.vector.space", op="read", active=active.id, mismatch=False, hits=len(hits))
        return hits

    async def search_rescore(
        self, namespace: str, vector: list[float], embedder_id: str, top_k: int = 8
    ) -> list[Any]:
        return await self.query(namespace, vector, embedder_id, top_k)

    async def search_text(
        self,
        namespace: str,
        text: str,
        embedders: Mapping[str, _Embedder],
        top_k: int = 8,
    ) -> list[Any]:
        """Embed ``text`` with the active space's own embedder (``embedders``: model id to it)."""
        active = self.active_space(namespace)
        if active is None or active.model_id not in embedders:
            return []
        [vector] = await embedders[active.model_id].embed([text])
        return await self.query(namespace, vector, active.model_id, top_k)

    async def delete(self, record_id: str) -> None:
        """Erasure finds the row in every space, old ones included."""
        for space in self._spaces.values():
            await self._backend(space).delete(record_id)

    async def delete_all(self) -> None:
        for space in self._spaces.values():
            await self._backend(space).delete_all()

    async def exists(self, record_id: str) -> bool:
        for space in self._spaces.values():
            if await self._backend(space).exists(record_id):
                return True
        return False

    # -- shadow re-embed ------------------------------------------------

    async def reembed(
        self,
        namespace: str,
        source: ReembedSource,
        embedder: _Embedder,
        *,
        target: VectorSpace | None = None,
        batch: int = 64,
        budget: int | None = None,
    ) -> ReembedReport:
        """Build ``target`` (default: the current space) for ``namespace`` in batches, then switch.

        Resumable: the cursor is kept in the state, so a call with ``budget`` batches continues
        where the last stopped. Reads keep using the old active space until the last batch
        lands; then the active space flips in one state save. The old space is not deleted (see
        ``drop_space``).
        """
        space = self.register_space(target or self._current)
        if batch < 1:
            raise ValueError("batch must be >= 1")
        if self._active.get(namespace) == space.id and namespace not in self._cursors:
            return ReembedReport(namespace, space.id, 0, 0, 0, True, False)
        cursor = self._cursors.get(namespace)
        if cursor is None or cursor.get("target") != space.id:
            cursor = {"target": space.id, "offset": 0}
        offset = int(cursor["offset"])
        backend = self._backend(space)
        batches = records = 0
        done = False
        while budget is None or batches < budget:
            rows = list(await _maybe_await(source(offset, batch)))
            if rows:
                vectors = await embedder.embed([text for _, text in rows])
                if len(vectors) != len(rows) or any(len(v) != space.dim for v in vectors):
                    raise ValueError(
                        f"embedder output does not match space {space.id} (dim {space.dim})"
                    )
                for (record_id, _), vec in zip(rows, vectors, strict=True):
                    await backend.upsert(record_id, namespace, space.qualified_id, vec)
                offset += len(rows)
                records += len(rows)
                batches += 1
                tap.event(
                    "job.reembed", phase="batch", space=space.id, batch=batches, offset=offset
                )
            if len(rows) < batch:
                done = True
                break
        switched = False
        if done:
            self._active[namespace] = space.id
            self._cursors.pop(namespace, None)
            switched = True
        else:
            self._cursors[namespace] = {"target": space.id, "offset": offset}
        self._save()
        tap.event(
            "job.reembed",
            phase="end",
            space=space.id,
            batches=batches,
            records=records,
            done=done,
            switched=switched,
        )
        return ReembedReport(namespace, space.id, batches, records, offset, done, switched)

    def in_flight(self, namespace: str) -> dict[str, Any] | None:
        return dict(self._cursors[namespace]) if namespace in self._cursors else None

    async def drop_space(self, space_id: str) -> bool:
        """Remove a space and its vectors. Refused while any namespace still reads from it."""
        if space_id not in self._spaces or space_id in self._active.values():
            return False
        if self._current.id == space_id:
            return False
        await self._backend(self._spaces[space_id]).delete_all()
        self._backends.pop(space_id, None)
        del self._spaces[space_id]
        self._cursors = {ns: c for ns, c in self._cursors.items() if c.get("target") != space_id}
        self._save()
        return True


# ------------------------------------------------------------------ re-derivation


def derivation_digest(model_id: str | None, prompt_hash: str | None) -> str:
    """The derivation fingerprint of (model, prompt); ``legacy`` when neither is known."""
    if model_id is None and prompt_hash is None:
        return "legacy"
    return fingerprint_payload({"model": model_id or "", "prompt": prompt_hash or ""})


@dataclass(frozen=True)
class StaleReport:
    namespace: str | None
    total_derived: int
    stale_ids: tuple[str, ...]
    by_model: dict[str, int] = field(default_factory=dict)
    by_prompt: dict[str, int] = field(default_factory=dict)

    @property
    def stale(self) -> int:
        return len(self.stale_ids)


def stale_derivations(
    records: Iterable[MemoryRecord],
    current_model_id: str | None,
    current_prompt_hash: str | None,
    namespace: str | None = None,
) -> StaleReport:
    """Derived records (layer above L0) whose derivation differs from the current extractor.

    A record's derivation is its ``fingerprint.derivation`` or, when unset, the digest of its
    ``fingerprint.model_id`` / ``prompt_hash``. A record with neither is ``legacy`` and stale.
    """
    current = derivation_digest(current_model_id, current_prompt_hash)
    ids: list[str] = []
    models: Counter[str] = Counter()
    prompts: Counter[str] = Counter()
    total = 0
    for rec in records:
        if namespace is not None and rec.namespace != namespace:
            continue
        if rec.layer == "L0":
            continue
        total += 1
        fp = rec.fingerprint
        mine = fp.derivation or derivation_digest(fp.model_id, fp.prompt_hash)
        if mine == current:
            continue
        ids.append(rec.record_id)
        models[fp.model_id or "legacy"] += 1
        prompts[fp.prompt_hash or "legacy"] += 1
    report = StaleReport(namespace, total, tuple(ids), dict(models), dict(prompts))
    tap.event(
        "job.rederive.plan",
        total_derived=total,
        stale=report.stale,
        by_model=report.by_model,
        by_prompt=report.by_prompt,
    )
    return report


@dataclass(frozen=True)
class RederivePlan:
    """Batches of stale record ids. ``run`` hands each batch to the offline runner's hook."""

    namespace: str | None
    batches: tuple[tuple[str, ...], ...]

    @property
    def size(self) -> int:
        return sum(len(b) for b in self.batches)

    async def run(
        self, hook: Callable[[list[str]], Awaitable[Any] | Any], *, budget: int | None = None
    ) -> int:
        """Call ``hook(batch)`` for up to ``budget`` batches; returns the batches run."""
        ran = 0
        for batch in self.batches:
            if budget is not None and ran >= budget:
                break
            await _maybe_await(hook(list(batch)))
            ran += 1
        return ran


def rederive_plan(report: StaleReport, batch: int = 32) -> RederivePlan:
    if batch < 1:
        raise ValueError("batch must be >= 1")
    ids = report.stale_ids
    return RederivePlan(
        report.namespace, tuple(tuple(ids[i : i + batch]) for i in range(0, len(ids), batch))
    )
