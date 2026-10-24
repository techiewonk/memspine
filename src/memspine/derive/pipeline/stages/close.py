"""Derive: call 2 at session close (O4-02; LIB-40 ``derive/session_close.py``), opt-in.

``offline.session_close`` off (default): the stub in ``close.py`` runs and nothing here does. On,
one session close does:

1. read the session's live turns (``_live_members``: activated, unquarantined, inflated) and the
   episode stubs of the namespace that could be extended (semantic ``kind:episode`` records
   within the stitch adjacency of the session);
2. ``plan_session_close`` (budgeted, immutable) and ``render_prompt`` (stable prefix with no
   examples and the same-language rule, then the data body); ONE model call through
   ``ctx.close_call``, outside any namespace lock;
3. ``validate_call2``: every item is checked on its own (reserved names, citations, derived time),
   episodes are stitched onto adjacent stubs by code;
4. the summary and episodes are written through the derived-write door (parents = the cited
   turns, lineage when ``write.lineage_table``); the profile ops go to the guarded card when
   ``offline.profile_card`` is on (else they are only counted);
5. a ``stage_done`` marker keyed by the session and the turn fingerprint makes a repeat close a
   no-op; a late turn changes the fingerprint, so the session is read again as a new version.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime

from memspine.derive.pipeline.stages.context import (
    PipelineContext,
    SessionClosed,
    live_record,
    live_records,
)
from memspine.derive.pipeline.stages.index import stage_marker
from memspine.derive.pipeline.stages.profile_card import apply_session_ops
from memspine.derive.pipeline.stages.session_stage import _live_members
from memspine.derive.session_close import (
    Call2Plan,
    Call2Result,
    DerivedSessionRecord,
    ExistingEpisode,
    plan_session_close,
    render_prompt,
    validate_call2,
)
from memspine.memories.episodic.lifecycle import session_of
from memspine.observability import tap
from memspine.schemas.steps.shared import Turn
from memspine.services.llm.base import lenient_json
from memspine.store.events import EventKind, EventLogMode, MemoryEvent
from memspine.store.records import MemoryRecord, RecordStatus

#: the stage name of the done marker.
STAGE = "session_close"
#: at most this many episode stubs go into the plan (the plan trims further by budget).
_MAX_STUBS = 20
_SPEAKER = re.compile(r"^\s*([^\W\d_][\w .'-]{0,30}?):\s+")


def _split_speaker(record: MemoryRecord) -> tuple[str | None, str]:
    match = _SPEAKER.match(record.content)
    if match:
        return match.group(1).strip(), record.content[match.end() :]
    return None, record.content


def _aware(value: datetime | None) -> datetime | None:
    return value if value is not None and value.tzinfo is not None else None


def _turn(record: MemoryRecord, session_id: str) -> Turn:
    speaker, text = _split_speaker(record)
    return Turn(
        record_id=record.record_id,
        text=text,
        speaker=speaker or record.source.role or None,
        source_at=_aware(record.valid_from),
        session=session_id,
    )


async def _session_turns(ctx: PipelineContext, namespace: str, session_id: str) -> list[Turn]:
    records = await live_records(ctx, namespace, "episodic")
    ids = [r.record_id for r in records if session_of(r) == session_id]
    members = await _live_members(ctx, ids)
    return [_turn(m, session_id) for m in members if m.content.strip()]


async def _stubs(
    ctx: PipelineContext, namespace: str, turns: list[Turn], adjacency_s: float
) -> list[ExistingEpisode]:
    """Episodes already stored that this session's episodes may extend (O-09)."""
    times = [t.source_at for t in turns if t.source_at is not None]
    if not times:
        return []
    lo, hi = min(times).timestamp() - adjacency_s, max(times).timestamp() + adjacency_s
    out: list[ExistingEpisode] = []
    for record in await live_records(ctx, namespace, "semantic"):
        if "kind:episode" not in record.tags or record.status is not RecordStatus.ACTIVATED:
            continue
        if record.quarantined or _aware(record.valid_from) is None:
            continue
        parents = [
            p
            for p in [await live_record(ctx, pid) for pid in record.source.parents]
            if p is not None
        ]
        spans = [t for p in parents if (t := _aware(p.valid_from)) is not None]
        start = min(spans) if spans else record.valid_from
        end = max(spans) if spans else record.valid_from
        if not (lo <= end.timestamp() and start.timestamp() <= hi):
            continue
        speakers = {s for p in parents if (s := _split_speaker(p)[0])}
        out.append(
            ExistingEpisode(
                episode_id=record.record_id,
                text=record.content,
                participants=tuple(sorted(speakers)),
                start=start,
                end=end,
                parents=tuple(p.record_id for p in parents),
            )
        )
    out.sort(key=lambda s: (s.end.timestamp() if s.end else 0.0, s.episode_id))
    return out[-_MAX_STUBS:]


def _parse(raw: str, plan: Call2Plan) -> Call2Result:
    """Validate; a reply that is near-JSON gets one repair attempt (D-31 safety net)."""
    result = validate_call2(raw, plan)
    if result.parse_error is None:
        return result
    try:
        repaired = lenient_json(raw)
    except Exception:
        return result
    return validate_call2(repaired, plan) if isinstance(repaired, dict) else result


def _valid_from(record: DerivedSessionRecord, turns: list[Turn]) -> datetime:
    tr = record.time_range
    if tr is not None and tr.end is not None and tr.end.at is not None:
        return tr.end.at
    times = [t.source_at for t in turns if t.source_at is not None]
    return max(times) if times else datetime.now().astimezone()


def _tap(event: SessionClosed, **fields: object) -> None:
    if tap.enabled():
        tap.event("job.close.call2", session=event.session_id, **fields)


async def run_close(ctx: PipelineContext, event: SessionClosed) -> dict[str, object]:
    """Call 2 for one closed session. Never raises on model output; a failed model call is
    reported (``status: error``) and leaves no marker, so a later close retries it."""
    if ctx.close_call is None or ctx.deposit_session_record is None:
        return {"status": "skipped", "reason": "no call-2 LLM role bound / no deposit door"}
    if ctx.append_event is None or ctx.config.event_log.mode is EventLogMode.EPHEMERAL:
        return {"status": "skipped", "reason": "no durable log for the done marker"}
    ns, sid = event.namespace, event.session_id
    turns = await _session_turns(ctx, ns, sid)
    if not turns:
        _tap(event, status="skipped", reason="no_live_turns")
        return {"status": "skipped", "reason": "no live turns", "session_id": sid}
    budget = ctx.config.offline.session_close_budget
    base = plan_session_close(turns, (), budget, namespace=ns)
    stubs = await _stubs(ctx, ns, turns, base.stitch.adjacency.total_seconds())
    plan = plan_session_close(turns, stubs, budget, namespace=ns) if stubs else base
    index = ctx.session_index
    await index.refresh(ctx.storage)
    if (
        index.state(STAGE, ns, sid) is True
        and index.key_fp.get((STAGE, ns, sid)) == plan.members_fp
    ):
        _tap(event, status="skipped", reason="already_done")
        return {"status": "skipped", "reason": "already done", "session_id": sid}

    prompt = render_prompt(plan)
    messages = [
        {"role": "system", "content": prompt.prefix},
        {"role": "user", "content": prompt.body},
    ]
    try:
        raw = await ctx.close_call(messages)
    except Exception as exc:  # the model is an enhancer, never a gate: retry on a later close
        _tap(event, status="error", reason=type(exc).__name__)
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}", "session_id": sid}
    result = _parse(raw, plan)
    reasons = Counter(r.reason.value for r in result.rejections)
    if result.parse_error is not None:
        _tap(event, status="parse_error", failure=result.parse_error.code, turns=len(plan.lines))
        return {
            "status": "parse_error",
            "reason": result.parse_error.code,
            "session_id": sid,
        }

    written = not_written = 0
    stitched = 0
    errors: list[str] = []
    records: list[DerivedSessionRecord] = (
        [result.summary] if result.summary is not None else []
    ) + list(result.episodes)
    for rec in records:
        try:
            stored = await ctx.deposit_session_record(
                ns,
                rec.kind,
                rec.merged_text if rec.extends and rec.merged_text else rec.text,
                list(rec.parents),
                _valid_from(rec, turns),
                sid,
                extends=rec.extends,
                when_text=rec.when_text,
                participants=rec.participants,
                idem_key=rec.idem_key,
                folded=rec.folded,
            )
        except Exception as exc:  # one failed deposit never blocks the others
            errors.append(f"{ns}:{sid}: {rec.kind} deposit failed: {type(exc).__name__}: {exc}")
            continue
        if stored is None:
            not_written += 1
            continue
        written += 1
        stitched += 1 if rec.extends else 0

    profile: dict[str, object] = {"status": "off", "proposed": len(result.profile_ops)}
    if ctx.config.offline.profile_card:
        try:
            profile = await apply_session_ops(ctx, ns, sid, result.profile_ops)
        except Exception as exc:  # the card is an enhancer too
            errors.append(f"{ns}:{sid}: profile ops failed: {type(exc).__name__}: {exc}")
            profile = {"status": "error", "proposed": len(result.profile_ops)}

    if not errors:
        await ctx.append_event(stage_marker(ns, STAGE, sid, members_fp=plan.members_fp))
        index.mark(STAGE, ns, sid, done=True, members_fp=plan.members_fp)
    _tap(
        event,
        status="ok" if not errors else "partial",
        turns=len(plan.lines),
        deferred=len(plan.deferred_turn_ids),
        complete=plan.complete,
        summary=int(result.summary is not None),
        episodes=len(result.episodes),
        stitched=stitched,
        written=written,
        not_written=not_written,
        profile_ops=len(result.profile_ops),
        noop_ops=result.noop_ops,
        rejected=len(result.rejections),
        reasons=dict(sorted(reasons.items())),
    )
    return {
        "status": "ok" if not errors else "partial",
        "session_id": sid,
        "turns": len(plan.lines),
        "complete": plan.complete,
        "summary": int(result.summary is not None),
        "episodes": len(result.episodes),
        "stitched": stitched,
        "written": written,
        "not_written": not_written,
        "rejected": len(result.rejections),
        "reasons": dict(sorted(reasons.items())),
        "profile": profile,
        "errors": errors,
    }


SESSION_CLOSED_MARKER = "session_closed"


async def session_close(ctx: PipelineContext, event: SessionClosed) -> dict[str, object]:
    """Run the base session-close pipeline and record the boundary."""
    if ctx.close_session is not None:
        return await ctx.close_session(event)
    if not ctx.config.offline.session_close:
        await _mark_closed(ctx, event)
        return {"status": "skipped", "reason": "offline.session_close is off"}
    stats = await run_close(ctx, event)
    await _mark_closed(ctx, event)
    return stats


async def _mark_closed(ctx: PipelineContext, event: SessionClosed) -> None:
    """The marker a closed session leaves in the log (ids and a count, never content)."""
    if ctx.append_event is not None and ctx.config.event_log.mode is not EventLogMode.EPHEMERAL:
        await ctx.append_event(
            MemoryEvent(
                kind=EventKind.MARKER,
                namespace=event.namespace,
                actor="system",
                payload={
                    "marker": SESSION_CLOSED_MARKER,
                    "session_id": event.session_id,
                    "windows": event.windows,
                },
            )
        )
