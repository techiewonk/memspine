"""Memory: procedural moved out of the engine (F-12)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from memspine.config import constants
from memspine.exceptions import ConflictError, MemspineError
from memspine.memories.procedural.lessons import (
    OutcomeReceipt,
    failures_dominate,
    lesson_key,
    lesson_text,
    outcome_rank,
    quarantine_lesson_key,
    quarantine_lesson_text,
    should_retire,
    source_signature,
)
from memspine.memories.procedural.skills import ProceduralMemory, make_skill_record
from memspine.memories.procedural.task_state import Subgoal, TaskState
from memspine.memories.procedural.task_state import update_subgoal as lib_update_subgoal
from memspine.memories.procedural.trajectory import (
    TrajectoryStep,
    observation_diff,
    step_index,
    step_text,
    trajectory_manifest,
    window_indices,
)
from memspine.observability.logging import EVENT_WRITE, get_logger
from memspine.services.storage.records_port import RecordsStore
from memspine.store.events import EventKind, MemoryEvent, fingerprint_payload
from memspine.store.records import (
    ArchivedVersion,
    MemoryRecord,
    RecordStatus,
    SkillStage,
    SourceInfo,
    new_record_id,
)
from memspine.store.scope import validate_namespace

_log = get_logger(__name__)


async def add_skill(
    eng: Any,
    content: str,
    name: str,
    namespace: str = "default",
    actor: str = "user",
    source: SourceInfo | None = None,
) -> MemoryRecord:
    """Store a skill at ``draft`` (M13.4). It must climb the ladder —
    staged → verified → dry-run-gated active — before it is ever offered."""
    return await eng._write_procedural(  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
        make_skill_record(
            validate_namespace(namespace),
            name,
            content,
            kind="skill",
            source=source or SourceInfo(role=actor),
        ),
        actor,
    )


async def record_plan(
    eng: Any,
    task: str,
    content: str,
    namespace: str = "default",
    actor: str = "assistant",
    source: SourceInfo | None = None,
) -> MemoryRecord:
    """E6: capture a validated multi-step plan after a task SUCCEEDED.

    Plans enter at ``staged`` (success was the first validation) and are
    held out of every retrieval surface — like E1 quarantine — until they
    are promoted through ``verified`` and the dry-run gate into ``active``.
    """
    return await eng._write_procedural(  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
        make_skill_record(
            validate_namespace(namespace),
            task,
            content,
            kind="plan",
            source=source or SourceInfo(role=actor),
        ),
        actor,
    )


async def _write_procedural(eng: Any, record: MemoryRecord, actor: str) -> MemoryRecord:
    storage = eng._require_started()
    if eng._procedural is None:
        raise MemspineError("procedural memory not enabled — set memories.procedural.enabled: true")
    ns = record.namespace
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        return await eng._write_locked(storage, ns, record, "procedural", actor)  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt


async def promote_skill(
    eng: Any,
    record_id: str,
    namespace: str = "default",
    dry_run_passed: bool = False,
) -> MemoryRecord:
    """One legal step up draft→staged→verified→active; verified→active
    requires ``dry_run_passed=True`` (M13.4)."""
    eng._require_started()
    if eng._procedural is None:
        raise MemspineError("procedural memory not enabled — set memories.procedural.enabled: true")
    ns = validate_namespace(namespace)
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        return await eng._procedural.promote(record_id, namespace=ns, dry_run_passed=dry_run_passed)  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt


async def deprecate_skill(eng: Any, record_id: str, namespace: str = "default") -> MemoryRecord:
    """Retire a skill/plan (terminal, M13.4)."""
    eng._require_started()
    if eng._procedural is None:
        raise MemspineError("procedural memory not enabled — set memories.procedural.enabled: true")
    ns = validate_namespace(namespace)
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        return await eng._procedural.deprecate(record_id, namespace=ns)  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt


async def skills(
    eng: Any,
    namespace: str = "default",
    kind: str | None = None,
    usable_only: bool = True,
) -> list[MemoryRecord]:
    """Procedural records; by default only the usable (ACTIVE) set (M13.4)."""
    eng._require_started()
    if eng._procedural is None:
        raise MemspineError("procedural memory not enabled — set memories.procedural.enabled: true")
    ns = validate_namespace(namespace)
    return eng._inflate_all(await eng._procedural.list(ns, kind=kind, usable_only=usable_only), ns)  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt


def _procedural_policy(eng: Any) -> dict[str, Any]:
    """``memories.procedural.policies`` as configured (empty when unset)."""
    return eng._memory_policy(eng._config(), "procedural")  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt


def _require_procedural(eng: Any) -> ProceduralMemory:
    eng._require_started()
    if eng._procedural is None:
        raise MemspineError("procedural memory not enabled — set memories.procedural.enabled: true")
    return eng._procedural  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt


def _is_live_advisory(record: MemoryRecord, kind: str) -> bool:
    """A lesson / exemplar that is still offered: advisory stage, held status
    (out of ordinary search), never quarantined, not retired."""
    return (
        record.attribute == kind
        and record.skill_stage is SkillStage.ADVISORY
        and record.status is RecordStatus.RESOLVING
        and not record.quarantined
    )


async def _append_feedback(eng: Any, ns: str, record_id: str, signal: str, note: str = "") -> None:
    """N28: one FEEDBACK event (caller holds the namespace write lock)."""
    payload: dict[str, object] = {"record_id": record_id, "signal": signal}
    if note:
        payload["content"] = note[: constants.FEEDBACK_NOTE_MAX_CHARS]
    await eng._append_and_project(
        MemoryEvent(kind=EventKind.FEEDBACK, namespace=ns, actor="system", payload=payload)
    )


async def record_outcome(
    eng: Any,
    task: str,
    outcome: str,
    namespace: str = "default",
    *,
    action: str | None = None,
    error: str | None = None,
    next_action: str | None = None,
    reward: float | None = None,
    tool: str | None = None,
    used_ids: Sequence[str] = (),
    derived_from: Sequence[str] = (),
    lesson: bool = True,
    actor: str = "assistant",
) -> MemoryRecord | None:
    """W17a + N28: record a structured outcome receipt.

    Each plan or lesson in ``used_ids`` (what was injected for this run) gets a
    helpful (success) or harmful (failure) count, a FEEDBACK like / dislike on
    the log. With ``memories.procedural.policies.auto_verify_on_reward`` a
    reward of at least ``OUTCOME_AUTO_VERIFY_REWARD`` advances a used staged
    plan to verified (``active`` still needs the dry-run gate).

    With ``lesson`` (default), the receipt also becomes a ``kind="lesson"``
    record: the fixed-field template of :func:`lesson_text`, at the
    ``advisory`` stage (never executable, never promotable), trust capped at
    its ``derived_from`` parents (the trajectory's steps). A receipt whose key
    (trigger words, action signature, error class) matches a live lesson adds
    a repeat note to it instead of a new row. Returns the lesson (new or
    repeated) or None when ``lesson`` is false.
    """
    procedural = eng._require_procedural()
    receipt = OutcomeReceipt(
        task=task,
        outcome=outcome,  # type: ignore[arg-type]  # validated by the receipt
        action=action,
        error=error,
        next_action=next_action,
        reward=reward,
        tool=tool,
    )
    ns = validate_namespace(namespace)
    storage = eng._require_started()
    signal = "like" if receipt.outcome == "success" else "dislike"
    auto_verify = bool(eng._procedural_policy().get("auto_verify_on_reward"))
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        for record_id in dict.fromkeys(used_ids):
            used = await storage.get_record(record_id)
            if used is None or used.namespace != ns or used.memory_type != "procedural":
                raise ConflictError(f"no such procedural record {record_id!r} in {ns!r}")
            await eng._append_feedback(ns, record_id, signal)
            if (
                auto_verify
                and reward is not None
                and reward >= constants.OUTCOME_AUTO_VERIFY_REWARD
                and used.attribute == "plan"
                and used.skill_stage is SkillStage.STAGED
                and not used.quarantined
            ):
                await procedural.promote(record_id, namespace=ns)
    if not lesson:
        return None
    key = f"{constants.LESSON_KEY_TAG_PREFIX}{lesson_key(receipt)}"
    trust_cap = await eng._parent_trust_cap(ns, list(derived_from))
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        repeat = await eng._repeat_lesson(storage, ns, key, f"repeat:{receipt.outcome}")
        if repeat is not None:
            return repeat  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
        record = make_skill_record(
            ns,
            receipt.task.strip(),
            lesson_text(receipt),
            kind="lesson",
            source=SourceInfo(role=actor, channel="lesson", parents=list(derived_from)),
        ).model_copy(update={"tags": [constants.LESSON_TAG, key, f"outcome:{outcome}"]})
        return await _write_lesson(eng, storage, ns, record, actor, trust_cap)


async def _write_lesson(
    eng: Any,
    storage: RecordsStore,
    ns: str,
    record: MemoryRecord,
    actor: str,
    trust_cap: list[float] | None,
    *,
    parents: Sequence[str] | None = None,
) -> MemoryRecord:
    """FU-SAFE: a lesson through the derived-write door when ``write.lineage_table`` is on and it
    has parents (the trajectory steps, or the held record), so lineage and erasure find it as a
    dependant. No parent, or the flag off: the plain write, as before. Caller holds the lock."""
    pids = list(parents if parents is not None else record.source.parents)
    if not eng._config().write.lineage_table or not pids:
        return await eng._write_locked(  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
            storage, ns, record, "procedural", actor, trust_cap=trust_cap
        )
    return await eng._write_derived(  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
        storage, ns, record, "procedural", actor, parents=pids, trust_cap=trust_cap, site="lesson"
    )


async def _repeat_lesson(
    eng: Any, storage: RecordsStore, ns: str, key_tag: str, note: str
) -> MemoryRecord | None:
    """N28: the live lesson carrying ``key_tag`` with one more repeat note, or None."""
    for existing in await storage.list_records(ns, "procedural"):
        if key_tag in existing.tags and eng._is_live_advisory(existing, "lesson"):
            await eng._append_feedback(ns, existing.record_id, "note", note)
            return await storage.get_record(existing.record_id)
    return None


async def _quarantine_lesson(
    eng: Any, storage: RecordsStore, ns: str, held: MemoryRecord, reasons: list[str]
) -> None:
    """N24 (``memories.procedural.policies.quarantine_lesson``): a quarantine
    verdict becomes an advisory lesson naming the source, the reasons and the
    content hash, never the held text. Repeats from one source signature add
    notes (N28). The caller holds the namespace write lock."""
    key = f"{constants.LESSON_KEY_TAG_PREFIX}{quarantine_lesson_key(held, reasons)}"
    if await eng._repeat_lesson(storage, ns, key, "repeat:quarantine") is not None:
        return
    record = make_skill_record(
        ns,
        f"content held from {source_signature(held)}",
        quarantine_lesson_text(held, reasons),
        kind="lesson",
        source=SourceInfo(role="system", channel="firewall"),
    ).model_copy(update={"tags": [constants.LESSON_TAG, key, "outcome:quarantine"]})
    parents = [held.record_id] if await storage.get_record(held.record_id) is not None else []
    await _write_lesson(eng, storage, ns, record, "system", None, parents=parents)


async def recall_lessons(
    eng: Any,
    task: str,
    namespace: str = "default",
    k: int = constants.LESSON_BLOCK_MAX,
    min_similarity: float = constants.LESSON_MIN_SIMILARITY,
) -> list[tuple[MemoryRecord, float]]:
    """W17a: the live lessons whose task trigger is most similar to ``task``,
    ranked by ``cos x (1 + helpful) / (1 + harmful)`` (N28), at most ``k``.
    Each returned lesson's use rides the log as a RETRIEVE."""
    eng._require_procedural()
    ns = validate_namespace(namespace)
    scored = [
        (record, outcome_rank(sim, record.scoring.likes, record.scoring.dislikes))
        for record, sim in await eng._similar_advisory(ns, task, "lesson", min_similarity)
        if not failures_dominate(record.scoring.likes, record.scoring.dislikes)
    ]
    scored.sort(key=lambda pair: (-pair[1], pair[0].recorded_at))
    top = scored[: max(0, k)]
    if top:
        await eng._append_and_project(
            MemoryEvent(
                kind=EventKind.RETRIEVE,
                namespace=ns,
                actor="system",
                payload={"record_ids": [r.record_id for r, _ in top]},
            )
        )
    return [(eng._inflate.inflate(r), s) for r, s in top]


async def prune_experience(eng: Any, namespace: str = "default") -> list[str]:
    """N28: retire (deprecate, terminal) plans and lessons whose failures
    dominate, or that were used at least ``OUTCOME_RETIRE_MIN_USED`` times with
    a helpful share below ``OUTCOME_RETIRE_MIN_RATIO``. Returns retired ids."""
    procedural = eng._require_procedural()
    ns = validate_namespace(namespace)
    retired: list[str] = []
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        for record in await procedural.list(ns):
            if record.attribute not in ("plan", "lesson") or record.skill_stage in (
                None,
                SkillStage.DEPRECATED,
            ):
                continue
            score = record.scoring
            if failures_dominate(score.likes, score.dislikes) or should_retire(
                score.likes, score.access_count
            ):
                await procedural.deprecate(record.record_id, namespace=ns)
                retired.append(record.record_id)
    return retired


async def record_trajectory(
    eng: Any,
    goal: str,
    steps: Sequence[TrajectoryStep | Mapping[str, str]],
    outcome: str,
    namespace: str = "default",
    *,
    reward: float | None = None,
    trajectory_id: str | None = None,
    start: str | None = None,
    actor: str = "assistant",
) -> list[MemoryRecord]:
    """W17c + N27: a trajectory as one episodic group (``group_id`` =
    ``trajectory_id``, new when None). Each step is one record (tag
    ``step:<i>``) holding its action, page and the observation as a line diff
    against the previous step; the head record (``trajectory_head``) is the
    deterministic manifest of :func:`trajectory_manifest`. Returns the head
    first, then the steps; pass the step ids as ``derived_from`` of the plan or
    lesson the run produced."""
    eng._require_started()
    if eng._episodic is None:
        raise MemspineError("episodic memory not enabled — set memories.episodic.enabled: true")
    parsed = [
        step
        if isinstance(step, TrajectoryStep)
        else TrajectoryStep(
            action=step["action"], page=step.get("page"), observation=step.get("observation")
        )
        for step in steps
    ]
    if not parsed:
        raise ConflictError("a trajectory needs at least one step")
    group = trajectory_id or f"trajectory:{new_record_id()}"
    source = SourceInfo(role=actor, channel="trajectory")
    head = await eng.write(
        trajectory_manifest(goal, parsed, outcome, reward=reward, start=start),
        namespace=namespace,
        memory_type="episodic",
        source=source,
        actor=actor,
        group_id=group,
        tags=[constants.TRAJECTORY_HEAD_TAG],
    )
    records = [head]
    previous: str | None = None
    for index, step in enumerate(parsed):
        diff = observation_diff(previous, step.observation)
        previous = step.observation if step.observation is not None else previous
        records.append(
            await eng.write(
                step_text(index, step, diff),
                namespace=namespace,
                memory_type="episodic",
                source=source,
                actor=actor,
                group_id=group,
                tags=[
                    constants.TRAJECTORY_STEP_TAG,
                    f"{constants.TRAJECTORY_STEP_PREFIX}{index}",
                ],
            )
        )
    return records


async def trajectory_window(
    eng: Any,
    record_id: str,
    namespace: str = "default",
    radius: int = constants.TRAJECTORY_WINDOW_RADIUS,
    cap: int = constants.TRAJECTORY_WINDOW_CAP,
) -> list[MemoryRecord]:
    """N27: the steps around a trajectory hit (a step: within ``radius``; the
    head: the first steps), inside its group only, at most ``cap`` states, in
    step order. A record outside any trajectory gives an empty list."""
    storage = eng._require_started()
    ns = validate_namespace(namespace)
    hit = await storage.get_record(record_id)
    if hit is None or hit.namespace != ns or hit.group_id is None:
        return []
    is_head = constants.TRAJECTORY_HEAD_TAG in hit.tags
    if not is_head and constants.TRAJECTORY_STEP_TAG not in hit.tags:
        return []
    steps: dict[int, MemoryRecord] = {}
    for record in await storage.list_records(ns, "episodic", hit.group_id):
        index = step_index(record.tags)
        if (
            index is not None
            and constants.TRAJECTORY_STEP_TAG in record.tags
            and record.status is RecordStatus.ACTIVATED
            and not record.quarantined
        ):
            steps[index] = record
    wanted = window_indices(
        list(steps), None if is_head else step_index(hit.tags), radius=radius, cap=cap
    )
    return eng._inflate_all([steps[i] for i in wanted], ns)  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt


async def _task_state_record(eng: Any, ns: str, task_id: str) -> MemoryRecord | None:
    storage = eng._require_started()
    for record in await storage.list_records(ns, "working", task_id):
        if (
            record.source.channel == constants.TASK_STATE_CHANNEL
            and record.status is not RecordStatus.DELETED
        ):
            return record  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
    return None


async def _put_task_state(
    eng: Any, ns: str, state: TaskState, prior: MemoryRecord | None, reason: str
) -> MemoryRecord:
    """Keyed supersede in place (the persona pattern): one stable record per
    task, the prior state archived to its history, every version on the log."""
    text = state.model_dump_json()
    if prior is not None:
        record = prior.model_copy(
            update={
                "content": text,
                "content_fingerprint": fingerprint_payload({"content": text}),
                "version": prior.version + 1,
                "history": [
                    *prior.history,
                    ArchivedVersion(
                        version=prior.version,
                        content=prior.content,
                        archived_at=datetime.now(UTC),
                        reason=reason,
                    ),
                ],
            }
        )
    else:
        record = MemoryRecord(
            namespace=ns,
            memory_type="working",
            content=text,
            entity=state.task_id,
            attribute="state",
            group_id=state.task_id,
            source=SourceInfo(role="system", channel=constants.TASK_STATE_CHANNEL),
        )
    await eng._append_and_project(
        MemoryEvent(
            kind=EventKind.WRITE,
            namespace=ns,
            actor="system",
            payload={"record": record.model_dump(mode="json")},
        )
    )
    _log.info(EVENT_WRITE, namespace=ns, record_id=record.record_id, task_state=True)
    return record


async def set_task_state(
    eng: Any,
    task_id: str,
    goal: str,
    namespace: str = "default",
    *,
    subgoals: Sequence[str | Mapping[str, str]] = (),
    constraints: Sequence[str] = (),
    budget_left: float | None = None,
) -> TaskState:
    """W17e: open (or reset) the task-state record of ``task_id``: goal,
    constraints, subgoals (all ``pending``) and budget. The record lives in
    working memory (channel ``task_state``, ``group_id`` = ``task_id``); with
    ``memories.procedural.policies.task_state`` a read whose ``session_id`` is
    the task pins it right after the persona."""
    eng._require_started()
    ns = validate_namespace(namespace)
    state = TaskState(
        task_id=task_id,
        goal=goal,
        constraints=list(constraints),
        subgoals=[
            Subgoal(id=item)
            if isinstance(item, str)
            else Subgoal(id=item["id"], description=item.get("description", ""))
            for item in subgoals
        ],
        budget_left=budget_left,
    )
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        prior = await eng._task_state_record(ns, task_id)
        await eng._put_task_state(ns, state, prior, "task_state_reset")
    return state


async def task_state(eng: Any, task_id: str, namespace: str = "default") -> TaskState | None:
    """W17e: the current state of ``task_id`` (None when never opened)."""
    record = await eng._task_state_record(validate_namespace(namespace), task_id)
    return None if record is None else TaskState.model_validate_json(record.content)


async def update_subgoal(
    eng: Any,
    task_id: str,
    subgoal_id: str,
    status: str,
    namespace: str = "default",
    *,
    receipt_id: str | None = None,
    result: str | None = None,
    budget_left: float | None = None,
) -> TaskState:
    """W17e: move one subgoal to ``pending`` / ``done`` / ``failed`` (a keyed
    supersede of the task state). ``done`` requires ``receipt_id``."""
    if status not in ("pending", "done", "failed"):
        raise ConflictError(f"subgoal status must be pending, done or failed, got {status!r}")
    ns = validate_namespace(namespace)
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        prior = await eng._task_state_record(ns, task_id)
        if prior is None:
            raise ConflictError(f"no task state {task_id!r} in namespace {ns!r}")
        state = lib_update_subgoal(
            TaskState.model_validate_json(prior.content),
            subgoal_id,
            status,  # type: ignore[arg-type]  # checked above
            at=datetime.now(UTC),
            receipt_id=receipt_id,
            result=result,
        )
        if budget_left is not None:
            state = state.model_copy(update={"budget_left": budget_left})
        await eng._put_task_state(ns, state, prior, f"subgoal:{subgoal_id}->{status}")
    return state


async def close_task(eng: Any, task_id: str, namespace: str = "default") -> TaskState:
    """W17e: close the task (an explicit done, or a fired prospective watch):
    the state stays on the log but is no longer pinned in reads."""
    ns = validate_namespace(namespace)
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        prior = await eng._task_state_record(ns, task_id)
        if prior is None:
            raise ConflictError(f"no task state {task_id!r} in namespace {ns!r}")
        state = TaskState.model_validate_json(prior.content).model_copy(update={"closed": True})
        await eng._put_task_state(ns, state, prior, "task_closed")
    return state


async def task_search(
    eng: Any, task_id: str, query: str, namespace: str = "default", top_k: int = 10
) -> list[tuple[MemoryRecord, float]]:
    """W17e: a read scoped to the task first (``group_id`` = ``task_id``), then
    filled from the whole namespace (MemoryArena: all@10 = 0 unscoped)."""
    scoped = await eng.search(query, namespace=namespace, top_k=top_k, group_id=task_id)
    if len(scoped) >= top_k:
        return scoped[:top_k]  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
    seen = {record.record_id for record, _ in scoped}
    rest = [
        pair
        for pair in await eng.search(query, namespace=namespace, top_k=top_k)
        if pair[0].record_id not in seen
    ]
    return [*scoped, *rest][:top_k]


async def add_exemplar(
    eng: Any,
    text: str,
    label: str,
    namespace: str = "default",
    group: str = "default",
    actor: str = "user",
) -> MemoryRecord:
    """W17f: store a labelled exemplar (``kind="mapping"``, advisory stage) in
    the mapping group ``group``, through the firewall like any write."""
    record = make_skill_record(
        validate_namespace(namespace),
        label,
        text,
        kind="mapping",
        source=SourceInfo(role=actor, channel="mapping"),
    ).model_copy(update={"group_id": f"mapping:{group}"})
    return await eng._write_procedural(record, actor)  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
