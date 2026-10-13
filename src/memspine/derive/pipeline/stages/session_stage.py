"""Derive: the per-session stage runner shared by the session stages (F-15)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from memspine.compose.compression import CompressionPolicy
from memspine.derive import role_filter
from memspine.derive.pipeline.stages.context import PipelineContext, live_record
from memspine.derive.pipeline.stages.index import _members_fp, stage_marker
from memspine.observability import tap
from memspine.store.events import EventLogMode
from memspine.store.records import MemoryRecord, RecordStatus, chrono_key
from memspine.trust.pipeline import gates_v2


async def _live_members(ctx: PipelineContext, member_ids: list[str]) -> list[MemoryRecord]:
    """R2-2/R2-8: the session members a derived stage may read.

    Only ACTIVATED, unquarantined records: a forgotten (DELETED) or rolled-back
    (ARCHIVED) turn must never be re-mined into new memories. Cold-tier members
    are inflated, otherwise the stage would see compressed (blank) text.
    """
    inflate = CompressionPolicy.bind()
    members: list[MemoryRecord] = []
    role_on = ctx.config.write.role_policy_v2
    for record_id in member_ids:
        record = await live_record(ctx, record_id)
        if record is None or record.status is not RecordStatus.ACTIVATED or record.quarantined:
            continue
        if role_on:  # W2-03: system turns and echoes are raw only
            why = role_filter.block_reason(record.tags)
            if why is not None:
                tap.event("write.role.derive_skip", reason=why, id=record_id, site="session")
                continue
        members.append(inflate.inflate(record))
    members.sort(key=chrono_key)
    if gates_v2.config_gates_on(ctx.config):  # W2-04 gate 2: the extraction input filter
        members = gates_v2.filter_input(members)
    return members


#: work(namespace, session_key, members) -> (records produced, deposit errors).
#: Raising means the LLM call failed: the session is retried next cycle.
_StageWork = Callable[[str, str, list[MemoryRecord]], Awaitable[tuple[int, list[str]]]]


#: legacy_done(namespace, session_key): done-ness for logs written before markers.
_LegacyDone = Callable[[str, str], Awaitable[bool]]


async def _run_session_stage(
    ctx: PipelineContext,
    stage: str,
    counter: str,
    legacy_done: _LegacyDone,
    work: _StageWork,
) -> dict[str, object]:
    """Shared loop of the derived stages: each consolidated session once.

    A session is marked done once its LLM call succeeded, whatever the call
    produced (nothing, merges, rejects, or some failed deposits), so it is never
    re-sent to the LLM and a merge never re-inflates importance (R2-6/R5-2).
    A deposit that raises is reported and the rest still land.
    """
    if ctx.append_event is None:
        return {"status": "skipped", "reason": "read-only context (no write door)"}
    if ctx.config.event_log.mode is EventLogMode.EPHEMERAL:
        # R2-12: CONSOLIDATE events are not persisted, so there is nothing to
        # read sessions from; "ok, 0" would hide that the stage never runs.
        return {
            "status": "skipped",
            "reason": "event_log.mode=ephemeral: consolidated sessions are not in the log",
        }
    index = ctx.session_index
    await index.refresh(ctx.storage)
    processed = 0
    produced = 0
    errors: list[str] = []
    for (namespace, key), member_ids in list(index.sessions.items()):
        if ctx.scope_namespace is not None and namespace != ctx.scope_namespace:
            continue  # FU-SLEEP: a scoped sleep job touches only its own namespace
        state = index.state(stage, namespace, key)
        if state is True:
            continue
        if state is None and await legacy_done(namespace, key):
            continue
        members = await _live_members(ctx, member_ids)
        fp = _members_fp([m.record_id for m in members])
        if members and index.membership_done(stage, namespace, fp):
            members = []  # the same turns were already processed under another key
        if members:
            try:
                count, deposit_errors = await work(namespace, key, members)
            except Exception as exc:  # LLM is an enhancer, never a gate: retry next cycle
                errors.append(f"{namespace}:{key}: {exc}")
                continue
            produced += count
            errors.extend(deposit_errors)
            processed += 1
        await ctx.append_event(stage_marker(namespace, stage, key, members_fp=fp))
        index.mark(stage, namespace, key, done=True, members_fp=fp)
    return {
        "status": "ok" if not errors else "partial",
        "sessions": processed,
        counter: produced,
        "errors": errors,
    }


def _legacy_tag_check(
    ctx: PipelineContext, memory_type: str, matches: Callable[[MemoryRecord, str], bool]
) -> _LegacyDone:
    """Pre-marker idempotence: a derived record already names the session.

    Lists each namespace once per stage run, not once per session (R5-2).
    """
    cache: dict[str, list[MemoryRecord]] = {}

    async def done(namespace: str, key: str) -> bool:
        if namespace not in cache:
            cache[namespace] = await ctx.storage.list_records(namespace, memory_type)
        return any(matches(record, key) for record in cache[namespace])

    return done
