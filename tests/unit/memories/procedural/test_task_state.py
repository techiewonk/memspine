"""W17e pure task-state model (ADR-060)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from memspine.exceptions import ConflictError
from memspine.memories.procedural.task_state import (
    Subgoal,
    TaskState,
    render_task_state,
    update_subgoal,
)

NOW = datetime(2026, 10, 7, tzinfo=UTC)


def _state() -> TaskState:
    return TaskState(
        task_id="t1",
        goal="buy a laptop",
        constraints=["under 1000 EUR"],
        subgoals=[Subgoal(id="compare"), Subgoal(id="order")],
    )


def test_done_requires_receipt() -> None:
    with pytest.raises(ConflictError, match="receipt"):
        update_subgoal(_state(), "order", "done", at=NOW)
    done = update_subgoal(_state(), "order", "done", at=NOW, receipt_id="rcpt-9")
    assert done.subgoals[1].status == "done" and done.subgoals[1].receipt_id == "rcpt-9"


def test_unknown_subgoal_and_closed_task_raise() -> None:
    with pytest.raises(ConflictError, match="no subgoal"):
        update_subgoal(_state(), "nope", "failed", at=NOW)
    closed = _state().model_copy(update={"closed": True})
    with pytest.raises(ConflictError, match="closed"):
        update_subgoal(closed, "compare", "failed", at=NOW)


def test_render_shows_status_and_receipt() -> None:
    state = update_subgoal(_state(), "compare", "done", at=NOW, receipt_id="r1", result="X1")
    text = render_task_state(state)
    assert "Task t1: buy a laptop" in text
    assert "[done] compare -> X1 (receipt r1)" in text
    assert "[pending] order" in text
    assert "Constraints: under 1000 EUR" in text
