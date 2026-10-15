"""Tier base classes for ``memspine.schemas`` (schema session 1, S1-1..S1-4).

Three compatibility tiers:

* :class:`DurableModel` -- events, records, outbox rows, erasure manifests. Integer
  ``schema_version`` per kind, upcasters (see :mod:`memspine.schemas.versioning`), frozen fixtures.
* :class:`PublicModel` -- Python API, REST/MCP, plug-in protocols, receipts. Semver (``__semver__``).
* :class:`InternalModel` -- step-to-step types. Typed and validated, may change in any PR.

Every tier uses ``extra="allow"`` (S1-3): unknown fields are kept and round-tripped. The reserved
system-field rule of session 2 (condition 1) is *not* enforced here; it lives in
:mod:`memspine.schemas.reserved` and is applied by the extraction compiler.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["DurableModel", "DurablePart", "InternalModel", "PublicModel", "TierModel", "kind_id"]


class TierModel(BaseModel):
    """Common configuration. Not a tier itself; do not subclass directly."""

    model_config = ConfigDict(
        extra="allow",
        protected_namespaces=(),
        populate_by_name=True,
    )

    __tier__: ClassVar[str] = ""


class DurablePart(TierModel):
    """A value type embedded in durable kinds (evidence, temporal refs, ...).

    It is part of the durable tier (its schema is exported and checked as part of the owning kind), but it
    has no ``schema_version`` of its own: a change to a part is a change to every kind that embeds it.
    """

    __tier__: ClassVar[str] = "durable"


class DurableModel(TierModel):
    """Persisted kinds. Subclasses set ``__kind__`` and may raise the ``schema_version`` default.

    The kind id lives in the dunder ``__kind__`` (a legacy class-level ``kind: ClassVar[str]`` is still
    honoured) so that a durable model may itself have an instance field called ``kind`` (``MemoryEvent``).
    The default of ``schema_version`` is the *latest* version the code writes; it is declared to the
    upcaster registry when the subclass is created, so ``upcast(kind, payload)`` knows the target.
    """

    __tier__: ClassVar[str] = "durable"
    __kind__: ClassVar[str | None] = None
    schema_version: int = Field(default=1, ge=1)

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        kind = kind_id(cls, own=True)
        if kind is None:
            return  # intermediate abstract base
        from memspine.schemas.versioning import declare_kind

        declare_kind(kind, cls.model_fields["schema_version"].default)


def kind_id(cls: type[BaseModel], *, own: bool = False) -> str | None:
    """Durable kind id of ``cls`` (``__kind__``, else a legacy ``kind`` class attribute), or ``None``."""
    for name in ("__kind__", "kind"):
        value = cls.__dict__.get(name) if own else getattr(cls, name, None)
        if isinstance(value, str):
            return value
    return None


class PublicModel(TierModel):
    """Public surface. Subclasses set ``__semver__`` (``"MAJOR.MINOR.PATCH"``)."""

    __tier__: ClassVar[str] = "public"
    __semver__: ClassVar[str] = "0.0.0"


class InternalModel(TierModel):
    """Step-to-step types; no compatibility promise."""

    __tier__: ClassVar[str] = "internal"
