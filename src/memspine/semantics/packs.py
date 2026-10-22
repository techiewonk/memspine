"""Versioned, digest-pinned vocabulary packs (RM-53).

Three sources share one data-only contract:

* ``builtin`` — packs shipped inside ``memspine.semantics`` (reserved
  ``memspine.*`` namespace), pinned by digests recorded in code;
* ``plugin`` — data files shipped inside another installed package, loaded by
  resource path with a caller-supplied digest pin;
* ``config`` — custom vocabulary JSON files named in configuration, with a
  digest pin.

A pack is data only: installing or loading it never enables extraction
(``extraction_rule_ids`` must be empty) and never activates it. Activation is
an effective-policy revision (RM-43) that pins the activated registry digest;
an unactivated catalogue edit triggers no requeue. Definition names are
globally unique across an assembled registry: a conflicting name is rejected,
never overridden by load order. Withdrawal marks a definition withdrawn (no new
jobs may use it) while the definition stays in the pack for history, replay
and erasure provenance; a later pack version may never drop a definition.
"""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from memspine.semantics.domains import DOMAIN_PATTERN
from memspine.semantics.specs import PredicateSpec, PropertySpec, RoleSpec

DefinitionKind = Literal["predicate", "property", "role"]
PackSource = Literal["builtin", "plugin", "config"]
_PACK_ID = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"
_DIGEST = r"^[0-9a-f]{64}$"
RESERVED_NAMESPACE = "memspine."


class PackDependency(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    pack_id: str = Field(pattern=_PACK_ID)
    digest: str = Field(pattern=_DIGEST)


class WithdrawnDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: DefinitionKind
    name: str
    withdrawn_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    reason: str = Field(min_length=1)


class PackManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    pack_id: str = Field(pattern=_PACK_ID)
    pack_version: int = Field(ge=1)
    source: PackSource
    status: Literal["approved", "proposed"]
    default_active: bool
    description: str = Field(min_length=1)
    #: RM-91: the pack's top topical domain, the fallback for its predicates that
    #: declare none (None = a general pack with no single topic).
    domain: str | None = Field(default=None, pattern=DOMAIN_PATTERN)
    dependencies: tuple[PackDependency, ...] = ()
    predicates: tuple[PredicateSpec, ...] = ()
    properties: tuple[PropertySpec, ...] = ()
    roles: tuple[RoleSpec, ...] = ()
    withdrawn: tuple[WithdrawnDefinition, ...] = ()

    @model_validator(mode="after")
    def _contract(self) -> PackManifest:
        reserved = self.pack_id.startswith(RESERVED_NAMESPACE)
        if reserved != (self.source == "builtin"):
            raise ValueError("the memspine.* namespace is reserved for built-in packs")
        if self.status == "proposed" and self.default_active:
            raise ValueError("a proposed pack cannot be active by default")
        if any(spec.extraction_rule_ids for spec in self.predicates):
            raise ValueError("packs are data only and cannot enable extraction rules")
        deps = [dep.pack_id for dep in self.dependencies]
        if len(set(deps)) != len(deps) or self.pack_id in deps:
            raise ValueError("duplicate or self dependency")
        names = self.definition_names()
        withdrawn = [(item.kind, item.name) for item in self.withdrawn]
        if len(set(withdrawn)) != len(withdrawn):
            raise ValueError("duplicate withdrawal")
        if not set(withdrawn) <= names:
            raise ValueError("a pack may only withdraw its own definitions")
        return self

    def definition_names(self) -> set[tuple[str, str]]:
        return (
            {("predicate", spec.name) for spec in self.predicates}
            | {("property", spec.name) for spec in self.properties}
            | {("role", spec.name) for spec in self.roles}
        )

    @property
    def digest(self) -> str:
        return compute_pack_digest(self)


def compute_pack_digest(manifest: PackManifest) -> str:
    """SHA-256 over canonical JSON; independent of definition order."""
    payload = manifest.model_dump(mode="json")
    for key in ("predicates", "properties", "roles"):
        payload[key] = sorted(payload[key], key=lambda item: str(item["name"]))
    payload["dependencies"] = sorted(payload["dependencies"], key=lambda item: item["pack_id"])
    payload["withdrawn"] = sorted(
        payload["withdrawn"], key=lambda item: (item["kind"], item["name"])
    )
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def parse_pack(text: str, *, source: PackSource, expected_digest: str) -> PackManifest:
    """Validate a manifest and its digest pin; mismatched pins are rejected."""
    manifest = PackManifest.model_validate_json(text)
    if manifest.source != source:
        raise ValueError(f"pack {manifest.pack_id} declares source {manifest.source}")
    if manifest.digest != expected_digest:
        raise ValueError(f"digest pin mismatch for pack {manifest.pack_id}")
    return manifest


def load_config_pack(path: str | Path, *, expected_digest: str) -> PackManifest:
    """Load a custom vocabulary named in configuration (JSON, data only)."""
    return parse_pack(
        Path(path).read_text(encoding="utf-8"), source="config", expected_digest=expected_digest
    )


def load_plugin_pack(package: str, resource: str, *, expected_digest: str) -> PackManifest:
    """Load a data-only pack shipped in another package; no plugin code runs."""
    if package == "memspine" or package.startswith("memspine."):
        raise ValueError("plugin packs cannot be loaded from memspine itself")
    text = files(package).joinpath(resource).read_text(encoding="utf-8")
    return parse_pack(text, source="plugin", expected_digest=expected_digest)


def check_pack_upgrade(previous: PackManifest, current: PackManifest) -> None:
    """A newer version keeps every definition and withdrawal; nothing is deleted.

    Changing a definition's content requires a new definition version; the
    registry currently supports version 1 only, so content changes are rejected.
    """
    if previous.pack_id != current.pack_id:
        raise ValueError("upgrade must keep the pack id")
    if current.pack_version <= previous.pack_version:
        raise ValueError("pack version must increase")
    if not previous.definition_names() <= current.definition_names():
        raise ValueError("a pack upgrade cannot remove definitions; withdraw them instead")
    old_withdrawn = {(item.kind, item.name) for item in previous.withdrawn}
    new_withdrawn = {(item.kind, item.name) for item in current.withdrawn}
    if not old_withdrawn <= new_withdrawn:
        raise ValueError("a withdrawal cannot be reverted")
    for kind, old_items, new_items in (
        ("predicate", previous.predicates, current.predicates),
        ("property", previous.properties, current.properties),
        ("role", previous.roles, current.roles),
    ):
        new_by_name = {item.name: item for item in new_items}
        for item in old_items:
            if new_by_name[item.name] != item:
                raise ValueError(f"{kind} {item.name} changed without a new definition version")
