"""Skill stage ladder (M13.4): legality, the dry-run gate, terminal states."""

from __future__ import annotations

import pytest

from memspine.exceptions import ConflictError
from memspine.memories.procedural.lifecycle import (
    SKILL_STAGES,
    SkillStage,
    is_usable,
    next_stage,
    requires_dry_run,
)


def test_ladder_order_is_linear() -> None:
    assert SKILL_STAGES == (
        SkillStage.DRAFT,
        SkillStage.STAGED,
        SkillStage.VERIFIED,
        SkillStage.ACTIVE,
        SkillStage.DEPRECATED,
    )


def test_forward_promotions_are_legal() -> None:
    assert next_stage(SkillStage.DRAFT) is SkillStage.STAGED
    assert next_stage(SkillStage.STAGED) is SkillStage.VERIFIED
    assert next_stage(SkillStage.VERIFIED, dry_run_passed=True) is SkillStage.ACTIVE


def test_verified_to_active_requires_dry_run() -> None:
    assert requires_dry_run(SkillStage.VERIFIED)
    with pytest.raises(ConflictError, match="dry run"):
        next_stage(SkillStage.VERIFIED)


def test_dry_run_flag_is_ignored_before_verified() -> None:
    # Passing the flag early must not skip stages.
    assert next_stage(SkillStage.DRAFT, dry_run_passed=True) is SkillStage.STAGED


def test_active_has_no_forward_stage() -> None:
    with pytest.raises(ConflictError, match="no forward promotion"):
        next_stage(SkillStage.ACTIVE)


def test_deprecated_is_terminal() -> None:
    with pytest.raises(ConflictError, match="terminal"):
        next_stage(SkillStage.DEPRECATED, dry_run_passed=True)


def test_only_active_is_usable() -> None:
    assert is_usable(SkillStage.ACTIVE)
    assert is_usable("active")
    for stage in (SkillStage.DRAFT, SkillStage.STAGED, SkillStage.VERIFIED, SkillStage.DEPRECATED):
        assert not is_usable(stage)
    assert not is_usable(None)
