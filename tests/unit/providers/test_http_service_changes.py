from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from memspine.providers.changes import (
    ChangeConflict,
    ChangePlan,
    ChangeRequest,
    ChangeScope,
    ChangeStatus,
    ModelRevisionResult,
    OperationCheckpoint,
    OperationRecord,
    ResourceEstimate,
    SourceCompleteness,
    TargetConfigRef,
)
from memspine.providers.http_service_changes import HTTPServiceChangeBackend


def fixture_change() -> tuple[ChangePlan, OperationRecord, ModelRevisionResult]:
    now = datetime.now(UTC)
    source = SourceCompleteness(complete=True, evidence_id="source-proof")
    request = ChangeRequest(
        category="llm",
        logical_service_id="llm.chat",
        source_revision="old",
        target_revision="new",
        requested_strategy="forward_revision",
        actor="operator",
        scope=ChangeScope(
            deployment_id="deployment-a",
            owner_id="deployment-a",
            resource_class="model_config",
            target_fingerprint="target-digest",
        ),
        target_config_ref=TargetConfigRef(
            provider="litellm", config_id="config-reference", semantic_fingerprint="target-digest"
        ),
        source_completeness=source,
    )
    plan = ChangePlan(
        plan_id="plan-a",
        request=request,
        source_semantic_fingerprint="source-digest",
        target_semantic_fingerprint="target-digest",
        dependency_revisions=(),
        steps=(),
        required_capabilities=frozenset(),
        source_completeness=source,
        estimate=ResourceEstimate(),
        created_at=now,
        expires_at=now + timedelta(minutes=5),
        supported=True,
    )
    operation = OperationRecord(
        operation_id="operation-a",
        plan_id="plan-a",
        deployment_id="deployment-a",
        status=ChangeStatus.RUNNING,
        operation_revision=2,
        fence_epoch=3,
        checkpoint=OperationCheckpoint(),
        created_at=now,
        updated_at=now,
    )
    result = ModelRevisionResult(
        operation_id="operation-a",
        status=ChangeStatus.VERIFIED,
        created_at=now,
        updated_at=now,
        logical_role="llm.chat",
        old_revision="old",
        new_revision="new",
        producer_revision="producer-2",
        prompt_schema_revision="prompt-1",
        in_flight_policy="drain",
        validation_report_id="actual-controller-proof",
    )
    return plan, operation, result


@pytest.mark.asyncio
async def test_http_backend_transports_all_phases_and_verifies_activation() -> None:
    plan, operation, result = fixture_change()
    phases: list[str] = []

    def controller(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        phase = payload["phase"]
        phases.append(phase)
        assert request.url.path == "/v1/provider-changes"
        assert request.headers["Idempotency-Key"] == f"operation-a:3:2:{phase}"
        assert payload["plan"]["request"]["target_config_ref"]["config_id"] == "config-reference"
        if phase == "prepare":
            return httpx.Response(
                200,
                json={
                    "operation_id": "operation-a",
                    "category": "llm",
                    "owner_id": "deployment-a",
                    "source_fingerprint": "source-digest",
                    "target_fingerprint": "target-digest",
                    "target_generation": "new",
                    "policy": "forward_only_revision",
                },
            )
        if phase == "execute":
            return httpx.Response(200, json={"phase": "executed"})
        if phase == "activate":
            return httpx.Response(
                200,
                json={
                    "operation_id": "operation-a",
                    "deployment_id": "deployment-a",
                    "logical_service_id": "llm.chat",
                    "target_revision": "new",
                    "target_fingerprint": "target-digest",
                    "activated": True,
                },
            )
        return httpx.Response(200, json=result.model_dump(mode="json"))

    async with httpx.AsyncClient(
        base_url="https://controller.invalid/", transport=httpx.MockTransport(controller)
    ) as client:
        backend = HTTPServiceChangeBackend(
            client, request_timeout_s=1.0, max_response_bytes=8192, max_request_bytes=8192
        )
        assert (await backend.prepare(plan, operation)).operation_id == operation.operation_id
        assert (await backend.execute(plan, operation, operation.checkpoint)).phase == "executed"
        assert await backend.verify(plan, operation) == result
        await backend.activate(plan, operation, result)
        assert await backend.cleanup(plan, operation, result) == result
        assert not client.is_closed
    assert phases == ["prepare", "execute", "verify", "activate", "cleanup"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["wrong_activation", "oversize", "malformed", "unauthorized", "redirect"]
)
async def test_remote_errors_are_bounded_and_redacted(mode: str) -> None:
    plan, operation, result = fixture_change()

    def controller(request: httpx.Request) -> httpx.Response:
        if mode == "unauthorized":
            return httpx.Response(401, text="secret-response-contents")
        if mode == "redirect":
            return httpx.Response(302, headers={"Location": "https://other.invalid/secret"})
        if mode == "oversize":
            return httpx.Response(200, content=b"secret-response-contents" * 100)
        if mode == "malformed":
            return httpx.Response(200, text="secret-response-contents")
        return httpx.Response(
            200,
            json={
                "operation_id": "other-operation",
                "deployment_id": "deployment-a",
                "logical_service_id": "llm.chat",
                "target_revision": "new",
                "target_fingerprint": "target-digest",
                "activated": True,
            },
        )

    async with httpx.AsyncClient(
        base_url="https://controller.invalid/", transport=httpx.MockTransport(controller)
    ) as client:
        backend = HTTPServiceChangeBackend(
            client, request_timeout_s=1.0, max_response_bytes=512, max_request_bytes=8192
        )
        with pytest.raises(ChangeConflict) as caught:
            await backend.activate(plan, operation, result)
        assert "secret-response-contents" not in str(caught.value)
        assert "other.invalid" not in str(caught.value)


@pytest.mark.asyncio
async def test_total_deadline_bounds_a_stalled_controller() -> None:
    plan, operation, _ = fixture_change()

    async def controller(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        base_url="https://controller.invalid/", transport=httpx.MockTransport(controller)
    ) as client:
        backend = HTTPServiceChangeBackend(
            client, request_timeout_s=0.01, max_response_bytes=512, max_request_bytes=8192
        )
        with pytest.raises(ChangeConflict, match="transport failed"):
            await backend.prepare(plan, operation)


@pytest.mark.asyncio
async def test_request_size_limit_fails_before_network_io() -> None:
    plan, operation, _ = fixture_change()

    def controller(request: httpx.Request) -> httpx.Response:
        pytest.fail("oversized request must not reach the controller")

    async with httpx.AsyncClient(
        base_url="https://controller.invalid/", transport=httpx.MockTransport(controller)
    ) as client:
        backend = HTTPServiceChangeBackend(
            client, request_timeout_s=1.0, max_response_bytes=512, max_request_bytes=16
        )
        with pytest.raises(ChangeConflict, match="request exceeds"):
            await backend.prepare(plan, operation)
