"""Executable HTTP adapter for deployment-owned service change controllers.

The deployment controller performs quiescence, restart and native resource work.
This adapter transports typed requests and verifies acknowledgements; it cannot
turn a config edit into a deployment migration without that controller.
"""

from __future__ import annotations

import asyncio
import json
import math
from typing import Literal

import httpx
from pydantic import Field, TypeAdapter, ValidationError, field_validator

from memspine.providers.changes import (
    ChangeConflict,
    ChangePlan,
    ChangeResult,
    OperationCheckpoint,
    OperationRecord,
)
from memspine.providers.service_changes import ChangePreparation
from memspine.providers.settings import StrictSettings

Phase = Literal["prepare", "execute", "verify", "activate", "cleanup"]
_RESULT = TypeAdapter(ChangeResult)


class ServiceActivationReceipt(StrictSettings):
    operation_id: str = Field(min_length=1)
    deployment_id: str = Field(min_length=1)
    logical_service_id: str = Field(min_length=1)
    target_revision: str = Field(min_length=1)
    target_fingerprint: str = Field(min_length=1)
    activated: Literal[True]

    @field_validator("activated", mode="before")
    @classmethod
    def require_true_boolean(cls, value: object) -> object:
        if value is not True:
            raise ValueError("activation must be explicitly confirmed")
        return value


class HTTPServiceChangeBackend:
    """Use a deployment's real HTTP control plane through a borrowed client.

    Pass an authenticated, origin-bound AsyncClient whose lifecycle the caller
    owns. Each call has an explicit deadline and bounded response size. Requests
    carry only persisted plans/evidence (config references, never native settings).
    Redirects are disabled and errors exclude remote response bodies and URLs.
    The remote implementation must persist the idempotency key and checkpoint.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        request_timeout_s: float,
        max_response_bytes: int,
        max_request_bytes: int,
    ) -> None:
        if (
            isinstance(request_timeout_s, bool)
            or not math.isfinite(request_timeout_s)
            or request_timeout_s <= 0
        ):
            raise ValueError("request_timeout_s must be finite and positive")
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes < 1
        ):
            raise ValueError("max_response_bytes must be a positive integer")
        if (
            isinstance(max_request_bytes, bool)
            or not isinstance(max_request_bytes, int)
            or max_request_bytes < 1
        ):
            raise ValueError("max_request_bytes must be a positive integer")
        if not client.base_url.is_absolute_url:
            raise ValueError("control-plane client requires an absolute base_url")
        self._client = client
        self._timeout = request_timeout_s
        self._limit = max_response_bytes
        self._request_limit = max_request_bytes

    async def _call(
        self,
        phase: Phase,
        plan: ChangePlan,
        operation: OperationRecord,
        *,
        checkpoint: OperationCheckpoint | None = None,
        result: ChangeResult | None = None,
    ) -> bytes:
        if plan.request.category not in {"llm", "rerank", "cache", "worker"}:
            raise ChangeConflict(
                "HTTP service changes support model, cache and worker revisions only"
            )
        payload = {
            "protocol_version": 1,
            "phase": phase,
            "plan": plan.model_dump(mode="json"),
            "operation": operation.model_dump(mode="json"),
            "checkpoint": None if checkpoint is None else checkpoint.model_dump(mode="json"),
            "result": None if result is None else result.model_dump(mode="json"),
        }
        # The operation ID stays in JSON/headers, not an unescaped URL path.
        key = f"{operation.operation_id}:{operation.fence_epoch}:{operation.operation_revision}:{phase}"
        try:
            encoded = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (ValueError, TypeError):
            raise ChangeConflict("service change request is not valid JSON data") from None
        if len(encoded) > self._request_limit:
            raise ChangeConflict("service change request exceeds configured limit")
        try:
            async with (
                asyncio.timeout(self._timeout),
                self._client.stream(
                    "POST",
                    "v1/provider-changes",
                    content=encoded,
                    headers={"Idempotency-Key": key, "Content-Type": "application/json"},
                    timeout=self._timeout,
                    follow_redirects=False,
                ) as response,
            ):
                if response.status_code != 200:
                    raise ChangeConflict("deployment controller rejected service change")
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=min(self._limit + 1, 65536)):
                    data.extend(chunk)
                    if len(data) > self._limit:
                        raise ChangeConflict(
                            "deployment controller response exceeds configured limit"
                        )
                return bytes(data)
        except (httpx.HTTPError, TimeoutError):
            raise ChangeConflict("deployment controller transport failed") from None

    async def prepare(self, plan: ChangePlan, operation: OperationRecord) -> ChangePreparation:
        raw = await self._call("prepare", plan, operation)
        try:
            return ChangePreparation.model_validate_json(raw)
        except (ValidationError, ValueError):
            raise ChangeConflict("invalid deployment preparation receipt") from None

    async def execute(
        self,
        plan: ChangePlan,
        operation: OperationRecord,
        checkpoint: OperationCheckpoint,
    ) -> OperationCheckpoint:
        raw = await self._call("execute", plan, operation, checkpoint=checkpoint)
        try:
            return OperationCheckpoint.model_validate_json(raw)
        except (ValidationError, ValueError):
            raise ChangeConflict("invalid deployment execution checkpoint") from None

    async def verify(self, plan: ChangePlan, operation: OperationRecord) -> ChangeResult:
        raw = await self._call("verify", plan, operation)
        try:
            return _RESULT.validate_json(raw)
        except (ValidationError, ValueError):
            raise ChangeConflict("invalid deployment verification result") from None

    async def activate(
        self,
        plan: ChangePlan,
        operation: OperationRecord,
        result: ChangeResult,
    ) -> None:
        raw = await self._call("activate", plan, operation, result=result)
        try:
            receipt = ServiceActivationReceipt.model_validate(json.loads(raw))
        except (ValidationError, ValueError):
            raise ChangeConflict("invalid deployment activation receipt") from None
        if (
            receipt.operation_id != operation.operation_id
            or receipt.deployment_id != plan.request.scope.deployment_id
            or receipt.logical_service_id != plan.request.logical_service_id
            or receipt.target_revision != plan.request.target_revision
            or receipt.target_fingerprint != plan.target_semantic_fingerprint
        ):
            raise ChangeConflict("deployment activation receipt does not match the plan")

    async def cleanup(
        self,
        plan: ChangePlan,
        operation: OperationRecord,
        result: ChangeResult,
    ) -> ChangeResult:
        raw = await self._call("cleanup", plan, operation, result=result)
        try:
            return _RESULT.validate_json(raw)
        except (ValidationError, ValueError):
            raise ChangeConflict("invalid deployment cleanup result") from None


__all__ = ["HTTPServiceChangeBackend", "ServiceActivationReceipt"]
