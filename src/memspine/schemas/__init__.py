"""Versioned Pydantic schemas for MemSpine (MOD-01).

Pure leaf package: imports nothing from other ``memspine`` modules.
Tiers: durable (current format validation, frozen fixtures), public (semver), internal (typed, no promise).
"""

from memspine.schemas.base import (
    DurableModel,
    DurablePart,
    InternalModel,
    PublicModel,
    TierModel,
    kind_id,
)
from memspine.schemas.export import collect_models, export, render
from memspine.schemas.reserved import RESERVED_NAMES, is_reserved, reserved_hits
from memspine.schemas.versioning import (
    NewerThanKnownError,
    SchemaValidationError,
    UnknownKindError,
    UnsupportedSchemaVersionError,
    declare_kind,
    latest_version,
    validate_payload,
)

__all__ = [
    "RESERVED_NAMES",
    "DurableModel",
    "DurablePart",
    "InternalModel",
    "NewerThanKnownError",
    "PublicModel",
    "SchemaValidationError",
    "TierModel",
    "UnknownKindError",
    "UnsupportedSchemaVersionError",
    "collect_models",
    "declare_kind",
    "export",
    "is_reserved",
    "kind_id",
    "latest_version",
    "render",
    "reserved_hits",
    "validate_payload",
]
