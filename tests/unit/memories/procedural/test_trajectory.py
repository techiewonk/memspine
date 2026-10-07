"""W17c / N27 pure helpers (ADR-060)."""

from __future__ import annotations

from memspine.memories.procedural.trajectory import (
    TrajectoryStep,
    observation_diff,
    step_index,
    step_text,
    trajectory_manifest,
    window_indices,
)


def test_observation_stored_as_diff() -> None:
    assert observation_diff(None, "a\nb") == "a\nb"
    diff = observation_diff("a\nb", "a\nc")
    assert "- b" in diff and "+ c" in diff and "a" not in diff.replace("- b", "")
    assert observation_diff("a", "a") == "(no change)"
    assert observation_diff("a", None) == ""


def test_manifest_is_deterministic() -> None:
    steps = [
        TrajectoryStep("search flights", page="home"),
        TrajectoryStep("search again"),
        TrajectoryStep("click book"),
    ]
    manifest = trajectory_manifest("book a flight", steps, "success", reward=1.0)
    assert manifest == trajectory_manifest("book a flight", steps, "success", reward=1.0)
    assert "goal book a flight" in manifest
    assert "start home" in manifest
    assert "actions search > click" in manifest
    assert "outcome success (reward 1)" in manifest
    assert "steps 3" in manifest
    assert step_text(0, steps[0], "x").startswith("step 0: search flights @ home")


def test_window_bounded_and_capped() -> None:
    assert window_indices([0, 1, 2, 3, 4], 2) == [1, 2, 3]
    assert window_indices([0, 1, 2], 0) == [0, 1]
    assert window_indices([0, 1, 2, 3], None) == [0, 1, 2]  # head opens the first steps
    assert window_indices(list(range(50)), 25, radius=30, cap=20) == list(range(15, 35))
    assert window_indices([], 1) == []
    assert step_index(["trajectory_step", "step:7"]) == 7
    assert step_index(["trajectory_head"]) is None
