"""Flow 11: task state and trajectories (working-memory task record, task-scoped search, step windows)."""

from __future__ import annotations

from flows._common import Ctx, Step
from memspine.exceptions import ConflictError

NAME = "tasks"
TITLE = (
    "Task state and trajectories: set/update/close a task, task-scoped search, trajectory windows"
)

CONFIG = {"memories": {"procedural": {"enabled": True}}}

TASK = "move-noor"


def _ascii(text: object) -> str:
    return str(text).encode("ascii", "replace").decode()


def _by_id(state: object) -> dict[str, object]:
    return {s.id: s for s in state.subgoals}  # type: ignore[attr-defined]


async def open_task(ctx: Ctx) -> None:
    e, ns = ctx.engine, ctx.ns
    ctx.expect("a task never opened has no state", None, await e.task_state(TASK, namespace=ns))
    st = await e.set_task_state(
        TASK,
        "move Noor's flat to Leiden",
        namespace=ns,
        subgoals=["boxes", {"id": "van", "description": "hire a van"}],
        constraints=["spend under 300"],
        budget_left=300,
    )
    ctx.show("set_task_state", st)
    got = await e.task_state(TASK, namespace=ns)
    ctx.expect("task_state returns what was set", st, got)
    ctx.expect(
        "every subgoal starts pending", ["pending", "pending"], [s.status for s in got.subgoals]
    )
    ctx.expect(
        "a string subgoal is taken as its id; a mapping keeps its description",
        ["", "hire a van"],
        [s.description for s in got.subgoals],
    )
    ctx.expect(
        "budget and constraints kept",
        (300.0, ["spend under 300"]),
        (got.budget_left, got.constraints),
    )
    rec = await e._task_state_record(
        ns, TASK
    )  # private: no public record handle for the task-state row
    ctx.show(
        "backing record (private _task_state_record)",
        {
            "type": rec.memory_type,
            "channel": rec.source.channel,
            "group_id": rec.group_id,
            "version": rec.version,
        },
    )
    ctx.expect(
        "the state lives in working memory, grouped by task id",
        ("working", "task_state", TASK),
        (rec.memory_type, rec.source.channel, rec.group_id),
    )


async def subgoal_updates(ctx: Ctx) -> None:
    e, ns = ctx.engine, ctx.ns
    st = await e.update_subgoal(
        TASK, "boxes", "done", namespace=ns, receipt_id="rc-1", result="12 boxes packed"
    )
    sg = _by_id(st)["boxes"]
    ctx.show("boxes", sg)
    ctx.expect(
        "done with a receipt is accepted",
        ("done", "rc-1", "12 boxes packed"),
        (sg.status, sg.receipt_id, sg.result),
    )
    ctx.check("the other subgoal is untouched", _by_id(st)["van"].status == "pending")
    errors = []
    for label, args, kw in [
        ("done without a receipt", ("van", "done"), {}),
        ("unknown subgoal", ("garage", "pending"), {}),
        ("bad status", ("van", "stuck"), {}),
    ]:
        try:
            await e.update_subgoal(TASK, *args, namespace=ns, **kw)
            errors.append((label, None))
        except ConflictError as exc:
            errors.append((label, _ascii(exc)))
    ctx.show("refusals", errors)
    ctx.check("all three bad updates are refused with ConflictError", all(msg for _, msg in errors))
    ctx.check(
        "a done step cannot be claimed without a receipt (no silent progress)",
        (await e.task_state(TASK, namespace=ns)).subgoals[1].status == "pending",
    )
    st = await e.update_subgoal(
        TASK, "van", "failed", namespace=ns, result="none free", budget_left=250
    )
    ctx.expect(
        "failed is a status, budget_left is updated",
        ("failed", 250.0),
        (_by_id(st)["van"].status, st.budget_left),
    )
    try:
        await e.update_subgoal("no-such-task", "x", "pending", namespace=ns)
        refused = False
    except ConflictError as exc:
        refused = True
        ctx.show("unknown task", _ascii(exc))
    ctx.check("updating a task that was never opened is refused", refused)
    rec = await e._task_state_record(ns, TASK)  # private: read the version trail
    reasons = [h.reason for h in rec.history]
    ctx.show(
        "version trail (private _task_state_record)",
        {"version": rec.version, "history reasons": reasons},
    )
    ctx.expect(
        "each update supersedes in place and archives the prior state",
        ["subgoal:boxes->done", "subgoal:van->failed"],
        reasons[-2:],
    )


async def search_scoped(ctx: Ctx) -> None:
    e, ns = ctx.engine, ctx.ns
    mine = await e.write(
        "Noor's van booking number is V-88.", namespace=ns, memory_type="episodic", group_id=TASK
    )
    other = await e.write(
        "Tomasz's van booking number is V-12.",
        namespace=ns,
        memory_type="episodic",
        group_id="move-tomasz",
    )
    loose = await e.write(
        "Van booking numbers are printed on the receipt.", namespace=ns, memory_type="episodic"
    )
    ctx.state.update(mine=mine.record_id)
    first = await e.task_search(TASK, "van booking number", namespace=ns, top_k=1)
    ctx.show("task_search top_k=1", [(r.content, r.group_id) for r, _ in first])
    ctx.expect(
        "top_k=1 is filled from the task's own group first",
        [mine.record_id],
        [r.record_id for r, _ in first],
    )
    many = await e.task_search(TASK, "van booking number", namespace=ns, top_k=5)
    ids = [r.record_id for r, _ in many]
    ctx.show("task_search top_k=5", [(r.content[:34], r.group_id) for r, _ in many])
    ctx.expect("the task's own record is ranked ahead of the rest", mine.record_id, ids[0])
    ctx.check(
        "the rest of the namespace fills the remaining slots",
        other.record_id in ids and loose.record_id in ids,
    )
    ctx.expect("no duplicates between the scoped and the filled results", len(ids), len(set(ids)))


async def close(ctx: Ctx) -> None:
    e, ns = ctx.engine, ctx.ns
    st = await e.close_task(TASK, namespace=ns)
    ctx.expect("close_task marks the state closed", True, st.closed)
    ctx.expect(
        "the closed state is still readable (stays on the log)",
        True,
        (await e.task_state(TASK, namespace=ns)).closed,
    )
    try:
        await e.update_subgoal(TASK, "van", "pending", namespace=ns)
        refused = False
    except ConflictError as exc:
        refused = True
        ctx.show("update after close", _ascii(exc))
    ctx.check("a closed task refuses further subgoal updates", refused)
    try:
        await e.close_task("no-such-task", namespace=ns)
        refused = False
    except ConflictError:
        refused = True
    ctx.check("closing a task that was never opened is refused", refused)
    reset = await e.set_task_state(TASK, "move Noor's flat again", namespace=ns, subgoals=["boxes"])
    ctx.expect(
        "set_task_state on an existing task resets it (reopens)",
        (False, "move Noor's flat again"),
        (reset.closed, reset.goal),
    )


STEPS_1 = [
    {"action": "open the council portal", "page": "home", "observation": "Login\nPay a bill"},
    {"action": "click pay a bill", "page": "pay", "observation": "Login\nPay a bill\nAmount due"},
    {"action": "enter the card number", "observation": "Login\nPay a bill\nAmount due\nCard"},
    {"action": "confirm", "observation": "Paid"},
]


async def trajectory(ctx: Ctx) -> None:
    e, ns = ctx.engine, ctx.ns
    recs = await e.record_trajectory(
        "pay the council tax", STEPS_1, "success", namespace=ns, reward=1.0
    )
    head, steps = recs[0], recs[1:]
    ctx.state.update(head=head.record_id, steps=[s.record_id for s in steps])
    ctx.show("head", head.content)
    for s in steps:
        ctx.show(f"  {s.tags[-1]}", s.content.replace("\n", " | "))
    ctx.expect("returns the head first, then one record per step", 5, len(recs))
    ctx.check(
        "head and steps share one trajectory group",
        len({r.group_id for r in recs}) == 1 and recs[0].group_id.startswith("trajectory:"),
    )
    ctx.expect(
        "steps are tagged step:<i> in order",
        ["step:0", "step:1", "step:2", "step:3"],
        [s.tags[-1] for s in steps],
    )
    ctx.check(
        "step 1 stores only the observation diff (+ Amount due), not the whole page",
        "+ Amount due" in steps[1].content and "Login" not in steps[1].content,
    )
    ctx.check(
        "the final step records what disappeared (- lines)",
        "- Login" in steps[3].content and "+ Paid" in steps[3].content,
    )
    ctx.check(
        "the head carries goal and outcome",
        "pay the council tax" in head.content and "success" in head.content.lower(),
    )
    try:
        await e.record_trajectory("empty", [], "success", namespace=ns)
        refused = False
    except ConflictError:
        refused = True
    ctx.check("a trajectory needs at least one step", refused)


async def window(ctx: Ctx) -> None:
    e, ns = ctx.engine, ctx.ns
    ids = ctx.state["steps"]

    async def tags(rid: str, **kw: int) -> list[str]:
        return [r.tags[-1] for r in await e.trajectory_window(rid, namespace=ns, **kw)]

    ctx.expect(
        "a middle step with radius 1 gives its neighbours",
        ["step:0", "step:1", "step:2"],
        await tags(ids[1]),
    )
    ctx.expect(
        "the last step gives itself and the one before", ["step:2", "step:3"], await tags(ids[3])
    )
    ctx.expect(
        "a wider radius reaches further, still in step order",
        ["step:0", "step:1", "step:2", "step:3"],
        await tags(ids[1], radius=2),
    )
    ctx.expect(
        "a head hit gives the first steps, capped by cap",
        ["step:0", "step:1"],
        await tags(ctx.state["head"], cap=2),
    )
    ctx.expect(
        "a record outside any trajectory gives an empty window", [], await tags(ctx.state["mine"])
    )
    ctx.expect("an unknown id gives an empty window", [], await tags("no-such-record"))


STEPS = [
    Step(
        "F11.1",
        "set_task_state / task_state: the task record in working memory",
        open_task,
        "memory/pipeline/procedural.py:set_task_state, task_state, _put_task_state",
    ),
    Step(
        "F11.2",
        "update_subgoal: receipt rule, refusals, version trail",
        subgoal_updates,
        "memories/procedural/task_state.py:update_subgoal",
    ),
    Step(
        "F11.3",
        "task_search: task's own group first, namespace fills the rest",
        search_scoped,
        "memory/pipeline/procedural.py:task_search",
    ),
    Step(
        "F11.4",
        "close_task: closed state kept, no more updates, reset reopens",
        close,
        "memory/pipeline/procedural.py:close_task",
    ),
    Step(
        "F11.5",
        "record_trajectory: head + step records with observation diffs",
        trajectory,
        "memory/pipeline/procedural.py:record_trajectory, memories/procedural/trajectory.py",
    ),
    Step(
        "F11.6",
        "trajectory_window: neighbours of a hit, radius and cap",
        window,
        "memory/pipeline/procedural.py:trajectory_window",
    ),
]
