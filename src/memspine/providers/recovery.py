"""Explicit, application-verified recovery of abandoned cleanup fences."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import Field, model_validator

from memspine.providers.settings import StrictSettings


class CleanupFenceRecoveryRequest(StrictSettings):
    """Exact durable state an operator asks the application to recover."""

    deployment_id: str = Field(min_length=1, max_length=256)
    operation_id: str = Field(min_length=1, max_length=128)
    expected_operation_revision: int = Field(ge=1, strict=True)
    expected_fence_epoch: int = Field(ge=1, strict=True)
    evidence_reference: str = Field(min_length=1, max_length=256)
    actor: str = Field(min_length=1, max_length=128)


class CleanupQuiescenceProof(StrictSettings):
    """Application-issued proof that all possible fence holders are quiesced."""

    deployment_id: str = Field(min_length=1, max_length=256)
    operation_id: str = Field(min_length=1, max_length=128)
    operation_revision: int = Field(ge=1, strict=True)
    fence_epoch: int = Field(ge=1, strict=True)
    evidence_reference: str = Field(min_length=1, max_length=256)
    proof_id: str = Field(min_length=1, max_length=128)
    issued_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_window(self) -> CleanupQuiescenceProof:
        if self.issued_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("quiescence proof timestamps must be timezone-aware")
        if self.expires_at <= self.issued_at:
            raise ValueError("quiescence proof expiry must follow issue time")
        return self


class CleanupFenceRecoveryReceipt(StrictSettings):
    """Durable result; operation_revision is the revision written by recovery."""

    deployment_id: str = Field(min_length=1, max_length=256)
    operation_id: str = Field(min_length=1, max_length=128)
    operation_revision: int = Field(
        ge=1,
        strict=True,
        description="Operation revision after recovery has advanced the durable row.",
    )
    previous_fence_epoch: int = Field(ge=1, strict=True)
    released_fence_epoch: int = Field(ge=2, strict=True)
    proof_id: str = Field(min_length=1, max_length=128)
    recovered_at: datetime


class CleanupQuiescenceGuard(Protocol):
    """Application integration that can verify real process/worker quiescence.

    The library intentionally supplies no default implementation: an operator
    assertion or elapsed timeout cannot prove that a stale worker has stopped.
    """

    async def prove_quiescence(
        self, request: CleanupFenceRecoveryRequest
    ) -> CleanupQuiescenceProof: ...


__all__ = [
    "CleanupFenceRecoveryReceipt",
    "CleanupFenceRecoveryRequest",
    "CleanupQuiescenceGuard",
    "CleanupQuiescenceProof",
]
