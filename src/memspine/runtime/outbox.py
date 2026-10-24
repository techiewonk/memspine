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
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
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
    "SqlJobStore",
    "outbox_metadata",
]

# The table definitions live with the storage layer (records port, ADR-081).


# Sync engines are lent by the storage connector (RM-113): clients/sqlite/sync.py and
# clients/postgres/sync.py; the job store only receives one.


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
            _source_fence_key(source)
            for source in identity_sources
            if isinstance(source, dict)
        }
        batches = [AnnotationBatch.model_validate(batch) for batch in raw_batches]
        batch_sources = [_source_fence_key(batch.source.model_dump(mode="json")) for batch in batches]
        if len(batch_sources) != len(set(batch_sources)):
            raise ValueError("annotation artifact contains duplicate source batches")
        if set(batch_sources) != authorized_sources:
            raise ValueError("annotation artifact sources differ from effect identity")
        artifact = {
            "annotation_batches": [batch.model_dump(mode="json") for batch in batches]
        }
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
    ) -> None:
        """Persist the source state against which derivation effects are fenced.

        This is the outbox-side source authority.  It is deliberately richer
        than queued item membership: a worker snapshots it before inference,
        then the transactional effect door compares that snapshot with the
        current row while it also checks the lease and appends effect events.
        """
        with self._tx() as c:
            for source in sources:
                record_id = str(source["record_id"])
                key = [
                    *_scope_where(outbox_source_membership, scope),
                    outbox_source_membership.c.stage == stage,
                    outbox_source_membership.c.record_id == record_id,
                ]
                existing = c.execute(select(outbox_source_membership).where(*key)).first()
                values = {
                    "source_id": str(source.get("source_id") or record_id),
                    "source_version": int(source["source_version"]),
                    "content_hash": str(source["content_hash"]),
                    "active": True,
                    "accepted_at": _ts(at),
                }
                if existing is not None:
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
        """Invalidate snapshots and remove immutable effects bound to ``record_id``."""
        with self._tx() as c:
            row = c.execute(
                select(outbox_source_membership.c.erasure_generation).where(
                    *_scope_where(outbox_source_membership, scope),
                    outbox_source_membership.c.stage == stage,
                    outbox_source_membership.c.record_id == record_id,
                )
            ).first()
            if row is None:
                return False
            c.execute(
                update(outbox_source_membership)
                .where(
                    *_scope_where(outbox_source_membership, scope),
                    outbox_source_membership.c.stage == stage,
                    outbox_source_membership.c.record_id == record_id,
                )
                .values(erasure_generation=int(row.erasure_generation) + 1, active=False)
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
    ) -> str | None:
        """Atomically fence inputs and append immutable effect authorization events.

        ``None`` means the lease or a source snapshot is stale. ``complete``
        means every effect was already projected and acknowledged, so replay
        can skip provider writes. ``authorized`` means the idempotent provider
        consumer must run (including recovery after a partial projection).
        """
        with self._tx() as c:
            job = c.execute(
                select(outbox.c.epoch, outbox.c.state, outbox.c.lease_until).where(
                    outbox.c.job_id == job_id
                )
            ).first()
            lease_until = None if job is None else _dt(job.lease_until)
            if (
                job is None
                or int(job.epoch) != epoch
                or job.state != "running"
                or lease_until is None
                or lease_until <= at
            ):
                return None
            for source in sources:
                row = c.execute(
                    select(outbox_source_membership).where(
                        *_scope_where(outbox_source_membership, scope),
                        outbox_source_membership.c.stage == stage,
                        outbox_source_membership.c.record_id == source["record_id"],
                    )
                ).first()
                if (
                    row is None
                    or source.get("namespace") != scope.owner
                    or not row.active
                    or row.source_id != source["source_id"]
                    or int(row.source_version) != int(source["source_version"])
                    or row.content_hash != source["content_hash"]
                    or int(row.erasure_generation) != int(source["erasure_generation"])
                ):
                    return None
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
                        return "busy"
                existing_effects.append((effect, existing))
            if all(
                existing is not None and existing.state == "complete"
                for _, existing in existing_effects
            ):
                return "complete"
            for effect, existing in existing_effects:
                if existing is not None and existing.state == "complete":
                    continue
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
        return "authorized"

    def finalize_effects(
        self,
        *,
        job_id: str,
        epoch: int,
        effects: Sequence[dict[str, Any]],
        failure: dict[str, Any] | None,
        at: datetime,
    ) -> bool:
        """Acknowledge projection outcomes, fenced by the same live lease."""
        artifacts = {str(effect["effect_id"]): _effect_artifact_json(effect) for effect in effects}
        with self._tx() as c:
            job = c.execute(
                select(outbox.c.epoch, outbox.c.state, outbox.c.lease_until).where(
                    outbox.c.job_id == job_id
                )
            ).first()
            lease_until = None if job is None else _dt(job.lease_until)
            if (
                job is None
                or int(job.epoch) != epoch
                or job.state != "running"
                or lease_until is None
                or lease_until <= at
            ):
                return False
            for effect in effects:
                row = c.execute(
                    select(outbox_effect).where(
                        outbox_effect.c.effect_id == effect["effect_id"]
                    )
                ).first()
                if (
                    row is None
                    or row.job_id != job_id
                    or int(row.epoch) != epoch
                    or row.identity
                    != json.dumps(effect["identity"], sort_keys=True, default=str)
                ):
                    return False
                scope = OwnerScope(
                    tenant=row.tenant, owner=row.owner, owner_kind=row.owner_kind
                )
                try:
                    identity_sources = json.loads(row.identity).get("sources", [])
                except (AttributeError, TypeError, ValueError):
                    return False
                for source in identity_sources:
                    if not isinstance(source, dict) or "record_id" not in source:
                        continue
                    current = c.execute(
                        select(outbox_source_membership).where(
                            *_scope_where(outbox_source_membership, scope),
                            outbox_source_membership.c.stage == row.stage,
                            outbox_source_membership.c.record_id == source["record_id"],
                        )
                    ).first()
                    if (
                        current is None
                        or source.get("namespace") != scope.owner
                        or not current.active
                        or current.source_id != source["source_id"]
                        or int(current.source_version) != int(source["source_version"])
                        or current.content_hash != source["content_hash"]
                        or int(current.erasure_generation)
                        != int(source.get("erasure_generation", 0))
                    ):
                        return False
                artifact = artifacts[str(effect["effect_id"])]
                if row.state == "complete":
                    if int(row.result_count) != int(effect["count"]) or row.artifact != artifact:
                        raise ValueError("effect replay result differs from persisted result")
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
                        artifact=artifact,
                        committed_at=_ts(at),
                    )
                )
        return True

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
