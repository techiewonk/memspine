"""Overload and accounting (schema session 9): the typed rejection payload and cost records."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import AwareDatetime, Field, model_validator

from memspine.schemas.base import PublicModel
from memspine.schemas.durable.common import OwnerScope

__all__ = ["COST_CLASSES", "CostRecord", "QuotaExceeded", "QuotaExceededError"]

COST_CLASSES: tuple[str, ...] = ("generative", "embedding", "rerank", "tool")


class QuotaExceeded(PublicModel):
    """A write over quota is rejected before acceptance; no receipt exists for it (ER-08)."""

    __semver__: ClassVar[str] = "1.0.0"

    error: Literal["quota_exceeded"] = "quota_exceeded"
    quota: Literal["storage", "queue_depth", "rate"]
    limit: float = Field(ge=0)
    current: float = Field(ge=0)
    retry_after: float | None = Field(default=None, ge=0)  # seconds


class QuotaExceededError(Exception):
    """Raised form of :class:`QuotaExceeded`."""

    def __init__(self, payload: QuotaExceeded) -> None:
        super().__init__(f"{payload.quota} quota exceeded ({payload.current} of {payload.limit})")
        self.payload = payload


class CostRecord(PublicModel):
    """One metered call, recorded per request and per job (ER-39).

    Generative calls are counted separately from embedding and rerank calls. ``purpose`` is the
    recovery-budget bucket (extraction, recovery or coverage) when the call belongs to a derivation.
    """

    __semver__: ClassVar[str] = "1.0.0"

    cost_class: Literal["generative", "embedding", "rerank", "tool"]
    scope: OwnerScope | None = None
    request_id: str | None = None
    job_id: str | None = None
    purpose: Literal["extraction", "recovery", "coverage", "read", "other"] = "other"
    model_id: str | None = None
    tool_name: str | None = None
    calls: int = Field(default=1, ge=0)
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    latency_ms: float | None = Field(default=None, ge=0)
    cost_estimate: float | None = Field(default=None, ge=0)
    at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _consistent(self) -> CostRecord:
        if self.request_id is None and self.job_id is None:
            raise ValueError("a cost record belongs to a request or a job")
        if self.cost_class == "tool" and not self.tool_name:
            raise ValueError("a tool cost record names the tool")
        if self.cost_class != "tool" and not self.model_id:
            raise ValueError(f"a {self.cost_class} cost record names the model")
        if self.cost_class in ("embedding", "rerank") and self.tokens_out:
            raise ValueError(f"{self.cost_class} calls produce no output tokens")
        return self
