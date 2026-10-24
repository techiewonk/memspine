"""Intake gateway (W2-01; W-01, W-02, W-03, W-24): the one door in front of the old write doors.

``ingest_messages``, ``write_episode`` and ``ingest`` become adapters (canonical intake): each call
is turned into a :class:`~memspine.schemas.steps.shared.SourceEnvelope`, run through the pure
``IntakeBook`` (LIB-30: validation, S3-3 write ids, idempotent retry, document version diff,
receipt), and the segments that are new go through the old write path unchanged.

Commit order (every arrow is a crash boundary, tested):

    validate -> [pre_commit] -> ledger commit (one SQL transaction) -> [post_commit] ->
    apply (old door, one record per segment, tagged ``idem:<write id>``) -> [pre_mark] ->
    ledger mark applied (real record ids, readiness, apply payload dropped) -> [post_mark]

* Before the ledger commit nothing is stored, so a retry writes everything.
* After it the apply payload is durable: ``start()`` calls :func:`recover`, which finds the records
  that already landed by their ``idem:`` tag and writes only the rest. A retry of the same call
  before a restart finishes the pending apply in-line, then returns the original receipt.
* After the mark a retry is a pure replay: the original receipt, no new record.

Receipt readiness is honest: ``raw_search`` becomes ``ready`` only once the records exist. ``facts``,
``graph`` and ``summaries`` are ``pending`` only when the window pipeline that produces them is
configured, and become ``ready`` only when the F-04 outbox barrier covers the intent's turns and none
of them still sits in an open window; without the outbox they stay ``pending`` (the engine cannot
prove them). ``profile`` is ``unavailable``: no per-write step produces it.

Names from the plan: ``IdempotencyKey`` and ``SourceVersioner`` are defined here;
``DerivationIntent`` is ``memspine.derive.intent``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from memspine.config import constants
from memspine.derive import role_filter
from memspine.derive.intent import DerivationIntent
from memspine.exceptions import ConfigError
from memspine.ingest import intake_store as store
from memspine.ingest import role_wiring
from memspine.ingest import write as ingest_write
from memspine.ingest.intake import (
    IntakeBook,
    IntakeCost,
    SourceState,
    ValidatedEnvelope,
    scope_key,
    validate_source,
    write_id,
)
from memspine.ingest.message_helpers import (
    IDEM_TAG_PREFIX,
    INTAKE_LANDED,
    _looks_like_recall,
    _turn_content,
)
from memspine.ingest.message_schema import ValidatedMessageBatch
from memspine.memories.procedural.skills import make_skill_record
from memspine.observability import tap
from memspine.schemas.public import QuotaExceeded, WaitResult
from memspine.schemas.public.errors import QuotaExceededError
from memspine.schemas.public.receipt import CAPABILITIES, Readiness, Receipt
from memspine.schemas.steps.shared import AssetRef, Scope, Segment, SourceEnvelope
from memspine.store.records import MemoryRecord, PiiTier, RecordStatus, SourceInfo
from memspine.store.scope import validate_namespace

__all__ = [
    "GatewayResult",
    "IdempotencyKey",
    "IntakeGateway",
    "SimulatedCrash",
    "SourceVersioner",
    "capabilities_of",
    "gateway_of",
    "ingest_data",
    "ingest_resolved_messages",
    "recover",
]


class SimulatedCrash(BaseException):
    """Raised by a test's ``IntakeGateway.fault`` hook at a named boundary."""


# --------------------------------------------------------------------------------------------- names
@dataclass(frozen=True)
class IdempotencyKey:
    """The identity of one segment write (W-03, S3-3).

    ``value`` is ``hash(owner scope, source id, version, position, content)``; position is the source
    time when the segment has one, else its index. The caller's own id (``caller_id``) is kept as a
    hint and never decides identity (S3-3: a client key must not let one caller's retry collide with
    another's write). The same words in a later source are a new write.
    """

    value: str
    caller_id: str | None = None

    @classmethod
    def of(
        cls, scope: Scope, source_id: str, version: int, seg: Segment, occurrence: int = 0
    ) -> IdempotencyKey:
        return cls(write_id(scope_key(scope), source_id, version, seg, occurrence), seg.caller_id)


@dataclass(frozen=True)
class SourceVersioner:
    """Chunk diff plus version events (W-24): a changed source re-points or retires its derived items.

    The diff itself is ``IntakeBook``'s (``VersionPlan``): unchanged chunks keep their record, changed
    ones are new records, vanished ones are retracted. :meth:`retire` is the second half: it archives
    the retracted chunk records and, through lineage, every derived record whose parents are all
    retired; a derived record that keeps a live parent is re-pointed (its edges to the retired parents
    are dropped from the lineage table). Idempotent: running it twice changes nothing more.
    """

    eng: Any

    async def retire(
        self,
        ns: str,
        retracted: Sequence[str],
        fresh: Sequence[str] = (),
        *,
        version: int = 1,
    ) -> dict[str, list[str]]:
        eng = self.eng
        storage = eng._require_started()
        records = await storage.list_records(ns)
        by_id = {r.record_id: r for r in records}
        live_status = (RecordStatus.ARCHIVED, RecordStatus.DELETED)
        retiring: set[str] = {
            r for r in retracted if r in by_id and by_id[r].status not in live_status
        }
        archived_before = {r for r in retracted if r in by_id and by_id[r].status in live_status}
        repointed: dict[str, set[str]] = {}
        frontier = set(retiring)
        while frontier:
            nxt: set[str] = set()
            for rec in records:
                if rec.record_id in retiring or rec.status in live_status:
                    continue
                parents = set(rec.source.parents)
                gone = parents & (retiring | archived_before)
                if not gone:
                    continue
                if parents - (retiring | archived_before):
                    repointed.setdefault(rec.record_id, set()).update(gone)
                else:
                    nxt.add(rec.record_id)
            retiring |= nxt
            frontier = nxt
        # a record whose last live parent went in a later round is retired, not re-pointed
        repointed = {k: v for k, v in repointed.items() if k not in retiring}
        if retiring:
            from memspine.derive.pipeline.stages.offline_common import archive_records

            async with eng._write_locks.setdefault(ns, asyncio.Lock()):
                await archive_records(
                    eng._append_and_project, ns, sorted(retiring), "intake_version", "source_edited"
                )
        edges_removed = 0
        if eng._config().write.lineage_table:
            from memspine.schemas.durable.lineage import LineageEdge

            drop = [
                LineageEdge(child=c, parent=p, role="support")
                for c, ps in repointed.items()
                for p in ps
            ]
            if drop:
                edges_removed = await storage.delete_lineage(drop)
            old = sorted(retracted)
            links = [
                LineageEdge(
                    child=new, parent=o, role="version_of", parent_version=max(1, version - 1)
                )
                for new, o in zip(fresh, old, strict=False)
            ]
            if links:
                await storage.add_lineage(links, ns)
        tap.event(
            "write.intake.retire",
            retracted=len(retracted),
            archived=len(retiring),
            repointed=len(repointed),
            edges_removed=edges_removed,
        )
        return {"archived": sorted(retiring), "repointed": sorted(repointed)}


@dataclass(frozen=True)
class GatewayResult:
    receipt: Receipt
    record_ids: tuple[str, ...]  # one per segment that became a record, in segment order
    replayed: bool = False
    noop: bool = False


# ------------------------------------------------------------------------------------------ helpers
def _digest(*parts: Any) -> str:
    blob = json.dumps(parts, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def _scope_of(ns: str, session: str | None, principal: str | None) -> Scope:
    tenant, _, owner = ns.partition("/")
    return Scope(tenant=tenant, user=owner or "~", session=session, principal=principal)


def _is_memory_db(eng: Any) -> bool:
    sql = eng._sql
    return bool(sql is not None and sql.is_memory)


def capabilities_of(eng: Any) -> dict[str, bool]:
    """Which receipt capabilities this engine will actually produce for a write (S7).

    ``raw_search``: always (the raw record is searchable at return). ``facts`` needs the window
    pipeline; ``graph`` its edge section; ``summaries`` its episode section. ``profile`` has no
    per-write step, so it is never claimed."""
    mode = eng._config().write.window_extract
    windows = getattr(eng, "_windows", None) is not None
    return {
        "raw_search": True,
        "facts": windows,
        "graph": windows and mode in ("facts_graph", "all"),
        "summaries": windows and mode == "all",
        "profile": False,
    }


def _progress(intent_id: str, current: Readiness, progress: Mapping[str, Any]) -> Readiness:
    """Apply readiness progress with the book's monotone rules."""
    ov = store.Overlay()
    ov._readiness[intent_id] = current
    return IntakeBook(ov).readiness(intent_id, progress)


def _turn_json(turn: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(dict(turn), default=str))  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt


def _name_of(turn: Mapping[str, Any], text: str) -> str:
    for key in ("name", "entity"):
        v = turn.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return " ".join(text.split()[:6]) or "unnamed"


def _with_wid(turn: Mapping[str, Any], wid: str) -> dict[str, Any]:
    return {**dict(turn), "_idem": wid}


# ------------------------------------------------------------------------------------------ gateway
class IntakeGateway:
    """One per engine. ``fault`` (tests): called with a boundary name; raise to simulate a crash."""

    def __init__(self, eng: Any) -> None:
        self.eng = eng
        self.fault: Callable[[str], None] | None = None
        self.last_receipt: Receipt | None = None
        self.last_new: set[str] = set()  # record ids this gateway call wrote (not replayed or kept)
        self.quota_check: Callable[[IntakeCost], QuotaExceeded | None] | None = None
        self._db: AsyncEngine | None = None
        self._message_recovery_needed = False
        self._locks: dict[str, asyncio.Lock] = {}
        self.versioner = SourceVersioner(eng)

    # -- plumbing -------------------------------------------------------------------------------
    def _boundary(self, name: str) -> None:
        if self.fault is not None:
            self.fault(name)

    async def _engine(self) -> AsyncEngine:
        storage = self.eng._require_started()
        if getattr(storage, "_client", None) is None:
            # RM-112: the intake book and the message index are SQL tables of the records
            # database; a records store without SQL (MongoDB) has neither.
            provider = self.eng._config().storage.records.provider
            raise ConfigError(
                "ingestion through the intake gateway (ingest_messages, ingest_data, "
                "ingest_asset) needs a SQL records store: storage.records.provider "
                f"{provider!r} keeps no intake book or message index; use write() or a "
                "SQLite/Postgres records store"
            )
        db: AsyncEngine = storage._client.engine
        if db is not self._db:
            await store.ensure_schema(db)
            self._db = db
        return db

    def _lock(self, skey: str) -> asyncio.Lock:
        return self._locks.setdefault(skey, asyncio.Lock())

    # -- the door ---------------------------------------------------------------------------------
    async def submit(
        self, env: SourceEnvelope, ns: str, adapter: str, args: dict[str, Any]
    ) -> GatewayResult:
        """Accept ``env`` and make its new segments records. ``adapter`` + ``args`` are what the apply
        needs besides the segments (persisted with the intent so a restart can finish it)."""
        eng = self.eng
        db = await self._engine()
        self.last_new = set()
        validated = validate_source(env)
        venv = validated.envelope
        skey = scope_key(venv.scope)
        async with self._lock(skey):
            now = datetime.now(UTC)
            probe = IntakeBook()
            if venv.kind == "document":
                head = await store.load_overlay(db, skey, venv.source_id, [])
                prev = head.get_source(skey, venv.source_id)
                version = venv.version or ((prev.latest_version if prev else 0) + 1)
            else:
                version = 1
            wids = probe._ids(venv, skey, version)
            ov = await store.load_overlay(db, skey, venv.source_id, wids)
            known_intents = {
                lw.intent_id for w in wids if (lw := ov.get_write(skey, w)) is not None
            }
            before = ov.get_source(skey, venv.source_id)
            book = IntakeBook(ov, ephemeral=_is_memory_db(eng))
            self._boundary("pre_accept")
            out = book.accept_validated(validated, capabilities_of(eng), self.quota_check, now=now)
            if isinstance(out, QuotaExceeded):
                tap.event("write.intake.rejected", reason="quota", quota=out.quota)
                raise QuotaExceededError(out)
            if ov.change is None:  # replay or no-op: nothing new was decided
                await self._resume_many(known_intents | {out.receipt.intent_id})
                return await self._result(db, skey, out.receipt, replayed=True, noop=out.noop)
            change = ov.change
            payload = self._payload(validated, ns, adapter, args, out, before, wids, version)
            self._boundary("pre_commit")
            await store.commit(
                db,
                change,
                namespace=ns,
                apply_payload=json.dumps(payload, default=str),
                created_at=now.isoformat(),
            )
            tap.event(
                "write.intake.committed",
                intent_id=change.receipt.intent_id,
                fresh=len(payload["fresh"]),
                retried=len(change.receipt.retried),
                version=change.version,
            )
            self._boundary("post_commit")
            await self._resume_many(known_intents - {change.receipt.intent_id})
            receipt, landed = await self._finish(
                db, change.receipt.intent_id, payload, resume=False
            )
            self.last_new = set(landed.values())
            return await self._result(db, skey, receipt)

    def _payload(
        self,
        validated: ValidatedEnvelope,
        ns: str,
        adapter: str,
        args: dict[str, Any],
        out: Any,
        before: SourceState | None,
        wids: Sequence[str],
        version: int,
    ) -> dict[str, Any]:
        env = validated.envelope
        fresh_idx = [e.payload["seg_index"] for e in out.events if e.kind == "memory.write"]
        fresh_wid = {
            e.payload["seg_index"]: e.payload["write_id"]
            for e in out.events
            if e.kind == "memory.write"
        }
        order = [
            e[len("evt-") :] for e in out.receipt.event_ids
        ]  # LIB-30: event id = evt-<write id>
        retract_wids: list[str] = []
        if out.plan is not None and before is not None:
            gone = set(out.plan.retract_event_ids)
            retract_wids = [o.write_id for o in before.live if o.event_id in gone]
        body: dict[str, Any] = {
            "adapter": adapter,
            "namespace": ns,
            "skey": scope_key(env.scope),
            "source_id": env.source_id,
            "version": version,
            "order": order,
            "retract": retract_wids,
            "args": args,
            "fresh": [{"i": i, "wid": fresh_wid[i]} for i in fresh_idx],
        }
        if adapter == "messages":
            turns = args["turns"]
            for item in body["fresh"]:
                i = item["i"]
                turn = dict(turns[i])
                segment = env.segments[i]
                turn["role"] = segment.role
                turn["content"] = segment.content
                # The adapter already folded the caption into validated content.
                turn.pop("caption", None)
                turn["speaker"] = segment.speaker
                if turn.get("created_at") is not None:
                    # Resolved message time preserves source precision and whether
                    # the caller supplied a zone; source_at is a normalized instant.
                    turn["timestamp"] = turn["created_at"]
                elif segment.source_at is not None:
                    turn["timestamp"] = segment.source_at
                item["turn"] = _turn_json(turn)
            body["args"] = {k: v for k, v in args.items() if k != "turns"}
            body["all_wids"] = {str(i): w for i, w in enumerate(wids)}
        else:
            for item in body["fresh"]:
                item["text"] = env.segments[item["i"]].content
            body["args"] = {k: v for k, v in args.items() if k != "chunks"}
        return body

    # -- apply ------------------------------------------------------------------------------------
    async def _resume_many(self, intent_ids: Sequence[str] | set[str]) -> None:
        db = await self._engine()
        rows = await store.intents_by_id(db, intent_ids)
        for iid in sorted(rows, key=lambda i: (rows[i]["created_at"], i)):
            if rows[iid]["status"] == "accepted":
                await self._finish(db, iid, json.loads(rows[iid]["apply"]), resume=True)

    async def _finish(
        self, db: AsyncEngine, intent_id: str, payload: dict[str, Any], *, resume: bool
    ) -> tuple[Receipt, dict[str, str]]:
        skey = payload["skey"]
        landed = await self._apply(payload, resume=resume)
        self._boundary("pre_mark")
        rows = await store.intents_by_id(db, [intent_id])
        row = rows[intent_id]
        known = await store.record_ids(db, skey, [w for w in payload["order"] if w not in landed])
        real = {**known, **landed}
        receipt = Receipt.model_validate_json(row["receipt"])
        readiness = Readiness.model_validate_json(row["readiness"])
        readiness = _progress(intent_id, readiness, {"raw_search": "ready"})
        receipt = receipt.model_copy(
            update={
                "record_ids": [real[w] for w in payload["order"] if w in real],
                "readiness": readiness,
            }
        )
        await store.applied_intent(
            db,
            intent_id=intent_id,
            skey=skey,
            source_id=payload["source_id"],
            real_ids=landed,
            receipt=receipt,
            readiness=readiness,
        )
        tap.event(
            "write.intake.applied",
            intent_id=intent_id,
            records=len(landed),
            resumed=resume,
            raw_search=readiness.raw_search,
        )
        self._boundary("post_mark")
        self.last_receipt = receipt
        return receipt, landed

    async def _scan_landed(self, ns: str, memory_type: str, wids: Sequence[str]) -> dict[str, str]:
        storage = self.eng._require_started()
        want = set(wids)
        found: dict[str, str] = {}
        prefix = IDEM_TAG_PREFIX
        for rec in await storage.list_records(ns, memory_type):
            for tag in rec.tags:
                if tag.startswith(prefix) and tag[len(prefix) :] in want:
                    found[tag[len(prefix) :]] = rec.record_id
        return found

    async def _apply(self, payload: dict[str, Any], *, resume: bool) -> dict[str, str]:
        if payload["adapter"] == "messages":
            return await self._apply_messages(payload, resume)
        if payload["adapter"] == "asset":
            return await self._apply_asset(payload, resume)
        return await self._apply_document(payload, resume)

    async def _apply_messages(self, p: dict[str, Any], resume: bool) -> dict[str, str]:
        eng = self.eng
        ns = p["namespace"]
        a = p["args"]
        fresh = p["fresh"]
        landed: dict[str, str] = {}
        if resume:
            landed = await self._scan_landed(ns, "episodic", [f["wid"] for f in fresh])
            tap.event(
                "write.intake.resume", intent=p["source_id"], landed=len(landed), fresh=len(fresh)
            )
        fw = eng._config().firewall
        todo = []
        for f in fresh:
            if f["wid"] in landed:
                continue
            turn = f["turn"]
            if resume and (
                turn.get("role") in fw.skip_message_roles
                or (fw.skip_injected_recall and _looks_like_recall(str(turn.get("content", ""))))
            ):
                continue  # legacy writes no record for these; do not replay their audit marker
            todo.append(f)
        sink: dict[str, str] = {}
        if todo:
            position = {f["i"]: n for n, f in enumerate(todo)}
            by_index = {int(i): w for i, w in p["all_wids"].items()}
            turns: list[dict[str, Any]] = []
            for f in todo:
                turn = _with_wid(f["turn"], f["wid"])
                ref = turn.get("reply_to")
                if isinstance(ref, int) and not isinstance(ref, bool):
                    if ref in position:
                        turn["reply_to"] = position[ref]
                    else:
                        rid = landed.get(by_index.get(ref, ""))
                        if rid is None:
                            rid = (
                                await store.record_ids(
                                    await self._engine(), p["skey"], [by_index.get(ref, "")]
                                )
                            ).get(by_index.get(ref, ""))
                        if rid is not None:
                            turn["reply_to"] = rid
                        else:
                            turn.pop("reply_to")
                turns.append(turn)
            token = INTAKE_LANDED.set(sink)
            try:
                await ingest_write.apply_messages(
                    eng,
                    turns,
                    ns,
                    a["actor"],
                    a["session_id"],
                    a["channel"],
                    a["group_id"],
                    a["tags"],
                    datetime.fromisoformat(a["valid_from"]) if a["valid_from"] else None,
                )
            finally:
                INTAKE_LANDED.reset(token)
        if eng._config().write.role_policy:  # W2-03: keyed turns also get a typed record
            await self._route_typed(p, fresh, {**landed, **sink})
        return {**landed, **sink}

    async def _route_typed(
        self, p: dict[str, Any], fresh: list[dict[str, Any]], raw_ids: dict[str, str]
    ) -> None:
        """W-09: a keyed entity/attribute turn, a skill or a plan is also written as a typed record whose
        parent is its raw turn (``derived_from``). Unkeyed turns stay raw. Best effort: the raw write is
        durable, so a typed failure is counted and tapped, never raised. Idempotent on resume: the typed
        record carries the turn's write id, and one that already landed is not written twice."""
        eng = self.eng
        ns = p["namespace"]
        a = p["args"]
        router = role_wiring.TypeRouter()
        routed = [(f, router.route(f["turn"])) for f in fresh]
        keyed = [(f, r) for f, r in routed if r.typed]
        tap.event(
            "write.route.decision",
            turns=len(routed),
            keyed=len(keyed),
            unkeyed=len(routed) - len(keyed),
        )
        metrics = role_wiring.metrics_of(eng)
        metrics["route.unkeyed"] += len(routed) - len(keyed)
        if not keyed:
            return
        storage = eng._require_started()
        done: dict[str, str] = {}
        for kind in {r.typed for _, r in keyed if r.typed}:
            done.update(await self._scan_landed(ns, kind, [f["wid"] for f, _ in keyed]))
        for f, r in keyed:
            assert r.typed is not None
            wid = f["wid"]
            if wid in done:
                continue
            raw_id = raw_ids.get(wid)
            raw = await storage.get_record(raw_id) if raw_id else None
            if raw is None or raw.quarantined or role_filter.block_reason(raw.tags) is not None:
                metrics["route.skipped"] += 1
                tap.event("write.route.skipped", reason="raw_unusable", type=r.typed, wid=wid)
                continue
            try:
                tags = [f"{IDEM_TAG_PREFIX}{wid}", constants.ROUTED_TAG]
                if r.typed in ("skill", "plan"):  # procedural: kind rides as the attribute
                    name = _name_of(f["turn"], r.text or raw.content)
                    rec = make_skill_record(
                        ns,
                        name,
                        r.text or raw.content,
                        kind=r.typed,  # type: ignore[arg-type]
                        source=SourceInfo(
                            role=raw.source.role,
                            channel=a["channel"],
                            message_id=a["session_id"],
                            parents=[raw.record_id],
                        ),
                    )
                    rec = rec.model_copy(update={"tags": tags})
                    typed = await eng._write_procedural(rec, a["actor"])
                else:
                    typed = await eng.write(
                        r.text or raw.content,
                        namespace=ns,
                        memory_type=r.typed,
                        source=SourceInfo(
                            role=raw.source.role, channel=a["channel"], message_id=a["session_id"]
                        ),
                        actor=a["actor"],
                        entity=r.entity,
                        attribute=r.attribute,
                        group_id=a["group_id"],
                        tags=tags,
                        derived_from=[raw.record_id],
                        valid_from=raw.valid_from,
                    )
            except Exception as exc:  # the raw turn is already durable
                metrics["route.failed"] += 1
                tap.event("write.route.failed", type=r.typed, wid=wid, error=type(exc).__name__)
                continue
            metrics[f"route.{r.typed}"] += 1
            tap.event(
                "write.route.typed",
                type=r.typed,
                reason=r.reason,
                id=typed.record_id,
                parent=raw.record_id,
            )

    async def _apply_document(self, p: dict[str, Any], resume: bool) -> dict[str, str]:
        eng = self.eng
        ns = p["namespace"]
        a = p["args"]
        fresh = p["fresh"]
        landed: dict[str, str] = {}
        if resume:
            landed = await self._scan_landed(ns, "resource", [f["wid"] for f in fresh])
            tap.event(
                "write.intake.resume", intent=p["source_id"], landed=len(landed), fresh=len(fresh)
            )
        todo = [(f["i"], f["wid"], f["text"]) for f in fresh if f["wid"] not in landed]
        if todo:
            async with eng._write_locks.setdefault(ns, asyncio.Lock()):
                recs = await eng._resource.ingest_chunks(
                    ns,
                    a["path"],
                    todo,
                    version=p["version"],
                    pii_tier=PiiTier(a["pii_tier"]),
                    actor=a["actor"],
                )
            landed.update({w: r.record_id for w, r in recs.items()})
        if p["retract"]:
            db = await self._engine()
            gone = await store.record_ids(db, p["skey"], p["retract"])
            self._boundary("pre_retire")
            await self.versioner.retire(
                ns,
                [gone[w] for w in p["retract"] if w in gone],
                [landed[f["wid"]] for f in fresh if f["wid"] in landed],
                version=p["version"],
            )
        return landed

    async def _apply_asset(self, p: dict[str, Any], resume: bool) -> dict[str, str]:
        """W2-11: an ``asset`` envelope. The record is the REFERENCE (hash, URI, mime, size; never
        the bytes). Under ``write.asset_describer: ingest`` the description follows as a derived record."""
        from memspine.ingest import asset_describer as ad

        eng = self.eng
        ns = p["namespace"]
        a = p["args"]
        fresh = p["fresh"]
        landed: dict[str, str] = {}
        if resume:
            landed = await self._scan_landed(ns, "resource", [f["wid"] for f in fresh])
        todo = [(f["i"], f["wid"], f["text"]) for f in fresh if f["wid"] not in landed]
        info = ad.AssetInfo(**a["asset"])
        if todo:
            async with eng._write_locks.setdefault(ns, asyncio.Lock()):
                recs = await eng._resource.ingest_chunks(
                    ns,
                    info.uri,
                    todo,
                    version=p["version"],
                    pii_tier=PiiTier(a["pii_tier"]),
                    actor=a["actor"],
                    extra_tags=ad.asset_tags(info),
                )
            landed.update({w: r.record_id for w, r in recs.items()})
            tap.event("write.asset.stored", assets=len(recs), hash=info.hash, mime=info.mime)
        if eng._config().write.asset_describer == "ingest":
            storage = eng._require_started()
            for rid in landed.values():
                rec = await storage.get_record(rid)
                if rec is None:
                    continue
                try:
                    await ad.describe_and_deposit(eng, ns, rec, trigger="ingest")
                except Exception as exc:
                    tap.event("write.asset.failed", asset=rid, error=type(exc).__name__)
        return landed

    async def _result(
        self,
        db: AsyncEngine,
        skey: str,
        receipt: Receipt,
        *,
        replayed: bool = False,
        noop: bool = False,
    ) -> GatewayResult:
        """The caller's answer: real record ids per segment, and the current readiness."""
        wids = [e[len("evt-") :] for e in receipt.event_ids]
        real = await store.record_ids(db, skey, wids)
        row = (await store.intents_by_id(db, [receipt.intent_id])).get(receipt.intent_id)
        update: dict[str, Any] = {"record_ids": [real[w] for w in wids if w in real]}
        if row is not None:
            update["readiness"] = Readiness.model_validate_json(row["readiness"])
        receipt = receipt.model_copy(update=update)
        self.last_receipt = receipt
        return GatewayResult(receipt, tuple(receipt.record_ids), replayed=replayed, noop=noop)

    # -- status -----------------------------------------------------------------------------------
    async def receipt(self, intent_id: str) -> Receipt | None:
        db = await self._engine()
        row = (await store.intents_by_id(db, [intent_id])).get(intent_id)
        if row is None:
            return None
        return Receipt.model_validate_json(row["receipt"]).model_copy(
            update={"readiness": Readiness.model_validate_json(row["readiness"])}
        )

    async def status(self, intent_id: str, timeout: float = 0.0) -> DerivationIntent:  # noqa: ASYNC109
        """Refresh the intent's readiness from the engine and report it per layer (W-02)."""
        db = await self._engine()
        row = (await store.intents_by_id(db, [intent_id]))[intent_id]
        readiness = Readiness.model_validate_json(row["readiness"])
        receipt = Receipt.model_validate_json(row["receipt"])
        applied = row["status"] == "applied"
        if applied:
            progress = await self._window_progress(row["namespace"], receipt, readiness, timeout)
            if progress:
                readiness = _progress(intent_id, readiness, progress)
                await store.set_readiness(db, intent_id, readiness)
                tap.event(
                    "write.intake.readiness",
                    intent_id=intent_id,
                    **{k: v for k, v in progress.items()},
                )
        return DerivationIntent(
            intent_id, row["source_id"], receipt.source_version, readiness, applied=applied
        )

    async def wait(
        self,
        intent_id: str,
        capabilities: Sequence[str],
        timeout: float = 0.0,  # noqa: ASYNC109
    ) -> WaitResult:
        """The readiness reached for ``capabilities`` within ``timeout`` (never raises on timeout)."""
        intent = await self.status(intent_id, timeout)
        req = list(dict.fromkeys(capabilities))
        bad = [c for c in req if c not in CAPABILITIES]
        if bad:
            raise KeyError(bad[0])
        return WaitResult(
            requested=req,  # type: ignore[arg-type]
            readiness=intent.readiness,
            timed_out=bool(intent.readiness.pending(req)),
            waited_ms=0,
        )

    async def _window_progress(
        self,
        ns: str,
        receipt: Receipt,
        readiness: Readiness,
        timeout: float,  # noqa: ASYNC109
    ) -> dict[str, str]:
        pending = [c for c in ("facts", "graph", "summaries") if readiness.get(c) == "pending"]
        if not pending:
            return {}
        pipeline = getattr(self.eng, "_windows", None)
        if pipeline is None or getattr(pipeline, "_outbox", None) is None:
            return {}  # no durable queue: derivation cannot be proved, so it stays pending
        open_ids = {t.record_id for t in pipeline._store(ns).segmenter.open_turns}
        if open_ids & set(receipt.record_ids):
            return {}  # a turn of this write still waits in an open window
        out = await pipeline.readiness(ns, ("window",), timeout)
        if out is None or "window" not in out.ready:
            return {}
        return dict.fromkeys(pending, "ready")


# ------------------------------------------------------------------------------------------ module API
def gateway_of(eng: Any) -> IntakeGateway:
    gw = getattr(eng, "_intake_gateway", None)
    if gw is None:
        gw = IntakeGateway(eng)
        eng._intake_gateway = gw
    return gw


async def recover(eng: Any) -> int:
    """Finish every accepted-but-unapplied intent (called from ``start()``). Returns how many."""
    gw = gateway_of(eng)
    db = await gw._engine()
    done = 0
    for row in await store.pending_intents(db):
        async with gw._lock(row["scope_key"]):
            now = await store.intents_by_id(db, [row["intent_id"]])
            if now[row["intent_id"]]["status"] != "accepted":
                continue
            await gw._finish(db, row["intent_id"], json.loads(row["apply"]), resume=True)
            done += 1
    if done:
        tap.event("write.intake.recovered", intents=done)
    return done


def _turn_text(turn: Mapping[str, Any]) -> str:
    return _turn_content(turn)


async def ingest_resolved_messages(
    eng: Any,
    batch: ValidatedMessageBatch,
    namespace: str,
    actor: str,
    session_id: str | None,
    channel: str,
    group_id: str | None,
    tags: list[str] | None,
    valid_from: datetime | None,
) -> list[MemoryRecord]:
    """Submit normalized messages only; the batch owns resolved message fields and content."""
    if not isinstance(batch, ValidatedMessageBatch):
        raise TypeError("ingest_resolved_messages requires a ValidatedMessageBatch")
    ns = validate_namespace(namespace)
    turns = batch.to_messages()
    segments: list[Segment] = []
    for i, turn in enumerate(turns):
        stamp = eng._event_time(turn.get("timestamp")) or (
            eng._localize(valid_from) if valid_from is not None else None
        )
        who = turn.get("speaker") or turn.get("name")
        segments.append(
            Segment(
                seg_index=i,
                role=turn["role"],
                speaker=who if isinstance(who, str) else None,
                source_at=stamp,
                content=_turn_text(turn),
            )
        )
    if not segments:
        return []
    source_digest = _digest([_turn_json(t) for t in turns])
    env = SourceEnvelope(
        source_id=f"{session_id or 'nosession'}#{source_digest}",
        kind="conversation",
        scope=_scope_of(ns, session_id, actor),
        segments=segments,
    )
    args = {
        "turns": turns,
        "actor": actor,
        "session_id": session_id,
        "channel": channel,
        "group_id": group_id,
        "tags": list(tags) if tags else None,
        "valid_from": valid_from.isoformat() if valid_from is not None else None,
    }
    gw = gateway_of(eng)
    res = await gw.submit(env, ns, "messages", args)
    storage = eng._require_started()
    out: list[MemoryRecord] = []
    for rid in res.record_ids:
        rec = await storage.get_record(rid)
        if rec is not None:
            out.append(rec)
    return out


async def ingest_data(
    eng: Any, path: str | Path, namespace: str = "default", pii_tier: PiiTier = PiiTier.NONE
) -> list[MemoryRecord]:
    """``ingest`` as an intake adapter: a document is a versioned source; chunks are segments.
    Returns the NEW records (an unchanged document or a retry returns none)."""
    from memspine.memories.resource.extraction import chunk_text, extract_text

    ns = validate_namespace(namespace)
    source = Path(path)
    text = await asyncio.to_thread(extract_text, source)
    chunks = await asyncio.to_thread(chunk_text, text)
    if not chunks:
        return []
    env = SourceEnvelope(
        source_id=str(await asyncio.to_thread(source.resolve)),
        kind="document",
        scope=_scope_of(ns, None, "user"),
        segments=[Segment(seg_index=i, role="document", content=c) for i, c in enumerate(chunks)],
    )
    gw = gateway_of(eng)
    res = await gw.submit(
        env,
        ns,
        "document",
        {
            "chunks": list(chunks),
            "path": str(source),
            "pii_tier": PiiTier(pii_tier).value,
            "actor": "user",
        },
    )
    storage = eng._require_started()
    out: list[MemoryRecord] = []
    for rid in res.record_ids:
        if rid in gw.last_new:
            rec = await storage.get_record(rid)
            if rec is not None:
                out.append(rec)
    return out


async def ingest_asset(
    eng: Any,
    path_or_uri: str | Path,
    namespace: str = "default",
    *,
    mime: str | None = None,
    size: int | None = None,
    content_hash: str | None = None,
    alt_text: str | None = None,
    pii_tier: PiiTier = PiiTier.NONE,
) -> MemoryRecord | None:
    """W2-11: store an asset reference (``asset`` envelope; ``write.asset_describer`` must not be
    ``off``). The same asset (content hash) in a namespace is stored
    once. Returns the asset record, or None when it was already stored."""
    from memspine.exceptions import ConfigError
    from memspine.ingest import asset_describer as ad

    cfg = eng._config().write
    if cfg.asset_describer == "off":
        raise ConfigError("ingest_asset needs write.asset_describer: ingest | on_read")
    ns = validate_namespace(namespace)
    info = await asyncio.to_thread(
        ad.asset_info_from_file,
        path_or_uri,
        mime=mime,
        size=size,
        content_hash=content_hash,
        alt_text=alt_text,
    )
    env = SourceEnvelope(
        source_id=f"asset:{info.hash}",
        kind="asset",
        scope=_scope_of(ns, None, "user"),
        segments=[
            Segment(
                seg_index=0,
                role="document",
                content=ad.reference_text(info),
                assets=[
                    AssetRef(
                        asset_id=info.hash[:16],
                        hash=info.hash,
                        mime=info.mime,
                        url_or_path=info.uri,
                    )
                ],
            )
        ],
    )
    gw = gateway_of(eng)
    res = await gw.submit(
        env,
        ns,
        "asset",
        {
            "chunks": [ad.reference_text(info)],
            "path": info.uri,
            "pii_tier": PiiTier(pii_tier).value,
            "actor": "user",
            "asset": {
                "hash": info.hash,
                "uri": info.uri,
                "mime": info.mime,
                "size": info.size,
                "alt_text": info.alt_text,
                "meta": dict(info.meta),
            },
        },
    )
    storage = eng._require_started()
    for rid in res.record_ids:
        if rid in gw.last_new:
            return await storage.get_record(rid)  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
    return None
