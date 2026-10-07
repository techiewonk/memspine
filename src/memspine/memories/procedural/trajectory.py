"""W17c / N27 (plan v3.2, ADR-060): a trajectory as an episodic group.

One episodic record per step (``group_id`` = trajectory id, tag ``step:<i>``) whose
observation is stored as a line diff against the previous step, not the whole page
again; plus one head record, a deterministic manifest (goal, start, action
signature, outcome, step count) in the style of LME-V2 AgentRunbook-C's
``render_trajectory_summary``. A hit on a step or the head expands to a bounded
window of neighbouring steps. Pure functions; the engine does the I/O.
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from dataclasses import dataclass

from memspine.config import constants

__all__ = [
    "TrajectoryStep",
    "observation_diff",
    "step_index",
    "step_text",
    "trajectory_manifest",
    "trajectory_signature",
    "window_indices",
]


@dataclass(frozen=True)
class TrajectoryStep:
    """One step of an agent trajectory: the action, where it ran, what it saw."""

    action: str
    page: str | None = None
    observation: str | None = None


def observation_diff(previous: str | None, current: str | None) -> str:
    """The observation as a line diff against the previous one (``+`` added,
    ``-`` removed), or the whole observation for the first step."""
    if not current:
        return ""
    if previous is None:
        return current
    lines = [
        line
        for line in difflib.ndiff(previous.splitlines(), current.splitlines())
        if line.startswith(("+ ", "- "))
    ]
    return "\n".join(lines) or "(no change)"


def step_text(index: int, step: TrajectoryStep, diff: str) -> str:
    """The stored text of one step."""
    text = f"step {index}: {step.action.strip()}"
    if step.page:
        text += f" @ {step.page.strip()}"
    if diff:
        text += f"\n{diff}"
    return text


def trajectory_signature(steps: Sequence[TrajectoryStep]) -> str:
    """The action signature: each step's first word, consecutive repeats folded."""
    verbs: list[str] = []
    for step in steps:
        words = step.action.split()
        verb = words[0].lower() if words else "?"
        if not verbs or verbs[-1] != verb:
            verbs.append(verb)
    return " > ".join(verbs)


def trajectory_manifest(
    goal: str,
    steps: Sequence[TrajectoryStep],
    outcome: str,
    reward: float | None = None,
    start: str | None = None,
) -> str:
    """The head record: goal, start state, action signature, outcome / reward, steps."""
    first = start or (steps[0].page if steps and steps[0].page else None) or "-"
    result = outcome if reward is None else f"{outcome} (reward {reward:g})"
    return (
        f"Trajectory: goal {goal.strip()} · start {first} · actions "
        f"{trajectory_signature(steps) or '-'} · outcome {result} · steps {len(steps)}"
    )


def step_index(tags: Sequence[str]) -> int | None:
    """A step record's index from its ``step:<i>`` tag (None for the head)."""
    for tag in tags:
        if tag.startswith(constants.TRAJECTORY_STEP_PREFIX):
            try:
                return int(tag[len(constants.TRAJECTORY_STEP_PREFIX) :])
            except ValueError:
                return None
    return None


def window_indices(
    available: Sequence[int],
    hit: int | None,
    radius: int = constants.TRAJECTORY_WINDOW_RADIUS,
    cap: int = constants.TRAJECTORY_WINDOW_CAP,
) -> list[int]:
    """The step indices around ``hit`` within ``radius`` (the head, ``hit=None``,
    opens the first steps), capped at ``cap`` states, in step order."""
    ordered = sorted(set(available))
    if not ordered or cap < 1:
        return []
    if hit is None:
        return ordered[: min(cap, 2 * radius + 1)]
    picked = [i for i in ordered if abs(i - hit) <= radius]
    if len(picked) > cap:
        picked = sorted(sorted(picked, key=lambda i: (abs(i - hit), i))[:cap])
    return picked
