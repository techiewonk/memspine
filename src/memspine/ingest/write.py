"""The write path: write, batch write, governance screens, correction (F-09; W-01, W-09)."""

from __future__ import annotations

import asyncio
import re
from collections.abc import (
    Callable,
    Mapping,
    Sequence,
)
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Any

from memspine.config import constants
from memspine.config.schema import FirewallConfig
from memspine.core.correction import Correction, detect_correction
from memspine.core.forget_request import is_forget_request
from memspine.core.inference import (
    INFERRED_CHANNELS,
    INFERRED_TAG,
    classify,
    is_inferred,
    support_count,
    support_tags,
)
from memspine.core.reference_edges import REF_PREFIX, antecedent_kind
from memspine.core.sensitive import sensitive_topics
from memspine.core.sensitivity import (
    GRADE_TAG_PREFIX,
    grade_rank,
    grade_text,
    label_of,
    label_tags,
    passes_gate,
    query_topics,
)
from memspine.exceptions import (
    ConflictError,
)
from memspine.ingest.stamping import (
    ASSENT_TAG_PREFIX,
    PENDING_STAMP,
    ConversationStamper,
    PendingStamp,
    apply_stamp,
    participants_of_call,
)
from memspine.ingest.timeparse import _zone_of
from memspine.memories.episodic.lifecycle import session_of
from memspine.models.encoders.embedding.base import EmbeddingService
from memspine.models.encoders.query.shape import (
    content_words,
)
from memspine.models.encoders.query.temporal_query import (
    RECOMMENDATION_TAG,
    SPEAKER_PREFIX,
    is_recommendation,
    speaker_of,
)
from memspine.observability import tap
from memspine.observability.logging import (
    EVENT_WRITE,
    get_logger,
    redact_error,
)
from memspine.services.storage.sql_base import SqlStorage
from memspine.services.vector.base import VectorHit
from memspine.store.events import EventKind, MemoryEvent, fingerprint_payload
from memspine.store.records import (
    MemoryRecord,
    PiiTier,
    RecordStatus,
    SourceInfo,
    new_record_id,
)
from memspine.store.scope import validate_namespace
from memspine.store.tri_temporal import derived_times, turn_times
from memspine.trust.firewall import FirewallVerdict
from memspine.trust.pipeline import consent as trust_consent
from memspine.trust.pipeline.gates_v2 import GATE_HOLD
from memspine.trust.redaction import find_pii, redact
from memspine.trust.visibility import (
    PARTICIPANT_PREFIX,
    VISIBILITY_PREFIX,
    inherit_tags,
    participant_tags,
    visibility_tag,
)

_log = get_logger(__name__)
CUE_TAG = constants.CUE_TAG


#: H21: markers memspine itself writes into assembled context. A message carrying them is
#: recalled memory echoed back, not a new observation. Built from the emitters' constants
#: (R5-4) so a new or reworded wrapper cannot silently slip past the filter.
_RECALL_MARKERS = (
    constants.UNTRUSTED_NOTE_MARKER,
    constants.CURRENT_STATE_MARKER,
    constants.HISTORY_MARKER,
    constants.DISPUTED_MARKER,
    constants.INSTRUCTION_FLAG_MARKER,
    constants.TIMELINE_MARKER,
    constants.STANDING_MARKER,
    constants.CLAIM_MARKER,
    constants.CARDS_MARKER,
    constants.PROFILE_MARKER,
    constants.COUNT_MARKER,
)


#: #44: ordering of PII tiers, least to most sensitive.
_PII_RANK = {PiiTier.NONE: 0, PiiTier.LOW: 1, PiiTier.HIGH: 2, PiiTier.REGULATED: 3}


def _codes(reasons: Sequence[str]) -> list[str]:
    """WT-02: firewall reasons reduced to short codes (``size_anomaly(9>5)`` -> ``size_anomaly``)."""
    return [r.split("(", 1)[0] for r in reasons]


def _screen_text(text: str, fw: FirewallConfig) -> tuple[str, list[str]]:
    """``text`` as the write door stores it: secrets masked under ``redact_secrets``,
    PII masked under ``pii: redact``; unchanged when both are off."""
    return redact(
        text, secrets=fw.redact_secrets, pii=fw.pii == "redact", pii_extended=fw.pii_extended
    )


def _looks_like_recall(content: str) -> bool:
    return any(marker in content for marker in _RECALL_MARKERS)


def _reply_targets(record: MemoryRecord) -> list[str]:
    """G27: the record ids ``record``'s ``reply_to:<id>`` tags name."""
    prefix = constants.REPLY_TO_PREFIX
    return [tag[len(prefix) :] for tag in record.tags if tag.startswith(prefix) and tag != prefix]


def _reply_target(turn: Mapping[str, Any], index: int, written: Mapping[int, str]) -> str | None:
    """G27: the record a ``write_messages`` turn answers (its ``reply_to`` field).

    A string is a record id; an int is the index of an earlier message of the same
    call (one skipped as system/tool text or injected recall links to nothing).
    Anything else, or an index that is not earlier, is a ``ValueError``."""
    raw: object = turn.get("reply_to")
    if raw is None:
        return None
    if isinstance(raw, str) and raw:
        return raw
    if isinstance(raw, int) and not isinstance(raw, bool) and 0 <= raw < index:
        return written.get(raw)
    raise ValueError(
        f"messages[{index}]['reply_to'] must be a record id or the index of an earlier message"
    )


def _caller_tags(tags: Sequence[str] | None, ns: str) -> list[str]:
    """Caller-supplied tags without the engine-only ones (``RESERVED_TAGS``): a
    caller must not mark its text as a lead block, a cue or a rolled-back record."""
    kept = [tag for tag in tags or [] if tag not in constants.RESERVED_TAGS]
    if len(kept) != len(tags or []):
        dropped = sorted({tag for tag in tags or [] if tag in constants.RESERVED_TAGS})
        _log.warning("memory.reserved_tags_dropped", namespace=ns, tags=dropped)
    return kept


def _merge_correction_tags(old: Sequence[str], extra: Sequence[str] | None, ns: str) -> list[str]:
    """Tags of a corrected record: the old ones minus ``disputed``, plus the caller's
    (reserved tags dropped); a new ``src:*`` tag replaces the old ``src:*`` ones."""
    added = _caller_tags(extra, ns)
    keep = [t for t in old if t != "disputed"]
    if any(t.startswith("src:") for t in added):
        keep = [t for t in keep if not t.startswith("src:")]
    return [*keep, *(t for t in added if t not in keep)]


@dataclass(frozen=True)
class WriteOutcome:
    """What ``Engine.write_ex`` did (G8): the surviving record, the action taken,
    and a fresh per-call occurrence id."""

    record: MemoryRecord
    action: str
    occurrence_id: str


class _NeighbourBatch:
    """#63: the firewall's nearest-neighbour context for one ``write_messages`` call.

    ``prefetched`` holds, per content, the top-k hits of ONE batched vector query
    made before the first turn is written. Every WRITE or FORGET the namespace
    sees while the batch is open is observed (whatever task made it), so
    :meth:`neighbours` ranks those prefetched hits together with the rows written
    since, exactly the set a per-turn query would rank.
    """

    def __init__(self, namespace: str, prefetched: dict[str, list[VectorHit]]) -> None:
        self.namespace = namespace
        self.prefetched = prefetched
        self.written: dict[str, str] = {}  # record_id -> content, in write order
        self.forgotten: set[str] = set()
        self._ids: list[str] = []
        self._matrix: Any = None  # unit rows of the written vectors (float32)

    def observe(self, event: MemoryEvent) -> None:
        if event.kind is EventKind.WRITE:
            snapshot = event.payload.get("record")
            if isinstance(snapshot, dict) and snapshot.get("record_id") is not None:
                record_id = str(snapshot["record_id"])
                if record_id in self.written:
                    self._matrix = None  # a replaced row invalidates the cached rows
                self.written[record_id] = str(snapshot.get("content") or "")
                self.forgotten.discard(record_id)
        elif event.kind is EventKind.FORGET:
            record_id = str(event.payload.get("record_id"))
            self.forgotten.add(record_id)
            self.written.pop(record_id, None)
            self._matrix = None

    async def neighbours(
        self, content: str, vector: list[float], embedder: EmbeddingService, top_k: int
    ) -> list[VectorHit]:
        """Top-k cosine neighbours of ``vector`` over the prefetched and written rows."""
        written = [rid for rid in self.written if rid not in self.forgotten]
        hits = [
            hit
            for hit in self.prefetched[content]
            if hit.record_id not in self.written and hit.record_id not in self.forgotten
        ]
        if written:
            import numpy as np  # lancedb's own dependency; loaded only for batched ingest

            if self._matrix is None or written[: len(self._ids)] != self._ids:
                self._matrix, self._ids = np.zeros((0, len(vector)), dtype=np.float32), []
            fresh = written[len(self._ids) :]
            if fresh:  # rows are appended as turns are written: embed only the new ones
                rows = np.asarray(
                    await embedder.embed([self.written[rid] for rid in fresh]), dtype=np.float32
                )
                norms = np.linalg.norm(rows, axis=1, keepdims=True)
                self._matrix = np.vstack([self._matrix, rows / np.where(norms == 0, 1, norms)])
                self._ids = written
            query = np.asarray(vector, dtype=np.float32)
            norm = float(np.linalg.norm(query)) or 1.0
            scores = self._matrix @ (query / norm)
            hits.extend(
                VectorHit(record_id=rid, score=float(score))
                for rid, score in zip(written, scores, strict=True)
            )
        hits.sort(key=lambda hit: -hit.score)
        return hits[:top_k]


#: W2-01: a turn carrying ``_idem`` (its intake write id) is tagged ``idem:<id>``; the intake
#: gateway finds the records of an interrupted write by that tag. ``INTAKE_LANDED`` collects
#: write id -> record id for the gateway's current apply (None outside one).
IDEM_TAG_PREFIX = "idem:"
INTAKE_LANDED: ContextVar[dict[str, str] | None] = ContextVar("intake_landed", default=None)

#: #63: the neighbour batch of the ``write_messages`` call running in this task.
_NEIGHBOUR_BATCH: ContextVar[_NeighbourBatch | None] = ContextVar(
    "memspine_neighbour_batch", default=None
)


def _speaker_of(turn: Mapping[str, str]) -> str:
    """I53: who spoke a message: its ``speaker`` or ``name`` key, else its role."""
    return str(turn.get("speaker") or turn.get("name") or turn.get("role") or "")


def _turn_content(turn: Mapping[str, str]) -> str:
    """G30 (plan v3.2): a turn's text, with an image or attachment ``caption`` (when
    the turn carries one) appended as ``[image: caption]``, so it is embedded, indexed
    and read with the turn. Raises ``KeyError`` without ``content``, as before."""
    content = turn["content"]
    caption = turn.get("caption")
    return f"{content} [image: {caption}]".strip() if caption else content


def _message_sender(record: MemoryRecord) -> str | None:
    """B5: the sender namespace of a :meth:`Engine.send` message, else None."""
    if record.source.channel != "message":
        return None
    for tag in record.tags:
        if tag.startswith("from:") and tag[5:] != record.namespace:
            return tag[5:]
    return None


async def write(
    eng: Any,
    content: str,
    namespace: str = "default",
    memory_type: str = "semantic",
    source: SourceInfo | None = None,
    pii_tier: PiiTier = PiiTier.NONE,
    actor: str = "user",
    entity: str | None = None,
    attribute: str | None = None,
    group_id: str | None = None,
    tags: list[str] | None = None,
    derived_from: Sequence[str] | None = None,
    valid_from: datetime | None = None,
    session_id: str | None = None,
    ledger_id: str | None = None,
    extraction_hint: str | None = None,
    parent_weights: Mapping[str, float] | None = None,
    purposes: Sequence[str] | None = None,
    reply_to: str | None = None,
    participants: Sequence[str] | None = None,
    visibility: str | None = None,
) -> MemoryRecord:
    """Append a WRITE event through the single door; projection materializes it.

    ``entity``/``attribute`` key a semantic fact directly (M13.3) so the M4
    conflict ladder engages without requiring an extractor — callers that
    know the fact key should always pass it. ``group_id``/``tags`` (D2) are a
    sub-scoping facet within the namespace (a conversation, a document, a
    project) — a retrieval filter, never an isolation boundary.

    ``derived_from`` names the records the writer had in context when it
    produced ``content`` (own or granted). They are always recorded as
    ``source.parents``; with ``integrity.enabled`` they also cap the new
    record's trust at the least-trusted parent's view trust (MTI-D), which
    is what stops a paraphrase from laundering foreign content.

    ``valid_from`` sets the record's EVENT time (when the content was true or
    said), distinct from ``recorded_at`` (when the engine learned it). Default
    is "now", which keeps every existing caller unchanged.

    ``purposes`` (#50) are the read purposes the record may serve, stored as its
    ``consent_tags`` (``*`` = any); enforced only under ``consent.enforce``.

    ``reply_to`` (G27, ADR-061) names the record this one answers, in the same
    namespace (a missing, foreign or erased one is refused like a missing
    record). It is stored as a ``reply_to:<id>`` tag, so ``read.reply_links``
    can show the answered message beside a replayed reply; with associative
    memory on, a ``reply_to`` LINK (reply -> answered) is written too, unless
    either end is held or at its link budget (logged, the write stands).

    ``participants`` / ``visibility`` (I53): the speakers present (the first is the
    speaker of this record) and who may see it (``private`` | ``participants`` |
    ``owner_shared``), stored as ``participant:<name>`` / ``visibility:<v>`` tags. They
    matter only to a read with ``viewer=``; the namespace stays the isolation boundary.
    """
    if tap.enabled():
        tap.event(
            "write.received",
            memory_type=memory_type,
            actor=actor,
            chars=len(content),
            has_entity=entity is not None,
            reply=reply_to is not None,
        )
    wt = eng._write_timers  # I73: None (the default) costs one attribute read
    t_validation = perf_counter() if wt is not None else 0.0
    session_id = ledger_id if ledger_id is not None else session_id  # I9
    if participants:
        tags = [*(tags or []), *participant_tags(participants)]
    if visibility is not None:
        tags = [*(tags or []), visibility_tag(visibility)]
    if extraction_hint:  # G-8: rides as a tag; extract_graph passes it to the prompt
        tags = [*(tags or []), constants.EXTRACTION_HINT_PREFIX + extraction_hint.strip()]
    storage = eng._require_started()
    ns = validate_namespace(namespace)
    if reply_to is not None:
        target = await storage.get_record(reply_to)
        if target is None or target.namespace != ns or target.status is RecordStatus.DELETED:
            raise ConflictError(f"no such record {reply_to!r} in namespace {ns!r}")
        tags = [*(tags or []), f"{constants.REPLY_TO_PREFIX}{reply_to}"]
    # SEC-H1/ADR-018: the "shared" type is engine-internal bookkeeping
    # (grants + subscriptions carry authorization state). A public write of
    # it would forge a live grant — parse_grant only checks shape, so a
    # crafted content='{"grant":...}' would bypass grant()'s scope/self-grant/
    # supersession guards. Grants come ONLY from grant()/subscribe(), which
    # build the record and go through _write_locked directly.
    if memory_type == "shared":
        raise ConflictError("memory_type 'shared' is engine-internal — use grant()/subscribe()")
    source = source or SourceInfo(role=actor)
    tags = _caller_tags(tags, ns)
    if wt is not None:
        wt.record("validation", perf_counter() - t_validation)
    if memory_type == "episodic" and eng._memory_policy(eng._config(), "episodic").get(
        "subject_tagging"
    ):
        # W8 (plan v3.2): the speaker of a "Name: text" turn becomes a tag.
        speaker = speaker_of(content)
        if speaker and f"{SPEAKER_PREFIX}{speaker}" not in (tags or []):
            tags = [*(tags or []), f"{SPEAKER_PREFIX}{speaker}"]
    if memory_type == "episodic":
        opts = eng._perspective_options()
        if opts.on:  # I39: speaker / addressee / subject / stance tags; content untouched
            tags = await eng._annotate_perspective(ns, content, source.role, tags, opts, valid_from)
    implicit = eng._consume_reads(ns, session_id)
    parents = list(dict.fromkeys([*(derived_from or []), *implicit]))
    if parents:
        source = source.model_copy(update={"parents": parents})
    record = MemoryRecord(
        namespace=ns,
        memory_type=memory_type,
        content=content,
        source=source,
        pii_tier=pii_tier,
        entity=entity,
        attribute=attribute,
        group_id=group_id,
        tags=tags or [],
        consent_tags=list(dict.fromkeys(purposes or [])),
    )
    if memory_type == "episodic" and "spk:assistant" in record.tags:
        state = eng._persp_state.get(ns)
        if state is not None:  # I47: the turn the next user turn may acknowledge
            state["assistant"] = (record.record_id, content[:600])
    if valid_from is not None:
        stamp = eng._localize(valid_from)
        record = record.model_copy(update={"valid_from": stamp, **turn_times(stamp)})
    elif eng._config().read.skip_defaulted_dates:
        # I7: the event time is the write clock, not the source's; date renders skip it.
        record = record.model_copy(update={"tags": [*record.tags, constants.UNDATED_TAG]})
    pending_stamp = PENDING_STAMP.get()
    if pending_stamp is not None and memory_type == "episodic":  # W2-02: write.stamping_v2
        record = apply_stamp(record, pending_stamp)
    trust_cap = await eng._parent_trust_cap(ns, record.source.parents)
    if trust_cap is not None and parent_weights:
        # B1 weighted lineage: a parent of weight w contributes
        # w*view + (1-w)*base, so strong edges (w=1) keep the full MTI-D and
        # the radius; weak edges (e.g. retrieved but barely used) drain less.
        base = eng._firewall.policy.trust_at_write(record.source)
        weighted = []
        for parent_id, view in zip(record.source.parents, trust_cap, strict=False):
            w = min(1.0, max(0.0, parent_weights.get(parent_id, 1.0)))
            weighted.append(w * view + (1.0 - w) * base)
        trust_cap = weighted
    if implicit:
        # B0: the view trust the session SAW at read time also caps the write
        # (a record's trust may have dropped since; never raise it back).
        trust_cap = [*(trust_cap or []), *implicit.values()]
    # One writer per namespace: the firewall's context reads, the write,
    # and corroboration form one unit racing writers must not interleave.
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        written, action = await eng._write_locked_ex(
            storage, ns, record, memory_type, actor, trust_cap=trust_cap
        )
    eng._last_write_action = action
    if eng._sessions_horizon is not None and memory_type == "episodic":
        await eng._reopen_session(storage, ns, session_of(record))
    if reply_to is not None:
        await eng._link_reply(ns, written, reply_to)
    if eng._windows is not None and memory_type == "episodic" and action == "added":
        try:  # W1: the turn joins the open window; never fails the write
            await eng._windows.observe(written, session_of(written))
        except Exception as exc:
            _log.warning("window.observe_failed", namespace=ns, error=redact_error(exc))
    return written


async def _link_reply(eng: Any, ns: str, written: MemoryRecord, reply_to: str) -> None:
    """G27: the ``reply_to`` LINK beside the tag, when associative memory is on.
    A held endpoint or a full link budget skips it (the tag still links them)."""
    if eng._associative is None or written.quarantined or written.record_id == reply_to:
        return
    try:
        async with eng._write_locks.setdefault(ns, asyncio.Lock()):
            await eng._associative.link(
                ns,
                written.record_id,
                reply_to,
                rel=constants.REPLY_TO_REL,
                weight=max(0.01, min(1.0, written.trust)),
                reason="reply_to",
                actor="system",
            )
    except ConflictError as exc:
        _log.warning("memory.reply_link_skipped", namespace=ns, error=str(exc))


async def write_ex(eng: Any, content: str, **kwargs: Any) -> WriteOutcome:
    """``write`` that also reports what happened (G8).

    ``action`` is ``added`` | ``merged`` | ``updated`` | ``rejected`` |
    ``quarantined``. On ``merged`` the returned record is the KEPT record
    (its id predates this call) — a caller tracking lineage must alias its
    occurrence to that id instead of assuming a fresh record.
    ``occurrence_id`` is fresh per call, so two identical deposits remain
    distinguishable in the caller's own bookkeeping.
    """
    record = await eng.write(content, **kwargs)
    # Safe without a lock: ``write`` sets the action and returns with no
    # await in between, so no other task can run before it is read here.
    action = eng._last_write_action
    return WriteOutcome(record=record, action=action, occurrence_id=new_record_id())


async def write_messages(
    eng: Any,
    messages: Sequence[Mapping[str, str]],
    namespace: str = "default",
    actor: str = "user",
    session_id: str | None = None,
    channel: str = "messages",
    group_id: str | None = None,
    tags: list[str] | None = None,
    valid_from: datetime | None = None,
) -> list[MemoryRecord]:
    """C4: ingest a chat transcript as per-turn **episodic** records.

    ``messages`` is OpenAI-style ``[{"role": ..., "content": ...}]``. Each
    turn becomes one episodic record whose provenance ``role`` is the turn's
    role and whose ``channel`` is ``channel`` (default ``"messages"``);
    passing ``session_id`` (or using :meth:`write_episode`) stamps every turn
    with the same conversation id so they group as one episode. Untrusted
    callers (e.g. REST) pass ``channel="rest"`` so TrustPolicy caps the
    turns' trust regardless of the claimed role (SEC-C1). Turns ride the
    ordinary write door, so the firewall, dedup, and lifecycle all apply.
    Additive over the P0 contract — callers that never use it see no change.

    Event time: a message may carry ``"timestamp"`` (ISO-8601 string or
    ``datetime``); otherwise ``valid_from`` applies to the whole batch;
    otherwise "now". This is what lets "when did X happen" be answered from
    the session date rather than from the ingestion time.

    Reply threads (G27): a message may carry ``"reply_to"``, the record id it
    answers or the index of an earlier message of this call; it is passed to
    :meth:`write` as ``reply_to``.

    Embedding (G9): the turns that will reach the door are embedded up front
    in calls of at most ``embedding.batch_size`` texts, which fills the
    embedding cache; the per-record firewall and vector projection then read
    their vectors from it. If that up-front embedding fails, the call raises
    before any turn is written, so a retry cannot duplicate turns.

    The turns share one projection batch (:meth:`_projection_batch`): the
    lexical index commits and the projector checkpoints happen once per
    call instead of once per turn.

    ``write.intake_v2`` (W2-01): the call goes through the intake gateway first (envelope, write
    ids, receipt, idempotent retry); the gateway runs :func:`write_messages_direct` on the fresh
    turns."""
    if eng._config().write.intake_v2:
        from memspine.ingest import gateway as intake_gateway

        return await intake_gateway.write_messages(
            eng, messages, namespace, actor, session_id, channel, group_id, tags, valid_from
        )
    return await write_messages_direct(
        eng, messages, namespace, actor, session_id, channel, group_id, tags, valid_from
    )


async def write_messages_direct(
    eng: Any,
    messages: Sequence[Mapping[str, str]],
    namespace: str = "default",
    actor: str = "user",
    session_id: str | None = None,
    channel: str = "messages",
    group_id: str | None = None,
    tags: list[str] | None = None,
    valid_from: datetime | None = None,
) -> list[MemoryRecord]:
    """The write path as it was before the intake gateway (``write.intake_v2`` off)."""
    contents = eng._depositable_contents(messages)
    tap.event(
        "write.messages.received",
        turns=len(messages),
        depositable=len(contents),
        session=session_id,
        channel=channel,
    )
    await eng._prewarm_embeddings(contents)
    batch = await eng._prefetch_neighbours(validate_namespace(namespace), contents)
    if batch is None:
        async with eng._projection_batch():
            return await eng._write_turns(
                messages, namespace, actor, session_id, channel, group_id, tags, valid_from
            )
    open_batches = eng._neighbour_batches.setdefault(batch.namespace, [])
    open_batches.append(batch)
    token = _NEIGHBOUR_BATCH.set(batch)
    try:
        async with eng._projection_batch():
            return await eng._write_turns(
                messages, namespace, actor, session_id, channel, group_id, tags, valid_from
            )
    finally:
        _NEIGHBOUR_BATCH.reset(token)
        open_batches.remove(batch)
        if not open_batches:
            del eng._neighbour_batches[batch.namespace]


async def _prefetch_neighbours(
    eng: Any, ns: str, contents: Sequence[str]
) -> _NeighbourBatch | None:
    """#63: the firewall's neighbour query for every turn, as one batched
    vector search (in chunks of ``embedding.batch_size``) before the first
    write. None (per-turn queries) below two distinct contents, with the
    firewall off, or when the vector store has no batched query."""
    query_many = getattr(eng._vector, "query_many", None)
    unique = list(dict.fromkeys(contents))
    if (
        len(unique) < 2
        or eng._embedder is None
        or not callable(query_many)
        or not eng._config().firewall.enabled
    ):
        return None
    prefetched: dict[str, list[VectorHit]] = {}
    # Enough rows that the nearest non-held ones are always among them (rows
    # held later in the batch are written ones, which are scored separately).
    top_k = constants.ANOMALY_MIN_NEIGHBOURS + await eng._require_started().count_quarantined(ns)
    size = eng._config().embedding.batch_size
    for start in range(0, len(unique), size):
        chunk = unique[start : start + size]
        vectors = await eng._embedder.embed(chunk)
        results = await query_many(
            ns,
            vectors,
            embedder_id=eng._embedder.embedder_id,
            top_k=top_k,
        )
        prefetched.update(zip(chunk, results, strict=True))
    return _NeighbourBatch(ns, prefetched)


async def _write_turns(
    eng: Any,
    messages: Sequence[Mapping[str, str]],
    namespace: str,
    actor: str,
    session_id: str | None,
    channel: str,
    group_id: str | None,
    tags: list[str] | None,
    valid_from: datetime | None,
) -> list[MemoryRecord]:
    """The per-turn loop of :meth:`write_messages`: each turn goes through
    the write door on its own, with its own firewall and ladder decisions."""
    records: list[MemoryRecord] = []
    written_at: dict[int, str] = {}  # G27: message index -> its record id
    ref_cfg = eng._config().read  # A07
    prev_turn: tuple[str, str] | None = None  # (record id, speaker) of the last turn written
    fw = eng._config().firewall
    if eng._perspective_options().on:
        # I39: the call's participants are known before its first turn is annotated
        state = await eng._persp_state_for(validate_namespace(namespace))
        for turn in messages:
            if isinstance(turn, Mapping):
                who = turn.get("speaker") or turn.get("name")
                if isinstance(who, str) and who.strip():
                    state["known"].add(who.strip().lower())
    session_speakers = (  # I53: everyone present in this call, in order of first turn
        list(dict.fromkeys(_speaker_of(t) for t in messages if isinstance(t, Mapping)))
        if eng._config().write.participants == "session"
        else []
    )
    conv_stamper = None
    if eng._config().write.stamping_v2:  # W2-02
        conv_stamper = ConversationStamper(
            namespace=validate_namespace(namespace),
            source_id=session_id or "nosession",
            default_tz=eng._config().write.stamp_default_tz or eng._config().read.naive_timezone,
            session_speakers=tuple(
                participants_of_call([t for t in messages if isinstance(t, Mapping)])
            ),
        )
    for i, turn in enumerate(messages):
        try:
            role = turn["role"]
            content = _turn_content(turn)
        except (KeyError, TypeError) as exc:
            raise ValueError(
                f"messages[{i}] must be a mapping with 'role' and 'content' keys"
            ) from exc
        reply_to = _reply_target(turn, i, written_at)
        if role in fw.skip_message_roles:
            tap.event("write.turn.skipped", reason="role", role=role, turn=i)
            continue  # H21: system/tool text is not memory
        if fw.skip_injected_recall and _looks_like_recall(content):
            # H21: recalled memory echoed back is not new evidence. R2-11:
            # never silently: a MARKER event traces what was dropped (by
            # fingerprint, not content) so a genuine turn that merely
            # quotes a marker can be found and re-ingested.
            _log.info("memory.skip_injected_recall", namespace=namespace)
            tap.event("write.turn.skipped", reason="injected_recall", turn=i)
            eng._require_started()
            await eng._append_and_project(
                MemoryEvent(
                    kind=EventKind.MARKER,
                    namespace=validate_namespace(namespace),
                    actor=actor,
                    payload={
                        "marker": "recall_skipped",
                        "role": role,
                        "session_id": session_id,
                        "message_index": i,
                        "content_fingerprint": fingerprint_payload({"content": content}),
                        "chars": len(content),
                    },
                )
            )
            continue
        turn_tags = list(tags or [])
        if eng._perspective_options().on:  # I39: explicit participants ride as tags
            who = turn.get("speaker") or turn.get("name")
            to = turn.get("addressee")
            if isinstance(who, str) and who.strip():
                turn_tags.append(f"spk:{who.strip().lower()}")
            if isinstance(to, str) and to.strip():
                turn_tags.append(f"addr:{to.strip().lower()}")
        if fw.tag_assistant_claims and role == "assistant":
            turn_tags.append("assistant_claim")
        if role == "assistant" and eng._config().read.role_aware and is_recommendation(content):
            turn_tags.append(RECOMMENDATION_TAG)  # W11: the recommendation ledger
        if (
            role == "user"
            and eng._memory_policy(eng._config(), "episodic").get("forget_detector")
            and is_forget_request(content)
        ):
            # G25: tagged and listed by forget_requests(); never deleted on a regex.
            turn_tags.append(constants.FORGET_REQUEST_TAG)
        correction = (
            detect_correction(content)
            if role == "user"
            and eng._memory_policy(eng._config(), "episodic").get("correction_detector")
            else None
        )
        if correction is not None:
            turn_tags.append(constants.CORRECTION_TAG)  # W17d
        if "_idem" in turn:  # W2-01: the intake write id rides as a tag (restart recovery)
            turn_tags.append(f"{IDEM_TAG_PREFIX}{turn['_idem']}")
        asset_refs = eng._attachment_refs(turn, i, session_id)
        for _ref, aid in asset_refs:
            turn_tags.append(f"asset:{aid}")  # E04: ingest.assets
        if ref_cfg.reference_edges == "on" and reply_to is None and prev_turn is not None:
            # A07: a short reply / deictic turn answers the turn right before it
            ref_kind = antecedent_kind(
                content,
                _speaker_of(turn),
                prev_turn[1],
                max_words=ref_cfg.reference_short_words,
            )
            if ref_kind is not None:
                reply_to = prev_turn[0]
                turn_tags.append(f"{REF_PREFIX}{ref_kind}")
        stamp = eng._event_time(turn.get("timestamp")) or valid_from
        if stamp is not None and session_id and eng._config().read.session_sequence:
            stamp = eng._sequenced(namespace, session_id, eng._localize(stamp))
        pending = None
        if conv_stamper is not None:  # W2-02: three clocks, axes, participants, provenance class
            pending = _stamp_turn(eng, conv_stamper, i, turn, role, content, bool(session_speakers))
        pending_token = PENDING_STAMP.set(pending) if pending is not None else None
        try:
            record = await eng.write(
                content,
                namespace=namespace,
                memory_type="episodic",
                source=SourceInfo(role=role, channel=channel, message_id=session_id),
                actor=actor,
                group_id=group_id,
                tags=turn_tags or None,
                valid_from=stamp,
                reply_to=reply_to,
                participants=(
                    [_speaker_of(turn), *session_speakers] if session_speakers else None
                ),
                visibility=(
                    str(turn["visibility"]) if session_speakers and "visibility" in turn else None
                ),
            )
        finally:
            if pending_token is not None:
                PENDING_STAMP.reset(pending_token)
        if conv_stamper is not None:
            conv_stamper.clear_proposal()
            if role == "assistant" and not record.quarantined:
                conv_stamper.note_assistant(record.record_id, content)
        if asset_refs:
            eng._register_assets(asset_refs, turn, i, session_id, record.record_id)
        if correction is not None and not record.quarantined:
            await eng._apply_correction(record.namespace, record, correction, records)
        records.append(record)
        written_at[i] = record.record_id
        if "_idem" in turn:  # W2-01: tell the intake gateway which record this write id became
            sink = INTAKE_LANDED.get()
            if sink is not None:
                sink[str(turn["_idem"])] = record.record_id
        if not record.quarantined:
            prev_turn = (record.record_id, _speaker_of(turn))
    return records


def _stamp_turn(
    eng: Any,
    conv: ConversationStamper,
    index: int,
    turn: Mapping[str, Any],
    role: str,
    content: str,
    session_tags_on: bool,
) -> PendingStamp:
    """W2-02: stamp one raw turn (taps ``write.stamp.*``) and run the assent check of a user turn
    against the assistant turn it answers."""
    wcfg = eng._config().write
    pending = conv.turn(index, turn, content, recorded_at=datetime.now(UTC))
    st = pending.stamped
    extra: list[str] = []
    verdict, proposal_id = conv.assent_for(role, content)
    if verdict is not None:
        if tap.enabled():
            tap.event(
                "write.stamp.assent",
                turn=index,
                kind=verdict.kind,
                binds=verdict.binds,
                matched=list(verdict.matched),
                reasons=list(verdict.reasons),
                proposal=proposal_id,
            )
        if verdict.confirms and proposal_id is not None:
            extra.append(f"{ASSENT_TAG_PREFIX}{proposal_id}")
    if wcfg.stamp_participant_tags == "v1" and not session_tags_on:
        extra.extend(st.participant_tags)  # the v1 tag, kept for the dual-read period
    if tap.enabled():
        tap.event(
            "write.stamp.turn",
            turn=index,
            role=role,
            src_class=st.src_class,
            spk=st.spk,
            addr=st.addr,
            participants=len(st.participants),
            source_tz=st.source_tz,
            tz_unknown=st.tz_unknown,
            precision=st.precision,
            dated=st.source_at is not None,
            flags=list(st.flags),
        )
    return PendingStamp(st, tuple(extra))


def _localize(eng: Any, value: datetime) -> datetime:
    """F5: an aware datetime is returned as is; a naive one is read in
    ``read.naive_timezone`` (default UTC) and a warning is logged once per engine."""
    if value.tzinfo is not None:
        return value
    name = eng._config().read.naive_timezone
    tap.event("write.time.parsed", anchored=True, unknown_tz=True, assumed=name)
    if not eng._naive_warned:
        eng._naive_warned = True
        _log.warning(
            "write.naive_datetime",
            assumed_timezone=name,
            hint="pass an aware datetime or set read.naive_timezone to the source's zone",
        )
    return value.replace(tzinfo=_zone_of(name))


def _event_time(eng: Any, value: object) -> datetime | None:
    """F5: an ISO-8601 string or datetime -> aware datetime (naive ones via
    :meth:`_localize`); None when absent or unparseable."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return eng._localize(value)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return eng._localize(parsed)


def _sequenced(eng: Any, namespace: str, session_id: str, stamp: datetime) -> datetime:
    """F5 (``read.session_sequence``): the n-th write of one session at the same event
    time is stored ``n`` microseconds later, so ties keep write order."""
    key = (namespace, session_id, stamp)
    tick = eng._session_ticks.get(key, 0)
    eng._session_ticks[key] = tick + 1
    return stamp + timedelta(microseconds=tick)


def _depositable_contents(eng: Any, messages: Sequence[Mapping[str, str]]) -> list[str]:
    """The contents ``write_messages`` will send through the door, as the
    firewall will see them (role/recall skips applied, secrets redacted).

    Malformed turns are left out here; the write loop raises on them exactly
    as it did before batching existed."""
    fw = eng._config().firewall
    contents: list[str] = []
    for turn in messages:
        try:
            role = turn["role"]
            content = _turn_content(turn)
        except (KeyError, TypeError):
            continue
        if role in fw.skip_message_roles:
            continue
        if fw.skip_injected_recall and _looks_like_recall(content):
            continue
        contents.append(_screen_text(content, fw)[0])
    return contents


async def _prewarm_embeddings(eng: Any, texts: Sequence[str]) -> None:
    """G9: embed a batch of contents in chunked calls so the per-record
    write path finds every vector in the embedding cache (E3).

    Below two distinct texts this does nothing, so a single write makes
    the same embedding calls it always has."""
    if eng._embedder is None or eng._vector is None:
        return
    unique = list(dict.fromkeys(texts))
    if len(unique) < 2:
        return
    size = eng._config().embedding.batch_size
    for start in range(0, len(unique), size):
        await eng._embedder.embed(unique[start : start + size])


async def write_episode(
    eng: Any,
    messages: Sequence[Mapping[str, str]],
    namespace: str = "default",
    actor: str = "user",
    channel: str = "messages",
    tags: list[str] | None = None,
) -> list[MemoryRecord]:
    """C4: ingest a transcript as ONE conversation. Like
    :meth:`write_messages`, but stamps every turn with a shared,
    content-derived session id (``message_id``) AND the same ``group_id``
    (D2), so the turns are retrievable and consolidatable as a single
    episode — the M13.2 session detector then treats them as one session at
    consolidation time."""
    session_id = fingerprint_payload({"episode": [str(turn) for turn in messages]})
    return await eng.write_messages(
        messages,
        namespace=namespace,
        actor=actor,
        session_id=session_id,
        channel=channel,
        group_id=session_id,
        tags=tags,
    )


async def _write_locked(
    eng: Any,
    storage: SqlStorage,
    ns: str,
    record: MemoryRecord,
    memory_type: str,
    actor: str,
    trust_cap: list[float] | None = None,
) -> MemoryRecord:
    written, _ = await eng._write_locked_ex(storage, ns, record, memory_type, actor, trust_cap)
    return written


async def _write_locked_ex(
    eng: Any,
    storage: SqlStorage,
    ns: str,
    record: MemoryRecord,
    memory_type: str,
    actor: str,
    trust_cap: list[float] | None = None,
) -> tuple[MemoryRecord, str]:
    record = await _stamp_derived_times(storage, record)
    record, verdict = await eng._screen_write(record, trust_cap)
    if verdict.quarantine:
        # Quarantined content is stored inert: no dedup merging, no
        # conflict-ladder participation, no retrieval surface — but the
        # write IS recorded (audit + later corroboration need it).
        await eng._append_and_project(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace=ns,
                actor=actor,
                payload={
                    "record": record.model_dump(mode="json"),
                    "firewall": {"reasons": verdict.reasons},
                },
            )
        )
        _log.warning(
            "memory.quarantined",
            namespace=ns,
            record_id=record.record_id,
            reasons=verdict.reasons,
        )
        tap.event("write.quarantine", id=record.record_id, reasons=_codes(verdict.reasons))
        if (
            eng._procedural is not None
            and record.attribute != "lesson"
            and eng._procedural_policy().get("quarantine_lesson")
        ):
            await eng._quarantine_lesson(storage, ns, record, verdict.reasons)  # N24
        _tap_deposit(record, memory_type, "quarantined")
        return record, "quarantined"
    written, action = await eng._write_screened(storage, ns, record, memory_type, actor)
    _tap_deposit(written, memory_type, action)
    return written, action


async def _stamp_derived_times(storage: SqlStorage, record: MemoryRecord) -> MemoryRecord:
    """F-02b: a derived record inherits ``source_at`` from its earliest dated parent; window-derived
    records say their event time is the window start (basis ``inferred``). Fills only unset."""
    if record.source_at is not None or not record.source.parents:
        return record
    found = [await storage.get_record(p) for p in record.source.parents]
    parents = [r for r in found if r is not None]
    update = {
        k: v
        for k, v in derived_times(
            channel=record.source.channel,
            tags=record.tags,
            valid_from=record.valid_from,
            parents=parents,
        ).items()
        if k not in record.model_fields_set
    }
    return record.model_copy(update=update) if update else record


def _tap_deposit(record: MemoryRecord, memory_type: str, action: str) -> None:
    """WT-02: the door's outcome (added / merged / updated / superseded / rejected / quarantined)."""
    if not tap.enabled():
        return
    tap.event(
        "write.deposit",
        id=record.record_id,
        memory_type=memory_type,
        action=action,
        trust=record.trust,
        parents=len(record.source.parents),
    )
    if action == "rejected":
        tap.event("write.rejected", id=record.record_id, reason="write_door")


async def _screen_derived(
    eng: Any, record: MemoryRecord, trust_cap: list[float]
) -> tuple[MemoryRecord, list[str]]:
    """N2: the write-door firewall for derived content written outside the door.

    ``extract_graph`` and the C3 write pipeline append their own WRITE events
    (their provenance and ladder semantics differ from a caller write), but
    the record passes the same screening first: redaction, trust matrix,
    instruction/anomaly/size/protected-key checks, the parent trust cap and
    principal reputation. Returns the stamped record and, when it is
    quarantined, the firewall reasons (empty otherwise).
    """
    screened, verdict = await eng._screen_write(record, trust_cap)
    return screened, (verdict.reasons if verdict.quarantine else [])


async def _screen_write(
    eng: Any, record: MemoryRecord, trust_cap: list[float] | None
) -> tuple[MemoryRecord, FirewallVerdict]:
    """The firewall half of the write door: the stamped record and its verdict."""
    fw = eng._config().firewall
    record = eng._redact_fields(record, fw)
    record = await eng._govern_write(record)  # I48 / I52 / I53 (all off by default)
    # Memory Firewall gate (E1/M17): every write of every type passes the
    # deterministic trust/anomaly/instruction assessment BEFORE the door.
    if fw.enabled:
        verdict = await eng._assess_write(record)
        privileged = record.source.role in ("operator", "system")
        key = f"{record.entity}.{record.attribute}" if record.entity else None
        extra: list[str] = []
        if (
            fw.max_content_chars is not None
            and len(record.content) > fw.max_content_chars
            and not privileged
        ):
            extra.append(f"size_anomaly({len(record.content)}>{fw.max_content_chars})")
        if (
            fw.protected_keys
            and not privileged
            and (record.entity in fw.protected_keys or key in fw.protected_keys)
        ):
            extra.append(f"protected_key({key or record.entity})")
        if extra:
            verdict = FirewallVerdict(
                trust=verdict.trust,
                instruction_flag=verdict.instruction_flag,
                anomalous=True,
                quarantine=True,
                reasons=[*verdict.reasons, *extra, "quarantined"],
            )
    else:
        # N1 ablation arm: trust scoring only — no flags, anomaly or quarantine.
        verdict = FirewallVerdict(trust=eng._firewall.policy.trust_at_write(record.source))
    if eng._config().consent.v2:
        # T5-03: may_store. A write the owner has not opted in to (a sensitive or special-category
        # tier with no grant, or a revoked purpose) is held inert, like any other quarantined write.
        await trust_consent.ensure_registry(eng)
        denied = trust_consent.store_denial(eng, record)
        if denied is not None:
            verdict = _held(verdict, [f"consent:{denied}"])
    hold = GATE_HOLD.get()
    if hold is not None and hold[0] == record.record_id:
        # W2-04 gate 3 / T5-03 may_derive: a derived record the gates refused
        verdict = _held(verdict, [f"gate3:{reason}" for reason in hold[1]])
    record = verdict.apply(record)
    if tap.enabled():
        tap.event(
            "write.firewall.verdict",
            id=record.record_id,
            firewall=fw.enabled,
            trust=verdict.trust,
            quarantine=verdict.quarantine,
            anomalous=verdict.anomalous,
            instruction_flag=verdict.instruction_flag,
            reasons=_codes(verdict.reasons),
        )
    hearsay_cap = fw.hearsay_trust_cap
    if (
        hearsay_cap is not None
        and record.trust > hearsay_cap
        and any(t.startswith("rep:") for t in record.tags)
    ):
        # I43: second-hand ("my mom said X") never outranks first-hand; the parent
        # cap below carries it to everything derived from the record.
        record = record.model_copy(update={"trust": hearsay_cap})
    if trust_cap is not None:
        # MTI-D (integrity.enabled): after the firewall has set base trust,
        # never exceed the least-trusted parent's view. Quarantine stays the
        # firewall's decision; admission (read side) enforces the radius.
        capped = eng._integrity().deposit_trust(record.trust, trust_cap)
        record = record.model_copy(update={"trust": capped})
    integrity_cfg = eng._config().integrity
    if integrity_cfg.enabled and integrity_cfg.principal_reputation and record.source.principal:
        factor = await eng.principal_reputation(record.source.principal)
        if factor < 1.0:
            record = record.model_copy(update={"trust": record.trust * factor})
    if INFERRED_TAG in record.tags:  # I48: an inference is never as trusted as a statement
        cap = eng._config().write.inferred_trust_cap
        if record.trust > cap:
            record = record.model_copy(update={"trust": cap})
    return record, verdict


def _held(verdict: FirewallVerdict, reasons: list[str]) -> FirewallVerdict:
    """``verdict`` with ``reasons`` added and the write quarantined (stored inert, never served)."""
    kept = [r for r in verdict.reasons if r != "quarantined"]
    return FirewallVerdict(
        trust=verdict.trust,
        instruction_flag=verdict.instruction_flag,
        anomalous=verdict.anomalous,
        quarantine=True,
        reasons=[*kept, *reasons, "quarantined"],
    )


def _redact_fields(eng: Any, record: MemoryRecord, fw: FirewallConfig) -> MemoryRecord:
    """B8 secrets + #44 PII pack over every text field, and the PII tier.

    Content, entity, attribute and tags are masked when ``redact_secrets`` or
    ``pii: redact`` is on. ``pii: tag`` leaves the text and adds a
    ``pii:<kind>`` tag per kind found, raising ``pii_tier`` to at least
    ``high``. A record written with no tier takes its memory type's
    ``pii_default_tier`` policy, when one is configured."""
    update: dict[str, object] = {}
    kinds: list[str] = []
    if fw.redact_secrets or fw.pii == "redact":
        content, found = _screen_text(record.content, fw)
        if found:
            update["content"] = content
            update["content_fingerprint"] = fingerprint_payload({"content": content})
            kinds.extend(found)
        for name in ("entity", "attribute"):
            value = getattr(record, name)
            if value:
                cleaned, found = _screen_text(value, fw)
                if found:
                    update[name] = cleaned
                    kinds.extend(found)
        tags: list[str] = []
        for tag in record.tags:
            cleaned, found = _screen_text(tag, fw)
            tags.append(cleaned)
            kinds.extend(found)
        if tags != record.tags:
            update["tags"] = tags
    if kinds:
        _log.warning(
            "memory.redacted", namespace=record.namespace, kinds=list(dict.fromkeys(kinds))
        )
        record = record.model_copy(update=update)
    update = {}
    tier = record.pii_tier
    if fw.pii == "tag":
        fields = [record.content, record.entity or "", record.attribute or "", *record.tags]
        found_pii = list(
            dict.fromkeys(k for text in fields for k in find_pii(text, extended=fw.pii_extended))
        )
        if found_pii:
            marks = [f"pii:{kind}" for kind in found_pii]
            update["tags"] = [*record.tags, *(m for m in marks if m not in record.tags)]
            tier = max(tier, PiiTier.HIGH, key=_PII_RANK.__getitem__)
            _log.warning("memory.pii_tagged", namespace=record.namespace, kinds=found_pii)
    if fw.sensitive_topics:
        # W16 (plan v3.2): GDPR art. 9-style topics are tagged, not masked; the
        # tier raise brings consent / purpose / remote-LLM rules to bear on them.
        topics = sensitive_topics(record.content)
        if topics:
            tagged = update.get("tags")
            current = list(tagged) if isinstance(tagged, list) else list(record.tags)
            marks = [f"sensitive:{topic}" for topic in topics]
            update["tags"] = [*current, *(m for m in marks if m not in current)]
            tier = max(tier, PiiTier.HIGH, key=_PII_RANK.__getitem__)
            _log.info("memory.sensitive_tagged", namespace=record.namespace, topics=topics)
    if tier is PiiTier.NONE:
        default = eng._memory_policy(eng._config(), record.memory_type).get("pii_default_tier")
        if default:
            tier = PiiTier(str(default))
    if tier is not record.pii_tier:
        update["pii_tier"] = tier
    return record.model_copy(update=update) if update else record


async def _grade_sensitivity(eng: Any, content: str, mode: str) -> tuple[str, tuple[str, ...]]:
    """I52: the (grade, categories) of ``content``. The lexicon grades it; under
    ``decider`` the ``sensitivity`` task may raise an ungraded text to ``medium`` (category
    ``other``) when it is sure. It never lowers a grade, and a failure keeps the lexicon."""
    label = grade_text(content)
    if mode != "decider" or label.grade != "none":
        return label.grade, label.categories
    read = eng._config().read
    try:
        decision = await eng._decider_adapter().decide("sensitivity", content[:2000], None)
        if (
            decision.label == "sensitive"
            and decision.confidence is not None
            and decision.confidence >= read.decider_min_confidence
        ):
            return "medium", ("other",)
    except Exception as exc:  # an enhancer, never a gate; log the class, not the text
        _log.warning("write.sensitivity_decider_failed", error=type(exc).__name__)
    return label.grade, label.categories


async def _govern_write(eng: Any, record: MemoryRecord) -> MemoryRecord:
    """I48 / I52 / I53: the write-time labels. Every key is off by default (unchanged)."""
    wc = eng._config().write
    if wc.sensitivity == "off" and wc.participants == "off" and wc.inferred == "off":
        return record
    tags = list(record.tags)
    update: dict[str, Any] = {}
    if wc.sensitivity != "off" and not any(t.startswith(GRADE_TAG_PREFIX) for t in tags):
        grade, categories = await eng._grade_sensitivity(record.content, wc.sensitivity)
        if grade != "none":
            tags.extend(t for t in label_tags((grade, categories)) if t not in tags)
            if grade == "high" and _PII_RANK[record.pii_tier] < _PII_RANK[PiiTier.HIGH]:
                update["pii_tier"] = PiiTier.HIGH
            _log.info(  # label and category only, never the text
                "memory.sensitivity_graded",
                namespace=record.namespace,
                grade=grade,
                categories=list(categories),
            )
    parents: list[MemoryRecord] = []
    if record.source.parents and (wc.participants != "off" or wc.inferred != "off"):
        storage = eng._require_started()
        got = [await storage.get_record(p) for p in record.source.parents]
        parents = [p for p in got if p is not None and p.namespace == record.namespace]
    if wc.participants != "off" and parents:
        has_people = any(t.startswith(PARTICIPANT_PREFIX) for t in tags)
        has_vis = any(t.startswith(VISIBILITY_PREFIX) for t in tags)
        for tag in inherit_tags([p.tags for p in parents]):
            is_person = tag.startswith(PARTICIPANT_PREFIX)
            if tag not in tags and not (has_people if is_person else has_vis):
                tags.append(tag)
    if wc.inferred != "off" and INFERRED_TAG not in tags:
        marked = eng._mark_inferred(record, parents, wc)
        if marked is not None:
            tags.extend(t for t in marked if t not in tags)
            _log.info("memory.inferred_tagged", namespace=record.namespace)
    if tags != record.tags:
        update["tags"] = tags
    return record.model_copy(update=update) if update else record


def _mark_inferred(
    record: MemoryRecord, parents: Sequence[MemoryRecord], wc: Any
) -> list[str] | None:
    """I48: the tags of an engine-derived record that states something no user turn
    does, else None (a stated or non-derived record is left alone)."""
    derived = record.source.channel in INFERRED_CHANNELS or (
        record.source.role == "assistant" and bool(parents)
    )
    if not derived or record.memory_type == "episodic":
        return None
    inferred, support = classify(
        record.content,
        [(p.record_id, p.source.role, p.content) for p in parents],
        explicit_overlap=wc.inferred_explicit_overlap,
        support_overlap=wc.inferred_support_overlap,
    )
    return [INFERRED_TAG, *support_tags(support)] if inferred else None


async def review_inferred(
    eng: Any, namespace: str = "default", *, include_supported: bool = False
) -> list[MemoryRecord]:
    """I48: the review list. Live records tagged ``src:inferred`` that have fewer than
    ``read.inferred_min_support`` distinct supporting user turns (all inferred records
    with ``include_supported``), lowest support first."""
    ns = validate_namespace(namespace)
    need = eng._config().read.inferred_min_support
    out = [
        r
        for r in await eng._records(ns)
        if is_inferred(r.tags)
        and r.status is RecordStatus.ACTIVATED
        and not r.quarantined
        and (include_supported or support_count(r.tags) < need)
    ]
    return sorted(out, key=lambda r: (support_count(r.tags), r.recorded_at))


async def _sensitivity_scope_open(eng: Any, query: str) -> bool:
    """I52 (``read.sensitivity_gate: decider``): is the message about the user's own
    sensitive details? Only a sure ``about`` answer opens the gate; anything else keeps it."""
    if eng._config().read.decider == "heuristic":
        return False
    return await eng._decided("sensitivity_scope", query, None, "not_about") == "about"


async def _scope_check(eng: Any, query: str) -> Callable[[MemoryRecord], bool] | None:
    """I48 / I52: the per-record read predicate for ``query``, or None when both gates are
    off. Sensitive records need a topic, subject or word match with the question
    (:func:`~memspine.core.sensitivity.passes_gate`); inferred ones need their support."""
    read = eng._config().read
    sens = read.sensitivity_gate != "off"
    infer = read.inferred_gate != "off"
    if not (sens or infer):
        return None
    topics = query_topics(query) if sens else frozenset()
    opened = (
        sens
        and read.sensitivity_gate == "decider"
        and not topics
        and await eng._sensitivity_scope_open(query)
    )
    need = read.inferred_min_support

    def check(record: MemoryRecord) -> bool:
        if sens and not opened:
            label = label_of(record.tags)
            if grade_rank(label.grade) >= 2 and not passes_gate(
                label, record.content, record.entity, query, topics
            ):
                return False
        return not (infer and is_inferred(record.tags) and support_count(record.tags) < need)

    return check


async def _write_screened(
    eng: Any,
    storage: SqlStorage,
    ns: str,
    record: MemoryRecord,
    memory_type: str,
    actor: str,
) -> tuple[MemoryRecord, str]:
    """The door after an admitting firewall verdict: cue, semantic or plain WRITE."""
    if tap.enabled():
        route = (
            "cue"
            if CUE_TAG in record.tags
            else "semantic_pipeline"
            if memory_type == "semantic" and eng._semantic is not None
            else "plain"
        )
        tap.event("write.route", id=record.record_id, memory_type=memory_type, route=route)
    if CUE_TAG in record.tags:
        # R2-1: a cue is a retrieval key, not a fact. It skips dedup, entity
        # extraction and the M4 ladder (an extracted key would let the cue
        # archive the very fact it points at), and it corroborates nothing.
        await eng._append_and_project(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace=ns,
                actor=actor,
                payload={"record": record.model_dump(mode="json")},
            )
        )
        _log.info(EVENT_WRITE, namespace=ns, record_id=record.record_id, cue=True)
        return record, "added"

    # Semantic writes run the full M5/M4 pipeline (dedup → entities →
    # conflict); every other type is a plain WRITE through the door.
    if memory_type == "semantic" and eng._semantic is not None:
        result = await eng._semantic.write(record)
        _log.info(
            EVENT_WRITE,
            namespace=ns,
            record_id=result.record.record_id,
            action=result.action,
        )
        await eng._corroborate(ns, result.record)
        await eng._evolve_links(ns, result.record)
        return result.record, result.action

    event = MemoryEvent(
        kind=EventKind.WRITE,
        namespace=ns,
        actor=actor,
        payload={"record": record.model_dump(mode="json")},
    )
    await eng._append_and_project(event)
    _log.info(EVENT_WRITE, namespace=ns, record_id=record.record_id)
    await eng._corroborate(ns, record)
    await eng._evolve_links(ns, record)

    # Working memory keeps its hot window bounded (M13.1): overflow pages
    # out to episodic via DECAY_TRANSITION events through the same door.
    if memory_type == "working" and eng._working is not None:
        active = await storage.list_records(ns, "working")
        await eng._working.enforce(ns, active)
    return record, "added"


async def correct(
    eng: Any,
    target: str | tuple[str, str],
    new_value: str,
    *,
    actor: str = "user",
    reason: str = "",
    namespace: str = "default",
    source: SourceInfo | None = None,
    tags: Sequence[str] | None = None,
) -> MemoryRecord:
    """#47 user-direct correction (rectification): supersede a live record.

    ``target`` is a record id or an ``(entity, attribute)`` fact key (its current
    fact). The old record is archived with ``evolve_to`` pointing at the new one,
    and the new record's WRITE event carries ``correction: {supersedes, actor,
    reason}``. Unlike :meth:`write`, the conflict ladder does not run: an
    explicit correction is never CONTESTed or rejected as lower-trust
    (``contest_lower_trust``). The firewall still screens the new value
    (redaction, instruction flags); a quarantined correction supersedes nothing.
    Disputes on the key are cleared. Returns the new record.

    ``tags`` (I65) are added to the new record; a ``src:*`` tag among them replaces
    the old record's ``src:*`` tags (``memory_confirm`` flips ``src:assistant-proposed``
    to ``src:user-confirmed``). Engine-reserved tags are dropped.
    """
    storage = eng._require_started()
    ns = validate_namespace(namespace)
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        if isinstance(target, tuple):
            entity, attribute = target
            existing = await storage.find_active_fact(ns, entity, attribute)
            if existing is None:
                raise ConflictError(f"no current fact {entity}.{attribute} in namespace {ns!r}")
        else:
            existing = await storage.get_record(target)
            if existing is None or existing.namespace != ns:
                raise ConflictError(f"no such record {target!r} in namespace {ns!r}")
        if existing.status is not RecordStatus.ACTIVATED or existing.quarantined:
            raise ConflictError(f"record {existing.record_id} is not live — nothing to correct")
        if existing.memory_type == "shared":
            raise ConflictError("memory_type 'shared' is engine-internal — use grant()")
        now = eng._clock()
        src = source or SourceInfo(role=actor, channel="correction")
        incoming = MemoryRecord(
            namespace=ns,
            memory_type=existing.memory_type,
            content=new_value,
            source=src,
            entity=existing.entity,
            attribute=existing.attribute,
            group_id=existing.group_id,
            tags=_merge_correction_tags(existing.tags, tags, ns),
            pii_tier=existing.pii_tier,
            consent_tags=list(existing.consent_tags),
            valid_from=now,
            recorded_at=now,
        )
        incoming, verdict = await eng._screen_write(incoming, None)
        correction = {"supersedes": existing.record_id, "actor": actor, "reason": reason}
        if verdict.quarantine:
            await eng._append_and_project(
                MemoryEvent(
                    kind=EventKind.WRITE,
                    namespace=ns,
                    actor=actor,
                    payload={
                        "record": incoming.model_dump(mode="json"),
                        "firewall": {"reasons": verdict.reasons},
                        "correction": correction,
                    },
                )
            )
            _log.warning(
                "memory.correction_quarantined",
                namespace=ns,
                record_id=incoming.record_id,
                reasons=verdict.reasons,
            )
            return incoming
        closed = existing.model_copy(
            update={
                "valid_to": now,
                "superseded_at": now,
                "status": RecordStatus.ARCHIVED,
                "evolve_to": incoming.record_id,
                "tags": [t for t in existing.tags if t != "disputed"],
            }
        )
        await eng._append_and_project(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace=ns,
                actor=actor,
                payload={"record": closed.model_dump(mode="json"), "correction": correction},
            )
        )
        await eng._append_and_project(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace=ns,
                actor=actor,
                payload={"record": incoming.model_dump(mode="json"), "correction": correction},
            )
        )
        if eng._semantic is not None and existing.memory_type == "semantic":
            await eng._semantic._clear_dispute(existing, keep={existing.record_id})
            eng._semantic.invalidate_index(ns)
    _log.info(
        EVENT_WRITE,
        namespace=ns,
        record_id=incoming.record_id,
        corrected=existing.record_id,
    )
    await eng._audit_action(
        "correct",
        ns,
        [existing.record_id, incoming.record_id],
        actor=actor,
        reason=reason or None,
    )
    return incoming


async def _apply_correction(
    eng: Any,
    ns: str,
    turn: MemoryRecord,
    correction: Correction,
    recent: Sequence[MemoryRecord],
) -> MemoryRecord | None:
    """W17d: act on a detected user correction.

    The target is the live keyed semantic fact with the highest word overlap
    on the negated span (or, without one, on the latest assistant turn within
    ``CORRECTION_LOOKBACK_TURNS``), when it reaches ``CORRECTION_MIN_OVERLAP``.
    With a replacement the fact is superseded on its key (the M4 ladder);
    without one it is retracted. With lessons on, a lesson records the
    correction so a later, different task retrieves it. Returns the new
    record (supersede or retraction) or None when no target qualified."""
    span = correction.negated
    if span is None:
        claims = [r for r in recent[-constants.CORRECTION_LOOKBACK_TURNS :]]
        claims = [r for r in claims if r.source.role == "assistant"]
        span = claims[-1].content if claims else None
    if not span:
        return None
    wanted = content_words(span)
    best: tuple[float, MemoryRecord] | None = None
    for fact in await eng._records(ns, "semantic"):
        if (
            fact.entity is None
            or fact.status is not RecordStatus.ACTIVATED
            or fact.quarantined
            or "retract" in fact.tags
        ):
            continue
        words = content_words(fact.content)
        if not words or not wanted:
            continue
        base = wanted if correction.negated else words
        overlap = len(wanted & words) / len(base)
        if best is None or overlap > best[0]:
            best = (overlap, fact)
    if best is None or best[0] < constants.CORRECTION_MIN_OVERLAP:
        return None
    fact = best[1]
    source = SourceInfo(role="user", channel="correction")
    if correction.replacement:
        content = correction.replacement
        if correction.negated:
            pattern = re.compile(re.escape(correction.negated), re.IGNORECASE)
            if pattern.search(fact.content):
                content = pattern.sub(correction.replacement, fact.content, count=1)
        written = await eng.write(
            content,
            namespace=ns,
            memory_type="semantic",
            source=source,
            entity=fact.entity,
            attribute=fact.attribute,
            group_id=turn.group_id,
            derived_from=[turn.record_id],
        )
    else:
        assert fact.entity is not None
        written = await eng.retract(
            fact.entity,
            fact.attribute or "",
            namespace=ns,
            reason="user correction",
            source=source.model_copy(update={"parents": [turn.record_id]}),
        )
    if eng._procedural is not None and eng._procedural_policy().get("lessons"):
        await eng.record_outcome(
            f"{fact.entity} {fact.attribute or ''}".strip(),
            "failure",
            namespace=ns,
            action=f"stated {fact.content}",
            error="the user corrected it",
            next_action=f"state {correction.replacement}" if correction.replacement else None,
            derived_from=[turn.record_id],
            actor="user",
        )
    return written
