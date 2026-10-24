"""Provider management tables in the authoritative SQL schema."""

from __future__ import annotations

from sqlalchemy import JSON, Column, Integer, String, Table

from memspine.services.storage.sqlite.schema import metadata as provider_metadata

__all__ = ["provider_metadata"]

_plans = Table(
    "provider_change_plans",
    provider_metadata,
    Column("plan_id", String(128), primary_key=True),
    Column("deployment_id", String(256), nullable=False, index=True),
    Column("payload", JSON, nullable=False),
)

_operations = Table(
    "provider_change_operations",
    provider_metadata,
    Column("operation_id", String(128), primary_key=True),
    Column("plan_id", String(128), nullable=False, unique=True),
    Column("deployment_id", String(256), nullable=False, index=True),
    Column("status", String(32), nullable=False),
    Column("operation_revision", Integer, nullable=False),
    Column("fence_epoch", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
)

_fences = Table(
    "provider_change_fences",
    provider_metadata,
    Column("deployment_id", String(256), primary_key=True),
    Column("epoch", Integer, nullable=False),
    Column("active_operation_id", String(128)),
)

_cleanup_fence_recoveries = Table(
    "provider_cleanup_fence_recoveries",
    provider_metadata,
    Column("recovery_id", String(128), primary_key=True),
    Column("deployment_id", String(256), nullable=False, index=True),
    Column("operation_id", String(128), nullable=False, index=True),
    Column("proof_id", String(128), nullable=False, unique=True),
    Column("recovered_at", String(64), nullable=False),
    Column("payload", JSON, nullable=False),
)

_generation_rows = Table(
    "provider_projection_generations",
    provider_metadata,
    Column("deployment_id", String(256), primary_key=True),
    Column("logical_service_id", String(256), primary_key=True),
    Column("generation_id", String(256), primary_key=True),
    Column("status", String(32), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
)

_switch_rows = Table(
    "provider_projection_switches",
    provider_metadata,
    Column("switch_id", String(128), primary_key=True),
    Column("deployment_id", String(256), nullable=False, index=True),
    Column("status", String(32), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
)
