"""Skill/plan lifecycle ladder (M13.4): draft → staged → verified → active →
deprecated, with a dry-run gate on the promotion into ``active``.

Pure state-machine logic — no I/O. The procedural store turns a legal
transition into a versioned WRITE through the write door; an illegal one
raises so a caller can never silently skip verification.
"""

from __future__ import annotations

from memspine.core.records import SkillStage
from memspine.exceptions import ConflictError

__all__ = ["SKILL_STAGES", "SkillStage", "is_usable", "next_stage", "requires_dry_run"]


#: Linear promotion order; ``deprecated`` is reachable from any live stage and
#: is terminal.
SKILL_STAGES: tuple[SkillStage, ...] = (
    SkillStage.DRAFT,
    SkillStage.STAGED,
    SkillStage.VERIFIED,
    SkillStage.ACTIVE,
    SkillStage.DEPRECATED,
)

_FORWARD: dict[SkillStage, SkillStage] = {
    SkillStage.DRAFT: SkillStage.STAGED,
    SkillStage.STAGED: SkillStage.VERIFIED,
    SkillStage.VERIFIED: SkillStage.ACTIVE,
}


def requires_dry_run(current: SkillStage) -> bool:
    """The verified→active promotion is gated on a passing dry run (M13.4):
    a skill only becomes executable after it has been proven to run."""
    return current is SkillStage.VERIFIED


def next_stage(current: SkillStage, *, dry_run_passed: bool = False) -> SkillStage:
    """The stage a legal promotion moves to. Raises on an illegal transition
    or a verified→active promotion without a passing dry run."""
    if current is SkillStage.DEPRECATED:
        raise ConflictError("a deprecated skill is terminal — it cannot be promoted")
    target = _FORWARD.get(current)
    if target is None:  # only ACTIVE reaches here (has no forward stage)
        raise ConflictError(f"skill stage {current.value!r} has no forward promotion")
    if requires_dry_run(current) and not dry_run_passed:
        raise ConflictError(
            "verified→active requires a passing dry run — promote(dry_run_passed=True)"
        )
    return target


def is_usable(stage: SkillStage | None) -> bool:
    """Only ACTIVE skills are offered for execution/retrieval (M13.4)."""
    return stage is SkillStage.ACTIVE
