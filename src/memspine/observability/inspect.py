"""Trace Inspector hooks (TI-1): correlation keys, content gating and per-item sleep notes.

Everything here is behind ``observability.inspect`` (default off). Inspect rides on the wiretap
(:mod:`memspine.observability.tap`): it adds events (``store.index.record``, ``write.extract.io``,
``write.window.members``, ``write.envelope.trace``, ``write.integrate.ladder``,
``write.edges.pending`` / ``.consumed``, ``job.stage.item``, ``compose.headers``,
``read.rerank.components``, ``read.planner.io``, ``read.retrieve.writeback``, ``write.turn.bound``)
and one correlation mechanism: :func:`inspect_scope`.

API for call sites:

- ``inspect_enabled()``: one bool test (inspect on AND the tap on); build payloads only behind it.
- ``inspect_scope(**keys)``: context manager; every tap event emitted inside it carries ``keys``
  (``run_id``, ``item``, ``turn_id``, ``query_id``, ``window_id`` ...). A plain null context when
  inspect is off. Scopes nest and merge (inner keys win).
- ``ic(text)``: text for a payload, kept only when ``observability.inspect_content`` is on.
- ``sleep_stage(name)`` / ``sleep_item(...)`` / ``note_event(event)``: per-output-record notes of
  the sleep stages (``job.stage.item``), fed from the one event door.

Off path: ``inspect_enabled()`` is a module bool read plus ``tap.enabled()``; nothing else runs.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from typing import Any

from memspine.observability import tap

__all__ = [
    "configure",
    "digest",
    "edges_consumed",
    "edges_pending",
    "envelope_deposit",
    "envelope_snapshot",
    "envelope_stage_trace",
    "extract_io",
    "headers_event",
    "ic",
    "idsafe",
    "index_record",
    "inspect_content",
    "inspect_enabled",
    "inspect_scope",
    "io_active",
    "io_capture",
    "io_note",
    "io_tokens",
    "ladder_event",
    "note_event",
    "planner_io",
    "rerank_components",
    "retrieve_writeback",
    "sleep_cycle",
    "sleep_item",
    "sleep_stage",
    "turn_bound",
    "window_members",
]

_ON = False
_CONTENT = False

_ITEM_TAGS = ("mined:", "window:", "session:", "integ:")
_STAGE: ContextVar[str | None] = ContextVar("memspine_inspect_stage", default=None)
_ITEM: ContextVar[dict[str, Any] | None] = ContextVar("memspine_inspect_item", default=None)
#: capture slot for the extraction call's rendered prompt and raw reply (see :func:`io_capture`)
_IO: ContextVar[dict[str, Any] | None] = ContextVar("memspine_inspect_io", default=None)
#: the sleep cycle number the running stage belongs to (set by the sleep job, or counted inline)
_CYCLE: ContextVar[int | None] = ContextVar("memspine_inspect_cycle", default=None)
_INLINE_CYCLES: dict[str, int] = {}


def configure(on: bool, content: bool = False) -> None:
    """Set the inspect switches (``Engine.start`` calls it with the config; off by default)."""
    global _ON, _CONTENT
    _ON = bool(on)
    _CONTENT = bool(on and content)


def inspect_enabled() -> bool:
    return _ON and tap.enabled()


def inspect_content() -> bool:
    return _ON and _CONTENT and tap.content_enabled()


def ic(text: object) -> tap.Content:
    """Text for an inspect payload: kept (truncated, redacted by the tap) only under
    ``observability.inspect_content``; otherwise silently absent from the event."""
    if not (_ON and _CONTENT):
        return tap._NO_CONTENT  # the tap's dropped marker: the field is silently absent
    return tap.content(str(text))


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+#=\-]{0,127}")


def idsafe(value: object) -> str | None:
    """``value`` when it is id-shaped (the tap guard accepts it), else its short hash."""
    if value is None:
        return None
    text = str(value)
    return text if _ID.fullmatch(text) else "h:" + digest(text)


def digest(text: object, n: int = 12) -> str:
    """Short stable hash of ``text`` (an id-shaped string that is safe in any event)."""
    return hashlib.sha256(str(text).encode("utf-8", "replace")).hexdigest()[:n]


@contextmanager
def _scope(keys: dict[str, Any]) -> Iterator[None]:
    token = tap._SCOPE.set({**(tap._SCOPE.get() or {}), **keys})
    try:
        yield
    finally:
        tap._SCOPE.reset(token)


def inspect_scope(**keys: Any) -> Any:
    """Merge ``keys`` into every tap event emitted inside the block (``None`` values are skipped).
    A null context when inspect is off."""
    if not inspect_enabled():
        return nullcontext()
    live = {k: v for k, v in keys.items() if v is not None}
    return _scope(live) if live else nullcontext()


# ── extraction I/O capture ────────────────────────────────────────────────────
@contextmanager
def io_capture() -> Iterator[dict[str, Any]]:
    """Collects what the extractor's model call saw and returned (``io_note``) for one window
    call; the caller emits ``write.extract.io`` from it. Yields a throwaway dict when off."""
    slot: dict[str, Any] = {}
    if not inspect_enabled():
        yield slot
        return
    token = _IO.set(slot)
    try:
        yield slot
    finally:
        _IO.reset(token)


def io_active() -> bool:
    return _IO.get() is not None


def io_note(**fields: Any) -> None:
    slot = _IO.get()
    if slot is not None:
        slot.update(fields)


# ── sleep per-item notes (job.stage.item) ─────────────────────────────────────
@contextmanager
def sleep_stage(name: str) -> Iterator[None]:
    """Mark the sleep stage whose events the block emits (adds ``stage`` to its tap events)."""
    if not inspect_enabled():
        yield
        return
    token = _STAGE.set(name)
    try:
        with _scope({"stage": name}):
            yield
    finally:
        _STAGE.reset(token)


@contextmanager
def sleep_cycle(cycle: int | None, namespace: str | None = None) -> Iterator[None]:
    """Mark the sleep cycle whose stages run inside the block (``cycle`` on ``job.stage.item``).
    ``cycle=None`` with a ``namespace``: the inline sleep path, which keeps no cycle table, counts
    its own (one per call) unless an enclosing job already set the cycle."""
    if not inspect_enabled():
        yield
        return
    if cycle is None:
        if namespace is None or _CYCLE.get() is not None:
            yield
            return
        cycle = _INLINE_CYCLES[namespace] = _INLINE_CYCLES.get(namespace, 0) + 1
    token = _CYCLE.set(int(cycle))
    try:
        yield
    finally:
        _CYCLE.reset(token)


def io_tokens(prompt: int, completion: int, estimated: bool) -> None:
    """Add one model call's token counts to the extraction call being captured (a window call that
    re-asks adds up). A no-op outside :func:`io_capture`."""
    slot = _IO.get()
    if slot is None:
        return
    cur = slot.setdefault("tokens", {"prompt": 0, "completion": 0, "estimated": estimated})
    cur["prompt"] += int(prompt)
    cur["completion"] += int(completion)
    cur["estimated"] = bool(cur["estimated"] and estimated)


@contextmanager
def sleep_item(
    session_key: str | None = None, input_ids: Sequence[str] | None = None, **extra: Any
) -> Iterator[None]:
    """One unit of a sleep stage (a session, a community ...): the records the stage writes inside
    the block are reported with this ``session_key`` and ``input_ids``."""
    if not inspect_enabled() or _STAGE.get() is None:
        yield
        return
    info: dict[str, Any] = {"session_key": session_key, "input_ids": list(input_ids or ())}
    info.update(extra)
    token = _ITEM.set(info)
    try:
        yield
    finally:
        _ITEM.reset(token)


def note_event(event: Any, *, rid: str | None = None) -> None:
    """Report what a sleep stage just wrote (``job.stage.item``). Called by the event door for
    every appended event; a no-op outside a sleep stage."""
    stage = _STAGE.get()
    if stage is None or not inspect_enabled():
        return
    kind = getattr(event.kind, "value", str(event.kind))
    payload = getattr(event, "payload", None) or {}
    item = _ITEM.get() or {}
    base: dict[str, Any] = {
        "cycle": _CYCLE.get(),
        "calls": None,
        "tokens": None,
        "status": "ok",
        "stage": stage,
        "event": kind,
        "session_key": item.get("session_key"),
        "input_ids": item.get("input_ids") or [],
        "ns_hash": digest(getattr(event, "namespace", ""), 8),
    }
    if kind == "memory.write":
        record = payload.get("record") or {}
        source = record.get("source") or {}
        tags = [t for t in (record.get("tags") or []) if isinstance(t, str)]
        tap.event(
            "job.stage.item",
            **base,
            output_ids=[record.get("record_id")],
            channel=source.get("channel"),
            memory_type=record.get("memory_type"),
            layer=record.get("layer"),
            parents=list(source.get("parents") or [])[:50],
            n_parents=len(source.get("parents") or []),
            tags=[t for t in tags if t.startswith(_ITEM_TAGS)][:10],
            seq=getattr(event, "seq", None),
        )
    elif kind == "memory.link":
        tap.event(
            "job.stage.item",
            **base,
            src=payload.get("src"),
            dst=payload.get("dst"),
            output_ids=[],
            rel=payload.get("rel"),
            reason=payload.get("reason"),
        )
    elif kind == "memory.decay_transition":
        tap.event(
            "job.stage.item",
            **base,
            output_ids=[payload.get("record_id")],
            transition=payload.get("transition"),
            reason=payload.get("reason"),
        )
    elif kind == "memory.consolidate":
        base["session_key"] = payload.get("session_key") or base["session_key"]
        tap.event(
            "job.stage.item",
            **base,
            output_ids=[payload.get("summary_record_id")],
            members=list(payload.get("member_record_ids") or [])[:50],
            n_members=len(payload.get("member_record_ids") or []),
            superseded=list(payload.get("superseded_summary_ids") or [])[:20],
        )
    elif kind == "memory.marker":
        base["session_key"] = payload.get("session_key") or base["session_key"]
        tap.event(
            "job.stage.item",
            **base,
            marker=payload.get("marker"),
            marker_stage=payload.get("stage"),
            members_fp=payload.get("members_fp"),
        )


# ── projectors (store.index.record) ───────────────────────────────────────────
def index_record(event: Any, projectors: Sequence[Any], embedder_id: str | None = None) -> None:
    """``store.index.record``: what the projectors did with one WRITE (vector / lexical / graph
    membership, the text that was embedded, graph edges added). Called by the event door."""
    payload = getattr(event, "payload", None) or {}
    record = payload.get("record") or {}
    names = [str(getattr(p, "name", "")) for p in projectors]
    index_text = payload.get("index_text")
    prefixed = isinstance(index_text, str) and bool(index_text)
    body = index_text if prefixed else record.get("content", "")
    edges = next((p.last_edges for p in projectors if getattr(p, "name", "") == "graph"), None)
    tap.event(
        "store.index.record",
        record_id=record.get("record_id"),
        seq=getattr(event, "seq", None),
        memory_type=record.get("memory_type"),
        projectors=names,
        vector="vectors" in names,
        lexical=any(n.startswith("lexical") for n in names),
        graph_node="graph" in names,
        edges=edges,
        index_view=prefixed,
        index_text_len=len(body or ""),
        index_text_hash=digest(body or ""),
        embedder=embedder_id,
        index_text=ic(body or ""),
    )


# ── write path ────────────────────────────────────────────────────────────────
def turn_bound(
    *,
    rid: str,
    index: int,
    session: str | None,
    speaker: str | None,
    quarantined: bool,
    wid: str | None = None,
) -> None:
    """``write.turn.bound``: the turn written at message ``index`` of this call became ``rid``.
    The pre-record events (role, echo, stamp, time) of the same turn carry the same correlation
    keys (``turn_id`` from :func:`inspect_scope`) and ``turn=index``; this event is the join."""
    tap.event(
        "write.turn.bound",
        rid=rid,
        record_id=rid,
        turn=index,
        session=idsafe(session),
        speaker=ic(speaker or ""),
        quarantined=quarantined,
        wid=idsafe(wid),
    )


def window_members(
    *,
    window_id: str,
    member_ids: Sequence[str],
    status: str,
    reason: str | None = None,
    cut_reason: str | None = None,
    prepass: str | None = None,
    excluded: Sequence[str] = (),
    batch: str | None = None,
    session: str | None = None,
) -> None:
    """``write.window.members``: who is in a window and what became of it (``planned``,
    ``skipped``, ``failed``, ``cancelled``); emitted for skipped and failed windows too."""
    tap.event(
        "write.window.members",
        window_id=window_id,
        status=status,
        reason=reason,
        cut_reason=cut_reason,
        prepass=prepass or "off",
        member_record_ids=list(member_ids),
        n_members=len(member_ids),
        excluded_rids=list(excluded),
        batch=batch,
        session=idsafe(session),
    )


def extract_io(
    *,
    window_id: str,
    sections: Sequence[str],
    cached: bool,
    prompt_id: str,
    transcript: str,
    member_ids: Sequence[str],
    captured: Mapping[str, Any],
    parsed: Mapping[str, Any] | None,
    failed: str | None = None,
    ms: float | None = None,
) -> None:
    """``write.extract.io``: one extraction call of a window (or of a batch: ``window_id`` is the
    batch id then). The rendered prompt, raw reply and parsed sections ride as content only."""
    counts: dict[str, int] = {}
    for name, value in (parsed or {}).items():
        if isinstance(value, list):
            counts[name] = len(value)
        elif isinstance(value, dict):
            counts[name] = 1
        else:
            counts[name] = 0 if value is None else 1
    tap.event(
        "write.extract.io",
        window_id=window_id,
        sections=list(sections),
        cached=cached,
        failed=failed,
        prompt_id=idsafe(prompt_id),
        model=idsafe(str(prompt_id).split("|")[0]),
        ms=ms,
        tokens=captured.get("tokens"),
        items=counts.get("facts"),
        prompt_hash=captured.get("prompt_hash") or digest(transcript),
        transcript_hash=digest(transcript),
        transcript_chars=len(transcript),
        member_record_ids=list(member_ids)[:200],
        reply_chars=captured.get("reply_chars"),
        reparse=captured.get("reparse"),
        parsed_counts=counts,
        prompt_text=captured.get("prompt") or ic(""),
        reply_text=captured.get("reply") or ic(""),
        parsed_text=ic(json.dumps(parsed, default=str)) if parsed is not None else ic(""),
    )


def _env_dump(env: Any) -> dict[str, Any]:
    return env.model_dump(mode="json")


def envelope_snapshot(envs: Sequence[Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    """``occurrence_id -> (hash, dump)`` of each envelope, taken before a stage runs."""
    out: dict[str, tuple[str, dict[str, Any]]] = {}
    for env in envs:
        dump = _env_dump(env)
        out[env.occurrence_id] = (digest(json.dumps(dump, sort_keys=True, default=str)), dump)
    return out


def _env_summary(env: Any) -> dict[str, Any]:
    event = getattr(env.time, "event", None)
    start = getattr(event, "start", None)
    verdict = getattr(env, "verdict", None)
    extras = getattr(env, "extras", None) or {}
    checks = getattr(env, "checks", None)
    at = getattr(start, "at", None)
    return {
        "kind": idsafe(env.kind),
        "src_class": idsafe(env.src_class),
        "env_status": env.status,
        "grounding": env.grounding,
        "checks": {
            k: v for k, v in (checks.model_dump() if checks else {}).items() if v is not None
        },
        "time": {
            "precision": idsafe(getattr(env.time, "event_precision", None)),
            "basis": idsafe(getattr(start, "basis", None)),
            "at": idsafe(at.isoformat() if at is not None else None),
        },
        "integrate": {
            "op": verdict.op if verdict else None,
            "rung": idsafe(verdict.rung) if verdict else None,
            "target": idsafe(extras.get("integrate_target")),
            "outcome": idsafe(extras.get("integrate_outcome")),
        },
        "resolve": {
            "subject_entity": idsafe(env.subject.entity_id) if env.subject else None,
            "object_entity": idsafe(env.object.entity_id) if env.object else None,
        },
        "evidence_rids": list(
            dict.fromkeys(e.turn.source_id for e in env.evidence if getattr(e, "turn", None))
        )[:20],
        "trust": getattr(env.assessment, "source_trust", None),
        "modality": idsafe(getattr(env.assessment, "modality", None)),
        "text": ic(env.text),
    }


def envelope_stage_trace(
    stage: str,
    window_id: str,
    before: Mapping[str, tuple[str, dict[str, Any]]],
    after: Sequence[Any],
    reasons: Sequence[str] = (),
) -> None:
    """``write.envelope.trace``: one event per envelope per stage. ``status`` is ``kept``,
    ``rewritten`` (with ``fields_changed``), ``added`` or ``dropped`` (with ``reason``: the notes
    the stage counted while it ran)."""
    seen: set[str] = set()
    # "+" joins the notes: the tap guard takes ids (letters, digits, . : / + = -), not commas
    reason = ("+".join(sorted(reasons)) or None) and "+".join(sorted(reasons))[:120]
    for env in after:
        occ = env.occurrence_id
        seen.add(occ)
        dump = _env_dump(env)
        h = digest(json.dumps(dump, sort_keys=True, default=str))
        prev = before.get(occ)
        changed: list[str] = []
        before_hash: str | None = None
        if prev is None:
            status = "added"
        elif prev[0] == h:
            status, before_hash = "kept", prev[0]
        else:
            status, before_hash = "rewritten", prev[0]
            changed = sorted(k for k in set(dump) | set(prev[1]) if dump.get(k) != prev[1].get(k))
        tap.event(
            "write.envelope.trace",
            stage=stage,
            window_id=window_id,
            occ=occ,
            write_id=idsafe(env.write_id),
            status=status,
            before_hash=before_hash,
            after_hash=h,
            fields_changed=changed,
            record_id=env.record_id,
            **_env_summary(env),
        )
    for occ, (h, dump) in before.items():
        if occ in seen:
            continue
        tap.event(
            "write.envelope.trace",
            stage=stage,
            window_id=window_id,
            occ=occ,
            write_id=idsafe(dump.get("write_id")),
            status="dropped",
            reason=reason or "unattributed",
            before_hash=h,
            after_hash=None,
            fields_changed=[],
            kind=idsafe(dump.get("kind")),
            record_id=dump.get("record_id"),
            text=ic(dump.get("text", "")),
        )


def envelope_deposit(
    window_id: str, env: Any, *, rid: str | None, status: str, reason: str | None = None
) -> None:
    """``write.envelope.trace`` for stage ``deposit``: ``occ -> rid`` (the join), or the reason
    the envelope was not stored. On success ``env.record_id`` is set to ``rid`` in memory."""
    if rid is not None:
        with contextlib.suppress(Exception):  # a frozen model: the event still carries the join
            env.record_id = rid
    tap.event(
        "write.envelope.trace",
        stage="deposit",
        window_id=window_id,
        occ=env.occurrence_id,
        write_id=idsafe(env.write_id),
        status=status,
        reason=reason,
        record_id=rid,
        fields_changed=[],
        **_env_summary(env),
    )


def ladder_event(
    *,
    rid: str,
    incumbent: str | None,
    verdict: str,
    rule: str | None,
    action: str,
    entity: object = None,
    attribute: object = None,
    trust_incoming: float | None = None,
    trust_incumbent: float | None = None,
) -> None:
    """``write.integrate.ladder``: the dedup / conflict decision of one semantic write."""
    tap.event(
        "write.integrate.ladder",
        rid=rid,
        incumbent=incumbent,
        verdict=verdict,
        rule=idsafe(rule),
        action=action,
        keyed=entity is not None and attribute is not None,
        entity=ic(entity or ""),
        attribute=ic(attribute or ""),
        trust_incoming=trust_incoming,
        trust_incumbent=trust_incumbent,
    )


def edges_pending(
    *, window_id: str, edges: int, covered: Sequence[str], queued_batches: int, evicted: str | None
) -> None:
    """``write.edges.pending``: a window's call-1 edges wait in memory for the graph stage."""
    tap.event(
        "write.edges.pending",
        window_id=window_id,
        edges=edges,
        covered_rids=list(covered)[:200],
        queued_batches=queued_batches,
        evicted_window=evicted,
    )


def edges_consumed(batches: Sequence[tuple[str, int]]) -> None:
    """``write.edges.consumed``: the sleep stage took these pending edge batches."""
    tap.event(
        "write.edges.consumed",
        batches=len(batches),
        edges=sum(n for _, n in batches),
        window_ids=[w for w, _ in batches][:200],
        edges_by_window=[n for _, n in batches][:200],
    )


# ── read path ─────────────────────────────────────────────────────────────────
def headers_event(headers: Sequence[Any]) -> None:
    """``compose.headers``: the lead header blocks (kind, rid, the records each was built from)."""
    items = []
    for pos, header in enumerate(headers):
        tags = [t for t in header.tags if isinstance(t, str)]
        kind = next((t for t in tags if t != "lead"), tags[0] if tags else "header")
        items.append(
            {
                "pos": pos,
                "rid": header.record_id,
                "kind": idsafe(kind),
                "source_ids": list(header.source.parents)[:50],
                "n_sources": len(header.source.parents),
                "chars": len(header.content),
            }
        )
    tap.event(
        "compose.headers",
        n=len(items),
        headers=items[:50],
        texts=[ic(h.content) for h in headers][:50] if inspect_content() else [],
    )


def rerank_components(
    rows: Sequence[Mapping[str, Any]], *, reranked: bool, mode: str | None = None
) -> None:
    """``read.rerank.components``: per candidate, in final rank order, the parts of its score
    (relevance, recency, importance, utility, trust) and its final score."""
    tap.event(
        "read.rerank.components",
        reranked=reranked,
        mode=mode,
        n=len(rows),
        items=[dict(r) for r in rows][:200],
    )


def planner_io(
    *, role: str, version: str | None, outcome: str, query: str, plan: Any = None
) -> None:
    """``read.planner.io``: a read-side planner / rewrite model call (text is content only)."""
    tap.event(
        "read.planner.io",
        role=role,
        version=idsafe(version),
        outcome=outcome,
        query_hash=digest(query),
        query=ic(query),
        prompt=ic(query),
        reply=ic(json.dumps(plan, default=str)) if plan is not None else ic(""),
        plan=ic(json.dumps(plan, default=str)) if plan is not None else ic(""),
    )


def retrieve_writeback(
    *, rids: Sequence[str], access_event: bool, ledger: bool, session: str | None
) -> None:
    """``read.retrieve.writeback``: the records a search touched (RETRIEVE event, read ledger)."""
    tap.event(
        "read.retrieve.writeback",
        rids=list(rids)[:200],
        n=len(rids),
        access_event=access_event,
        ledger=ledger,
        session=idsafe(session),
    )
