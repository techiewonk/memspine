from __future__ import annotations

import pytest

from memspine import Engine
from memspine.exceptions import MemspineError
from memspine.providers.changes import (
    ChangeConflict,
    ChangeRequest,
    ChangeScope,
    PlanStep,
    SourceCompleteness,
    TargetConfigRef,
    UnsupportedChangeStrategy,
)


class Guard:
    async def current_revision(self, logical_service_id: str) -> str:
        return "old"

    async def current_target_fingerprint(self, config_id: str) -> str:
        return "target-fingerprint"


def request(deployment: str) -> ChangeRequest:
    return ChangeRequest(
        category="llm",
        logical_service_id="llm.chat",
        source_revision="old",
        target_revision="new",
        scope=ChangeScope(
            deployment_id=deployment, owner_id=deployment, resource_class="model_config"
        ),
        target_config_ref=TargetConfigRef(
            provider="litellm", config_id="target", semantic_fingerprint="target-fingerprint"
        ),
        requested_strategy="forward_only",
        actor="test",
        source_completeness=SourceCompleteness(complete=True, evidence_id="forward-policy"),
    )


@pytest.mark.asyncio
async def test_engine_change_control_is_explicit_deployment_scoped_and_lifetime_bound() -> None:
    engine = Engine(
        template="core",
        dotenv_path=None,
        storage={"records": {"settings": {"path": ":memory:", "busy_timeout_s": 5.0}}},
        model={"embedding": {"provider": "hash", "settings": {}}},
        deployment={"id": "control-test"},
    )
    async with engine:
        control = engine.provider_changes(revision_guard=Guard(), strategies={}, plan_ttl_s=60.0)
        with pytest.raises(ChangeConflict, match="does not belong"):
            await control.plan_change(
                request("another-deployment"), steps=(), required_capabilities=frozenset()
            )
        plan = await control.plan_change(
            request("control-test"),
            steps=(
                PlanStep(
                    step_id="forward", phase="activate", description="Forward-only model revision"
                ),
            ),
            required_capabilities=frozenset(),
        )
        assert not plan.supported
        with pytest.raises(UnsupportedChangeStrategy):
            await control.execute_change(plan.plan_id)
    with pytest.raises(MemspineError):
        await control.execute_change(plan.plan_id)
