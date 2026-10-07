"""W17e (plan v3.2, ADR-060): one working ``task_state`` record per task.

The state is JSON ``{task_id, goal, constraints[], subgoals[{id, status, result,
receipt_id, at}], budget_left, closed}`` on a working record of channel
``task_state`` (``group_id`` = task id). Every update supersedes the record in
place with the prior state kept in its history, like the persona, so the log
carries the whole trail. A subgoal can only be ``done`` with a receipt id, which
stops a completed step reading as pending (CoMem) and a side effect being
repeated (InEp-Exec). Pure model and functions; the engine does the I/O.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from memspine.exceptions import ConflictError

__all__ = ["Subgoal", "SubgoalStatus", "TaskState", "render_task_state", "update_subgoal"]

SubgoalStatus = Literal["pending", "done", "failed"]


class Subgoal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    description: str = ""
    status: SubgoalStatus = "pending"
    result: str | None = None
    receipt_id: str | None = None
    at: datetime | None = None


class TaskState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    goal: str
    constraints: list[str] = Field(default_factory=list)
    subgoals: list[Subgoal] = Field(default_factory=list)
    budget_left: float | None = None
    closed: bool = False


def update_subgoal(
    state: TaskState,
    subgoal_id: str,
    status: SubgoalStatus,
    *,
    at: datetime,
    receipt_id: str | None = None,
    result: str | None = None,
) -> TaskState:
    """The state with one subgoal moved to ``status``. ``done`` needs a receipt id;
    an unknown subgoal raises (a typo must not read as progress)."""
    if status == "done" and not receipt_id:
        raise ConflictError(f"subgoal {subgoal_id!r} cannot be done without a receipt id")
    if state.closed:
        raise ConflictError(f"task {state.task_id!r} is closed")
    found = False
    subgoals: list[Subgoal] = []
    for subgoal in state.subgoals:
        if subgoal.id == subgoal_id:
            found = True
            subgoal = subgoal.model_copy(
                update={"status": status, "receipt_id": receipt_id, "result": result, "at": at}
            )
        subgoals.append(subgoal)
    if not found:
        raise ConflictError(f"task {state.task_id!r} has no subgoal {subgoal_id!r}")
    return state.model_copy(update={"subgoals": subgoals})


def render_task_state(state: TaskState) -> str:
    """The state as read context: goal, constraints, then each subgoal's status."""
    lines = [f"Task {state.task_id}: {state.goal}" + (" (closed)" if state.closed else "")]
    if state.constraints:
        lines.append("Constraints: " + "; ".join(state.constraints))
    for subgoal in state.subgoals:
        line = f"- [{subgoal.status}] {subgoal.id}"
        if subgoal.description:
            line += f": {subgoal.description}"
        if subgoal.result:
            line += f" -> {subgoal.result}"
        if subgoal.receipt_id:
            line += f" (receipt {subgoal.receipt_id})"
        lines.append(line)
    if state.budget_left is not None:
        lines.append(f"Budget left: {state.budget_left:g}")
    return "\n".join(lines)
