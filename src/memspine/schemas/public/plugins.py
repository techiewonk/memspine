# ruff: noqa: E501
"""Plug-in manifest (schema session 12) and the cache key (schema session 10)."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable
from typing import ClassVar, Literal

from pydantic import Field, field_validator

from memspine.schemas.base import PublicModel
from memspine.schemas.durable.common import OwnerScope

__all__ = [
    "CacheKey",
    "CapabilityManifest",
    "DuplicateSlotError",
    "LifetimeViolation",
    "check_enabled",
    "check_lifetime",
    "normalise_query",
]

Lifetime = Literal["app", "job", "request"]
_RANK: dict[str, int] = {"app": 3, "job": 2, "request": 1}
_PIN = re.compile(r"^(==)?\d+(\.\d+){0,3}((a|b|rc|\.post|\.dev)\d+)?(\+[A-Za-z0-9.]+)?$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_NAME = re.compile(r"^[a-z][a-z0-9_.-]*$")


class DuplicateSlotError(ValueError):
    """Two enabled providers for the same slot (startup error)."""


class LifetimeViolation(ValueError):
    """A longer-lived component depends on a shorter-lived plug-in."""


class CapabilityManifest(PublicModel):
    """One enabled plug-in. Discovery is not enablement (ER-34): a plug-in is used only when listed in
    config under ``plugins:`` with its name and a version pin; a hash pin is optional."""

    __semver__: ClassVar[str] = "1.0.0"

    name: str
    version: str  # pin, exact: "1.4.2" or "==1.4.2"; ranges and wildcards are refused
    hash: str | None = None  # optional "sha256:<64 hex>"
    slot: str = Field(min_length=1)
    lifetime: Lifetime

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not _NAME.match(v):
            raise ValueError("plug-in names are lower-case [a-z0-9_.-] and start with a letter")
        return v

    @field_validator("version")
    @classmethod
    def _pin(cls, v: str) -> str:
        if not _PIN.match(v):
            raise ValueError("version must be an exact pin such as '1.4.2' or '==1.4.2'")
        return v

    @field_validator("hash")
    @classmethod
    def _hash(cls, v: str | None) -> str | None:
        if v is not None and not _HASH.match(v):
            raise ValueError("hash must look like 'sha256:' followed by 64 lower-case hex digits")
        return v


def check_enabled(manifests: Iterable[CapabilityManifest]) -> None:
    """Startup check: two providers for one slot, or one plug-in listed twice, is an error."""
    slots: dict[str, str] = {}
    names: set[str] = set()
    for m in manifests:
        if m.name in names:
            raise DuplicateSlotError(f"plug-in {m.name!r} is listed twice")
        names.add(m.name)
        if m.slot in slots:
            raise DuplicateSlotError(
                f"slot {m.slot!r} has two providers: {slots[m.slot]!r} and {m.name!r}"
            )
        slots[m.slot] = m.name


def check_lifetime(consumer: Lifetime, dependency: Lifetime) -> None:
    """DI check: a component may only hold a dependency that lives at least as long as it does."""
    if _RANK[dependency] < _RANK[consumer]:
        raise LifetimeViolation(
            f"a {consumer}-lifetime component cannot hold a {dependency}-lifetime plug-in"
        )


def normalise_query(query: str) -> str:
    """Cache-key normalisation: NFKC, case-folded, whitespace collapsed and trimmed."""
    return " ".join(unicodedata.normalize("NFKC", query).casefold().split())


class CacheKey(PublicModel):
    """A cache entry is keyed on all of these (ER-31); caches stay off until the ER-31 tests pass.

    The query is normalised on validation and ``model_ids`` is sorted and de-duplicated, so equivalent
    inputs give the same key.
    """

    __semver__: ClassVar[str] = "1.0.0"

    scope: OwnerScope
    viewer: str | None = None
    query: str
    config_hash: str = Field(min_length=1)
    model_ids: list[str] = Field(default_factory=list)
    store_generation: int = Field(ge=0)
    erasure_generation: int = Field(ge=0)

    @field_validator("query")
    @classmethod
    def _query(cls, v: str) -> str:
        out = normalise_query(v)
        if not out:
            raise ValueError("empty query")
        return out

    @field_validator("model_ids")
    @classmethod
    def _models(cls, v: list[str]) -> list[str]:
        return sorted(set(v))

    def digest(self) -> str:
        blob = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return "ck_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]
