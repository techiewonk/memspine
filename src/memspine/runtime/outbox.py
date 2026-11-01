"""SQL-backed durable outbox (F-04): the ``JobStore`` of LIB-07.

The outbox is the truth (owner Q2, W-13): every unit of derived work is a row before any worker
touches it, so a stop or a crash loses nothing. ``JobBook`` (``runtime.jobs``) keeps all the rules
(leases with strictly increasing epochs, fenced commits, contiguous watermark plus holes, immutable
job snapshots, coalescing); this module only persists its state.

Tables (own ``MetaData``, so the v1 baseline is untouched; revision ``0006`` creates them):

``outbox``           one row per job (``Job``), including coalesced records.
``outbox_progress``  watermark, snapshot target and holes (JSON) per ``(scope, stage)``.
``outbox_item``      the work itself: item ``item_offset`` of ``(scope, stage)`` with its payload.
                     A job covers the offset range ``(watermark, target_offset]``.
``outbox_log``       the fence log: refused heartbeats, checkpoints, commits (evidence for ER-07).
``outbox_effect``    authorized provider-projection events and their durable outcomes.
``outbox_source_membership`` version/hash/erasure source state used by the effect door.

Synchronous Core on its own sync engine (the storage layer is async; ``JobBook`` is not). Each store
call is one short transaction. Under the inline runner all book calls run on the event-loop thread,
which makes a multi-call book operation atomic inside the process; cross-process safety is the
fenced commit, and a transport with real concurrency (RT-01) brings its own claim query.
SQLite and Postgres share the SQL: only the engine differs.

The effect door (``authorize_effects`` / ``finalize_effects`` against ``erase_source`` and
``mark_sources``) is a fenced read-check-write: each of those calls runs in ``_fenced_tx``,
which takes the database write lock BEFORE its first read (SQLite ``BEGIN IMMEDIATE``) or
row-locks what it reads (Postgres ``SELECT ... FOR UPDATE`` on the job row and each source row,
sources in record-id order and always before any effect row, so two doors never deadlock). An
erase therefore commits either wholly before a door's check (the door sees the new generation
and refuses) or wholly after its write (the erase then deletes the effect rows the door wrote).
The provider projection between authorize and finalize cannot hold a database lock (on SQLite
it writes the same file), so it is optimistic: finalize re-checks the fence and, when a source
changed or was erased meanwhile, answers ``EffectFence.STALE_SOURCE`` so the runner removes the
projected effects (rebuildable projections) instead of acknowledging them.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Table,
    delete,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import StaticPool

from memspine.observability import tap
from memspine.runtime.jobs import FenceEvent, Job, ScopeKey, scope_key
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.ingestion import AnnotationBatch, IngestionSourceSnapshot
from memspine.schemas.durable.jobs import Hole, Progress
from memspine.services.storage.outbox_tables import (
    OUTBOX_TABLES,
    outbox,
    outbox_draft,
    outbox_effect,
    outbox_item,
    outbox_log,
    outbox_metadata,
    outbox_progress,
    outbox_source_membership,
)

__all__ = [
    "OUTBOX_TABLES",
    "EffectFence",
    "SqlJobStore",
    "outbox_metadata",
]

# The table definitions live with the storage layer (records port, ADR-081).


# Sync engines are lent by the storage connector (RM-113): clients/sqlite/sync.py and
# clients/postgres/sync.py; the job store only receives one.


class EffectFence(StrEnum):
    """What the effect door answered (``authorize_effects`` / ``finalize_effects``).

    Truthy only when the door is open (``AUTHORIZED``, ``COMPLETE``, ``OK``), so
    ``if store.finalize_effects(...)`` reads as "acknowledged"."""

    AUTHORIZED = "authorized"  # the idempotent provider consumer must run
    COMPLETE = "complete"  # every effect was already acknowledged: skip the projection
    OK = "ok"  # finalize acknowledged the outcome
    BUSY = "busy"  # another consumer of this job and epoch is mid-projection
    STALE_LEASE = "stale_lease"  # this worker no longer owns the job
    STALE_SOURCE = "stale_source"  # a source changed or was erased: acknowledge nothing
    REPLAY_DIVERGED = "replay_diverged"  # a replay planned a different layer set or result

    def __bool__(self) -> bool:
        return self in (EffectFence.AUTHORIZED, EffectFence.COMPLETE, EffectFence.OK)


def _source_matches(row: Any, source: dict[str, Any], scope: OwnerScope) -> bool:
    """Whether a membership ``row`` is still exactly the snapshot ``source`` (the fence)."""
    return not (
        row is None
        or source.get("namespace") != scope.owner
        or not row.active
        or row.source_id != source.get("source_id")
        or int(row.source_version) != int(source.get("source_version", -1))
        or row.content_hash != source.get("content_hash")
        or int(row.erasure_generation) != int(source.get("erasure_generation", 0))
    )


# ---------------------------------------------------------------------------- codecs
def _ts(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def _dt(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _source_fence_key(source: dict[str, Any]) -> tuple[str, str, str, int, str, int]:
    """Fields persisted by the source fence; descriptive annotation metadata is separate."""
    snapshot = IngestionSourceSnapshot.model_validate(source)
    return (
        snapshot.namespace,
        snapshot.record_id,
        snapshot.source_id,
        snapshot.source_version,
        snapshot.content_hash,
        snapshot.erasure_generation,
    )


def _effect_artifact_json(effect: dict[str, Any]) -> str | None:
    """Validate and canonically encode an effect's optional materialized artifact."""
    artifact = effect.get("artifact")
    if artifact is None:
        return None
    if effect.get("layer") == "annotations":
        if not isinstance(artifact, dict):
            raise ValueError("annotation artifact must be an object")
        raw_batches = artifact.get("annotation_batches")
        if not isinstance(raw_batches, list) or not raw_batches:
            raise ValueError("annotation artifact must contain at least one annotation batch")
        identity_sources = effect.get("identity", {}).get("sources", [])
        authorized_sources = {
            _source_fence_key(source) for source in identity_sources if isinstance(source, dict)
        }
        batches = [AnnotationBatch.model_validate(batch) for batch in raw_batches]
        batch_sources = [
            _source_fence_key(batch.source.model_dump(mode="json")) for batch in batches
        ]
        if len(batch_sources) != len(set(batch_sources)):
            raise ValueError("annotation artifact contains duplicate source batches")
        if set(batch_sources) != authorized_sources:
            raise ValueError("annotation artifact sources differ from effect identity")
        artifact = {"annotation_batches": [batch.model_dump(mode="json") for batch in batches]}
    if not isinstance(artifact, dict):
        raise ValueError("effect artifact must be an object")
    return json.dumps(artifact, sort_keys=True, separators=(",", ":"), default=str)


def _job_to_row(job: Job) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "seq": job.seq,
        "tenant": job.scope.tenant,
        "owner": job.scope.owner,
        "owner_kind": job.scope.owner_kind,
        "stage": job.stage,
        "target_offset": job.target_offset,
        "state": job.state,
        "created_at": _ts(job.created_at),
        "payload": job.payload,
        "frozen": job.frozen,
        "epoch": job.epoch,
        "lease_owner": job.lease_owner,
        "lease_acquired_at": _ts(job.lease_acquired_at),
        "lease_heartbeat_at": _ts(job.lease_heartbeat_at),
        "lease_until": _ts(job.lease_until),
        "attempts": job.attempts,
        "not_before": _ts(job.not_before),
        "error": job.error,
        "failure_class": job.failure_class,
        "coalesced_into": job.coalesced_into,
    }


def _row_to_job(r: Any) -> Job:
    created = _dt(r.created_at)
    assert created is not None
    return Job(
        job_id=r.job_id,
        seq=r.seq,
        scope=OwnerScope(tenant=r.tenant, owner=r.owner, owner_kind=r.owner_kind),
        stage=r.stage,
        target_offset=r.target_offset,
        state=r.state,
        created_at=created,
        payload=r.payload,
        frozen=bool(r.frozen),
        epoch=r.epoch,
        lease_owner=r.lease_owner,
        lease_acquired_at=_dt(r.lease_acquired_at),
        lease_heartbeat_at=_dt(r.lease_heartbeat_at),
        lease_until=_dt(r.lease_until),
        attempts=r.attempts,
        not_before=_dt(r.not_before),
        error=r.error,
        failure_class=r.failure_class,
        coalesced_into=r.coalesced_into,
    )


def _scope_where(table: Table, scope: OwnerScope) -> list[Any]:
    return [
        table.c.tenant == scope.tenant,
        table.c.owner == scope.owner,
        table.c.owner_kind == scope.owner_kind,
    ]


class SqlJobStore:
    """``JobStore`` (``runtime.jobs``) over the outbox tables, plus the item side."""

    def __init__(self, engine: Engine, *, close_engine: bool = True) -> None:
        self.engine = engine
        self._close_engine = close_engine
        self._lock = threading.RLock()
        self._shared = isinstance(engine.pool, StaticPool)
        self.ensure_schema()

    # ------------------------------------------------------------------ plumbing
    def ensure_schema(self) -> None:
        """Create any missing outbox table (a no-op once revision 0006 ran)."""
        outbox_metadata.create_all(self.engine, checkfirst=True)

    def close(self) -> None:
        """Release pooled connections (a private in-memory database is kept: it IS the queue)."""
        if self._close_engine and not isinstance(self.engine.pool, StaticPool):
            self.engine.dispose()

    @contextmanager
    def _tx(self) -> Iterator[Connection]:
        # A file or server database serialises itself. Only the private in-memory database shares
        # ONE connection between threads, so only it needs the lock (taking it elsewhere would let
        # the event loop wait on a thread that waits on the database).
        if self._shared:
            with self._lock, self.engine.begin() as conn:
                yield conn
        else:
            with self.engine.begin() as conn:
                yield conn

    @contextmanager
    def _fenced_tx(self) -> Iterator[Connection]:
        """A transaction that holds the fence from its first read (see the module docstring).

        SQLite: the driver opens its transaction lazily at the first write, so a plain
        ``begin()`` reads outside any lock. Here the connection runs in driver autocommit and
        issues ``BEGIN IMMEDIATE`` itself, taking the single write lock before any read.
        Postgres: an ordinary transaction; the callers' ``with_for_update()`` reads lock the
        rows they check (the clause compiles to nothing on SQLite)."""
        if self.engine.dialect.name != "sqlite":
            with self._tx() as conn:
                yield conn
            return
        guard: AbstractContextManager[Any] = self._lock if self._shared else nullcontext()
        with guard, self.engine.connect() as raw:
            conn = raw.execution_options(isolation_level="AUTOCOMMIT")
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.exec_driver_sql("ROLLBACK")
                raise
            conn.exec_driver_sql("COMMIT")

    @staticmethod
    def _lease_live(c: Connection, job_id: str, epoch: int, at: datetime) -> bool:
        """Lock the job row and say whether ``epoch`` still holds a live lease on it."""
        job = c.execute(
            select(outbox.c.epoch, outbox.c.state, outbox.c.lease_until)
            .where(outbox.c.job_id == job_id)
            .with_for_update()
        ).first()
        lease_until = None if job is None else _dt(job.lease_until)
        return not (
            job is None
            or int(job.epoch) != epoch
            or job.state != "running"
            or lease_until is None
            or lease_until <= at
        )

    @staticmethod
    def _sources_current(
        c: Connection, scope: OwnerScope, stage: str, sources: Sequence[dict[str, Any]]
    ) -> bool:
        """Lock each source row (record-id order) and check it still matches its snapshot."""
        for source in sorted(sources, key=lambda s: str(s.get("record_id"))):
            row = c.execute(
                select(outbox_source_membership)
                .where(
                    *_scope_where(outbox_source_membership, scope),
                    outbox_source_membership.c.stage == stage,
                    outbox_source_membership.c.record_id == source.get("record_id"),
                )
                .with_for_update()
            ).first()
            if not _source_matches(row, source, scope):
                return False
        return True

    # ------------------------------------------------------------------ JobStore
    def next_seq(self) -> int:
        with self._tx() as c:
            top = c.execute(select(func.max(outbox.c.seq))).scalar()
        return int(top or 0) + 1

    def put_job(self, job: Job) -> None:
        row = _job_to_row(job)
        with self._tx() as c:
            res = c.execute(update(outbox).where(outbox.c.job_id == job.job_id).values(**row))
            if res.rowcount == 0:
                c.execute(insert(outbox).values(**row))

    def get_job(self, job_id: str) -> Job | None:
        with self._tx() as c:
            r = c.execute(select(outbox).where(outbox.c.job_id == job_id)).first()
        return None if r is None else _row_to_job(r)

    def jobs(self) -> list[Job]:
        with self._tx() as c:
            rows = c.execute(select(outbox).order_by(outbox.c.seq)).all()
        return [_row_to_job(r) for r in rows]

    def get_progress(self, key: tuple[ScopeKey, str]) -> Progress | None:
        (tenant, owner, kind), stage = key
        with self._tx() as c:
            r = c.execute(
                select(outbox_progress).where(
                    outbox_progress.c.tenant == tenant,
                    outbox_progress.c.owner == owner,
                    outbox_progress.c.owner_kind == kind,
                    outbox_progress.c.job_kind == stage,
                )
            ).first()
        if r is None:
            return None
        return Progress(
            scope=OwnerScope(tenant=tenant, owner=owner, owner_kind=kind),  # type: ignore[arg-type]
            job_kind=stage,
            watermark=r.watermark,
            target_offset=r.target_offset,
            holes=[Hole.model_validate(h) for h in json.loads(r.holes)],
        )

    def put_progress(self, progress: Progress) -> None:
        s = progress.scope
        values = {
            "watermark": progress.watermark,
            "target_offset": progress.target_offset,
            "holes": json.dumps([h.model_dump(mode="json") for h in progress.holes]),
        }
        key = [
            outbox_progress.c.tenant == s.tenant,
            outbox_progress.c.owner == s.owner,
            outbox_progress.c.owner_kind == s.owner_kind,
            outbox_progress.c.job_kind == progress.job_kind,
        ]
        with self._tx() as c:
            res = c.execute(update(outbox_progress).where(*key).values(**values))
            if res.rowcount == 0:
                c.execute(
                    insert(outbox_progress).values(
                        tenant=s.tenant,
                        owner=s.owner,
                        owner_kind=s.owner_kind,
                        job_kind=progress.job_kind,
                        **values,
                    )
                )

    def append_log(self, event: FenceEvent) -> None:
        with self._tx() as c:
            c.execute(
                insert(outbox_log).values(
                    at=_ts(event.at),
                    job_id=event.job_id,
                    op=event.op,
                    presented_epoch=event.presented_epoch,
                    current_epoch=event.current_epoch,
                    state=event.state,
                    reason=event.reason,
                )
            )
        tap.event(
            "runtime.outbox.fence_refused",
            job_id=event.job_id,
            op=event.op,
            presented_epoch=event.presented_epoch,
            current_epoch=event.current_epoch,
            state=event.state,
            reason=event.reason.replace(" ", "_"),
        )

    def log(self) -> list[FenceEvent]:
        with self._tx() as c:
            rows = c.execute(select(outbox_log).order_by(outbox_log.c.id)).all()
        out = []
        for r in rows:
            at = _dt(r.at)
            assert at is not None
            out.append(
                FenceEvent(
                    at=at,
                    job_id=r.job_id,
                    op=r.op,
                    presented_epoch=r.presented_epoch,
                    current_epoch=r.current_epoch,
                    state=r.state,
                    reason=r.reason,
                )
            )
        return out

    # ------------------------------------------------------------------ items (the work)
    def put_item(self, scope: OwnerScope, stage: str, payload: dict[str, Any], at: datetime) -> int:
        """Append one item to the ``(scope, stage)`` log and return its offset (0, 1, 2, ...)."""
        with self._tx() as c:
            top = c.execute(
                select(func.max(outbox_item.c.item_offset)).where(
                    *_scope_where(outbox_item, scope), outbox_item.c.stage == stage
                )
            ).scalar()
            offset = 0 if top is None else int(top) + 1
            c.execute(
                insert(outbox_item).values(
                    tenant=scope.tenant,
                    owner=scope.owner,
                    owner_kind=scope.owner_kind,
                    stage=stage,
                    item_offset=offset,
                    payload=json.dumps(payload, default=str),
                    created_at=_ts(at),
                )
            )
        return offset

    def get_item(self, scope: OwnerScope, stage: str, offset: int) -> dict[str, Any] | None:
        with self._tx() as c:
            r = c.execute(
                select(outbox_item.c.payload).where(
                    *_scope_where(outbox_item, scope),
                    outbox_item.c.stage == stage,
                    outbox_item.c.item_offset == offset,
                )
            ).first()
        return None if r is None else dict(json.loads(r.payload))

    def last_offset(self, scope: OwnerScope, stage: str) -> int:
        """Highest offset enqueued for ``(scope, stage)``; -1 when none."""
        with self._tx() as c:
            top = c.execute(
                select(func.max(outbox_item.c.item_offset)).where(
                    *_scope_where(outbox_item, scope), outbox_item.c.stage == stage
                )
            ).scalar()
        return -1 if top is None else int(top)

    def items_after(
        self, scope: OwnerScope, stage: str, after: int
    ) -> list[tuple[int, dict[str, Any]]]:
        with self._tx() as c:
            rows = c.execute(
                select(outbox_item.c.item_offset, outbox_item.c.payload)
                .where(
                    *_scope_where(outbox_item, scope),
                    outbox_item.c.stage == stage,
                    outbox_item.c.item_offset > after,
                )
                .order_by(outbox_item.c.item_offset)
            ).all()
        return [(int(r.item_offset), dict(json.loads(r.payload))) for r in rows]

    def item_scopes(self, stage: str) -> list[OwnerScope]:
        """Scopes with durable items for ``stage`` (used by crash reconciliation)."""
        with self._tx() as c:
            rows = c.execute(
                select(
                    outbox_item.c.tenant,
                    outbox_item.c.owner,
                    outbox_item.c.owner_kind,
                )
                .where(outbox_item.c.stage == stage)
                .distinct()
            ).all()
        return [
            OwnerScope(tenant=row.tenant, owner=row.owner, owner_kind=row.owner_kind)
            for row in rows
        ]

    def set_item(self, scope: OwnerScope, stage: str, offset: int, payload: dict[str, Any]) -> None:
        with self._tx() as c:
            c.execute(
                update(outbox_item)
                .where(
                    *_scope_where(outbox_item, scope),
                    outbox_item.c.stage == stage,
                    outbox_item.c.item_offset == offset,
                )
                .values(payload=json.dumps(payload, default=str))
            )

    # ------------------------------------------------------------------ durable open work
    def put_draft(
        self, scope: OwnerScope, stage: str, payload: dict[str, Any] | None, at: datetime
    ) -> None:
        """Replace one incomplete work draft, or delete it when ``payload`` is None."""
        key = [*_scope_where(outbox_draft, scope), outbox_draft.c.stage == stage]
        with self._tx() as c:
            if payload is None:
                c.execute(delete(outbox_draft).where(*key))
                return
            values = {"payload": json.dumps(payload, default=str), "updated_at": _ts(at)}
            result = c.execute(update(outbox_draft).where(*key).values(**values))
            if result.rowcount == 0:
                c.execute(
                    insert(outbox_draft).values(
                        tenant=scope.tenant,
                        owner=scope.owner,
                        owner_kind=scope.owner_kind,
                        stage=stage,
                        **values,
                    )
                )

    def get_draft(self, scope: OwnerScope, stage: str) -> dict[str, Any] | None:
        with self._tx() as c:
            row = c.execute(
                select(outbox_draft.c.payload).where(
                    *_scope_where(outbox_draft, scope), outbox_draft.c.stage == stage
                )
            ).first()
        return None if row is None else dict(json.loads(row.payload))

    @staticmethod
    def _delete_source_effects(
        c: Connection, scope: OwnerScope, stage: str, record_id: str
    ) -> None:
        """Remove whole immutable effects whose identity includes one changed source."""
        effects = c.execute(
            select(outbox_effect.c.effect_id, outbox_effect.c.identity).where(
                *_scope_where(outbox_effect, scope), outbox_effect.c.stage == stage
            )
        ).all()
        stale_ids = []
        for effect in effects:
            try:
                sources = json.loads(effect.identity).get("sources", [])
            except (AttributeError, TypeError, ValueError):
                # A malformed identity cannot prove that it excludes erased
                # content, so fail closed and remove it from the namespace.
                stale_ids.append(effect.effect_id)
                continue
            if any(
                isinstance(source, dict) and source.get("record_id") == record_id
                for source in sources
            ):
                stale_ids.append(effect.effect_id)
        if stale_ids:
            c.execute(delete(outbox_effect).where(outbox_effect.c.effect_id.in_(stale_ids)))

    def mark_sources(
        self, scope: OwnerScope, stage: str, sources: Sequence[dict[str, Any]], at: datetime
    ) -> list[str]:
        """Persist the source state against which derivation effects are fenced.

        This is the outbox-side source authority.  It is deliberately richer
        than queued item membership: a worker snapshots it before inference,
        then the transactional effect door compares that snapshot with the
        current row while it also checks the lease and appends effect events.

        An erased (inactive) source is never reactivated by a plain acceptance: that would
        undo the erasure fence for a worker still holding a pre-erasure snapshot. A caller
        may re-accept it only by presenting the row's current ``erasure_generation`` (proof it
        observed the erasure); a source presenting any other generation is refused. Returns
        the refused record ids."""
        refused: list[str] = []
        with self._fenced_tx() as c:
            for source in sorted(sources, key=lambda s: str(s["record_id"])):
                record_id = str(source["record_id"])
                key = [
                    *_scope_where(outbox_source_membership, scope),
                    outbox_source_membership.c.stage == stage,
                    outbox_source_membership.c.record_id == record_id,
                ]
                existing = c.execute(
                    select(outbox_source_membership).where(*key).with_for_update()
                ).first()
                values = {
                    "source_id": str(source.get("source_id") or record_id),
                    "source_version": int(source["source_version"]),
                    "content_hash": str(source["content_hash"]),
                    "active": True,
                    "accepted_at": _ts(at),
                }
                if existing is not None:
                    presented = source.get("erasure_generation")
                    current = int(existing.erasure_generation)
                    if (not existing.active and presented is None) or (
                        presented is not None and int(presented) != current
                    ):
                        refused.append(record_id)
                        continue
                    if (
                        int(existing.source_version) != values["source_version"]
                        or existing.content_hash != values["content_hash"]
                        or not existing.active
                    ):
                        self._delete_source_effects(c, scope, stage, record_id)
                    c.execute(update(outbox_source_membership).where(*key).values(**values))
                    continue
                c.execute(
                    insert(outbox_source_membership).values(
                        tenant=scope.tenant,
                        owner=scope.owner,
                        owner_kind=scope.owner_kind,
                        stage=stage,
                        record_id=record_id,
                        erasure_generation=0,
                        **values,
                    )
                )
        if refused:
            tap.event("runtime.outbox.source_refused", stage=stage, refused=len(refused))
        return refused

    def source_snapshots(
        self, scope: OwnerScope, stage: str, record_ids: Sequence[str]
    ) -> list[dict[str, Any]]:
        """Read ordered pre-inference snapshots; missing/inactive sources are refused."""
        snapshots: list[dict[str, Any]] = []
        with self._tx() as c:
            for record_id in dict.fromkeys(record_ids):
                row = c.execute(
                    select(outbox_source_membership).where(
                        *_scope_where(outbox_source_membership, scope),
                        outbox_source_membership.c.stage == stage,
                        outbox_source_membership.c.record_id == record_id,
                    )
                ).first()
                if row is None or not row.active:
                    return []
                snapshots.append(
                    {
                        "namespace": scope.owner,
                        "record_id": row.record_id,
                        "source_id": row.source_id,
                        "source_version": row.source_version,
                        "content_hash": row.content_hash,
                        "erasure_generation": row.erasure_generation,
                    }
                )
        return snapshots

    def erase_source(self, scope: OwnerScope, stage: str, record_id: str) -> bool:
        """Invalidate snapshots and remove immutable effects bound to ``record_id``.

        Fenced (``_fenced_tx``): the generation bump, the deactivation and the effect removal
        commit together, serialised against every effect door."""
        key = [
            *_scope_where(outbox_source_membership, scope),
            outbox_source_membership.c.stage == stage,
            outbox_source_membership.c.record_id == record_id,
        ]
        with self._fenced_tx() as c:
            row = c.execute(
                select(outbox_source_membership.c.erasure_generation).where(*key).with_for_update()
            ).first()
            if row is None:
                return False
            c.execute(
                update(outbox_source_membership)
                .where(*key)
                .values(
                    erasure_generation=outbox_source_membership.c.erasure_generation + 1,
                    active=False,
                )
            )
            self._delete_source_effects(c, scope, stage, record_id)
        return True

    def has_source(self, scope: OwnerScope, stage: str, record_id: str) -> bool:
        with self._tx() as c:
            return bool(
                c.execute(
                    select(outbox_source_membership.c.record_id).where(
                        *_scope_where(outbox_source_membership, scope),
                        outbox_source_membership.c.stage == stage,
                        outbox_source_membership.c.record_id == record_id,
                        outbox_source_membership.c.active.is_(True),
                    )
                ).first()
            )

    def authorize_effects(
        self,
        *,
        job_id: str,
        epoch: int,
        scope: OwnerScope,
        stage: str,
        offset: int,
        effects: Sequence[dict[str, Any]],
        sources: Sequence[dict[str, Any]],
        at: datetime,
    ) -> EffectFence:
        """Atomically fence inputs and append immutable effect authorization events.

        One fenced transaction (``_fenced_tx``) checks the lease and every source snapshot and
        writes the authorization rows, so an erase cannot commit between the check and the
        write. Answers: ``STALE_SOURCE`` / ``STALE_LEASE`` (refused, nothing written),
        ``BUSY`` (another consumer of this job and epoch holds the events), ``COMPLETE``
        (every effect was already acknowledged: replay skips provider writes),
        ``REPLAY_DIVERGED`` (some effects of this item were acknowledged and the replay planned
        others: a non-deterministic replay changed the layer set, so nothing is projected), or
        ``AUTHORIZED`` (the idempotent provider consumer must run, including recovery after a
        partial projection)."""
        with self._fenced_tx() as c:
            lease_ok = self._lease_live(c, job_id, epoch, at)
            if not self._sources_current(c, scope, stage, sources):
                return EffectFence.STALE_SOURCE
            if not lease_ok:
                return EffectFence.STALE_LEASE
            existing_effects: list[tuple[dict[str, Any], Any]] = []
            for effect in effects:
                existing = c.execute(
                    select(
                        outbox_effect.c.state,
                        outbox_effect.c.job_id,
                        outbox_effect.c.epoch,
                        outbox_effect.c.identity,
                    ).where(outbox_effect.c.effect_id == effect["effect_id"])
                ).first()
                if existing is not None:
                    incoming_identity = json.dumps(effect["identity"], sort_keys=True, default=str)
                    if existing.identity != incoming_identity:
                        raise ValueError("effect replay identity differs from persisted identity")
                    if (
                        existing.state == "authorized"
                        and existing.job_id == job_id
                        and int(existing.epoch) == epoch
                    ):
                        return EffectFence.BUSY
                existing_effects.append((effect, existing))
            done = [
                existing is not None and existing.state == "complete"
                for _, existing in existing_effects
            ]
            if all(done):
                return EffectFence.COMPLETE
            if any(done):
                return EffectFence.REPLAY_DIVERGED
            for effect, existing in existing_effects:
                if existing is not None:
                    c.execute(
                        update(outbox_effect)
                        .where(outbox_effect.c.effect_id == effect["effect_id"])
                        .values(
                            job_id=job_id,
                            epoch=epoch,
                            state="authorized",
                            failure=None,
                            committed_at=_ts(at),
                        )
                    )
                    continue
                c.execute(
                    insert(outbox_effect).values(
                        effect_id=effect["effect_id"],
                        job_id=job_id,
                        epoch=epoch,
                        tenant=scope.tenant,
                        owner=scope.owner,
                        owner_kind=scope.owner_kind,
                        stage=stage,
                        item_offset=offset,
                        layer=effect["layer"],
                        state="authorized",
                        result_count=0,
                        failure=None,
                        artifact=None,
                        identity=json.dumps(effect["identity"], sort_keys=True, default=str),
                        committed_at=_ts(at),
                    )
                )
        return EffectFence.AUTHORIZED

    def finalize_effects(
        self,
        *,
        job_id: str,
        epoch: int,
        effects: Sequence[dict[str, Any]],
        failure: dict[str, Any] | None,
        at: datetime,
    ) -> EffectFence:
        """Acknowledge projection outcomes, fenced by the same live lease and source state.

        Runs in one fenced transaction that first locks the job row and every source row, then
        writes the outcome rows. Answers ``OK`` (acknowledged), ``STALE_SOURCE`` (a source
        changed or was erased after authorization, or the erase already removed the effect
        row: the caller must compensate whatever it projected), ``STALE_LEASE`` (the job is no
        longer this worker's; its projection is valid and the new owner replays it), or
        ``REPLAY_DIVERGED`` (an already acknowledged effect is being acknowledged with a
        different result)."""
        artifacts = {str(effect["effect_id"]): _effect_artifact_json(effect) for effect in effects}
        with self._fenced_tx() as c:
            lease_ok = self._lease_live(c, job_id, epoch, at)
            rows: list[tuple[dict[str, Any], Any]] = []
            for effect in effects:
                row = c.execute(
                    select(outbox_effect).where(outbox_effect.c.effect_id == effect["effect_id"])
                ).first()
                if row is None:  # an erase or a source change removed it: fail closed
                    return EffectFence.STALE_SOURCE
                try:
                    identity_sources = json.loads(row.identity).get("sources", [])
                except (AttributeError, TypeError, ValueError):
                    return EffectFence.STALE_SOURCE
                scope = OwnerScope(tenant=row.tenant, owner=row.owner, owner_kind=row.owner_kind)
                checked = [
                    source
                    for source in identity_sources
                    if isinstance(source, dict) and "record_id" in source
                ]
                if not self._sources_current(c, scope, row.stage, checked):
                    return EffectFence.STALE_SOURCE
                rows.append((effect, row))
            if not lease_ok:
                return EffectFence.STALE_LEASE
            for effect, row in rows:
                if (
                    row.job_id != job_id
                    or int(row.epoch) != epoch
                    or row.identity != json.dumps(effect["identity"], sort_keys=True, default=str)
                ):
                    return EffectFence.STALE_LEASE
                artifact = artifacts[str(effect["effect_id"])]
                if row.state == "complete" and (
                    int(row.result_count) != int(effect["count"]) or row.artifact != artifact
                ):
                    return EffectFence.REPLAY_DIVERGED
            for effect, row in rows:
                if row.state == "complete":
                    continue
                c.execute(
                    update(outbox_effect)
                    .where(outbox_effect.c.effect_id == effect["effect_id"])
                    .values(
                        job_id=job_id,
                        epoch=epoch,
                        state="failed" if failure is not None else "complete",
                        result_count=int(effect["count"]),
                        failure=None if failure is None else json.dumps(failure, sort_keys=True),
                        artifact=artifacts[str(effect["effect_id"])],
                        committed_at=_ts(at),
                    )
                )
        return EffectFence.OK

    def effect_status(self, scope: OwnerScope) -> list[dict[str, Any]]:
        """Return persisted layer outcomes for one namespace scope."""
        with self._tx() as c:
            rows = c.execute(
                select(outbox_effect)
                .where(*_scope_where(outbox_effect, scope))
                .order_by(outbox_effect.c.committed_at, outbox_effect.c.effect_id)
            ).all()
        return [
            {
                "effect_id": row.effect_id,
                "job_id": row.job_id,
                "epoch": row.epoch,
                "stage": row.stage,
                "offset": row.item_offset,
                "layer": row.layer,
                "state": row.state,
                "count": row.result_count,
                "failure": None if row.failure is None else json.loads(row.failure),
                "artifact": None if row.artifact is None else json.loads(row.artifact),
                "identity": json.loads(row.identity),
                "committed_at": row.committed_at,
            }
            for row in rows
        ]

    def item_created_at(self, scope: OwnerScope, stage: str, offset: int) -> datetime | None:
        """When item ``offset`` was queued (RT-02: the start of its time-to-searchable)."""
        with self._tx() as c:
            r = c.execute(
                select(outbox_item.c.created_at).where(
                    *_scope_where(outbox_item, scope),
                    outbox_item.c.stage == stage,
                    outbox_item.c.item_offset == offset,
                )
            ).first()
        return None if r is None else _dt(r.created_at)

    def backlog(
        self, scope: OwnerScope | None = None, stage: str | None = None
    ) -> tuple[int, datetime | None]:
        """Unfinished items (beyond the watermark) and the oldest one's queue time (RT-02).

        These rows ARE the persisted "underived" markers: an item is underived from the moment it
        is queued until the watermark passes it, so a restart recomputes the same answer."""
        mark = func.coalesce(outbox_progress.c.watermark, -1)
        j = outbox_item.outerjoin(
            outbox_progress,
            (outbox_progress.c.tenant == outbox_item.c.tenant)
            & (outbox_progress.c.owner == outbox_item.c.owner)
            & (outbox_progress.c.owner_kind == outbox_item.c.owner_kind)
            & (outbox_progress.c.job_kind == outbox_item.c.stage),
        )
        q = (
            select(func.count(), func.min(outbox_item.c.created_at))
            .select_from(j)
            .where(outbox_item.c.item_offset > mark)
        )
        if scope is not None:
            q = q.where(*_scope_where(outbox_item, scope))
        if stage is not None:
            q = q.where(outbox_item.c.stage == stage)
        with self._tx() as c:
            n, oldest = c.execute(q).one()
        return int(n or 0), _dt(oldest)

    def live_scopes(self) -> list[tuple[OwnerScope, str]]:
        """``(scope, stage)`` pairs holding a queued or running job (what a restart resumes)."""
        seen: dict[tuple[ScopeKey, str], tuple[OwnerScope, str]] = {}
        for j in self.jobs():
            if j.state in ("queued", "running"):
                seen.setdefault((scope_key(j.scope), j.stage), (j.scope, j.stage))
        return list(seen.values())

    def prune_items(self, scope: OwnerScope, stage: str, through: int) -> int:
        """Drop handled items at or below ``through`` (the caller passes the watermark minus any
        open hole offsets it still needs). Returns the number removed."""
        with self._tx() as c:
            res = c.execute(
                delete(outbox_item).where(
                    *_scope_where(outbox_item, scope),
                    outbox_item.c.stage == stage,
                    outbox_item.c.item_offset <= through,
                )
            )
        return int(res.rowcount or 0)
