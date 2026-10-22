"""Versioned vocabulary and compatibility checks, never a fact writer."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from functools import lru_cache
from importlib.resources import files
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict

from memspine.semantics.packs import PackManifest, parse_pack
from memspine.semantics.specs import PredicateSpec, PropertySpec, RoleSpec, ValueKind

# Digest pins of packaged packs (RM-53). Editing a packaged pack file requires
# updating its pin here in the same change.
BUILTIN_PACK_FILES: Mapping[str, str] = MappingProxyType(
    {"memspine.core": "catalog.json", "memspine.proposed": "proposed_catalog.json"}
)
BUILTIN_PACK_DIGESTS: Mapping[str, str] = MappingProxyType(
    {
        "memspine.core": "1ed89af8e14031cf4d7e2ed71685d4e3819830b1b973799101b16be7ba39dbd3",
        "memspine.proposed": "e6e581df8ed8751be146ff8caec7e33fcbf106eaa854c9b68a633a173d64ec15",
    }
)


def _index[T: (PredicateSpec, PropertySpec, RoleSpec)](items: Iterable[T]) -> Mapping[str, T]:
    result: dict[str, T] = {}
    for item in items:
        if item.version != 1:
            raise ValueError(f"unsupported semantic definition version: {item.name}")
        if item.name in result:
            raise ValueError(f"duplicate semantic definition: {item.name}")
        result[item.name] = item
    return MappingProxyType(result)


class QualifierValue(BaseModel):
    """A typed qualifier on a claim; ``text`` holds text/entity id/TemporalRef id."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: ValueKind
    text: str | None = None
    amount: Decimal | None = None
    flag: bool | None = None


class RoleBindingCheck(BaseModel):
    """Shape of one proposed role binding; evidence identity is checked elsewhere."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    role: str
    value_kind: ValueKind
    entity_type: str | None = None
    evidence: Literal["exact_span", "envelope", "temporal_ref"] | None = None
    unit: str | None = None
    amount: Decimal | None = None


_EVIDENCE_OK = {
    "exact_span": {"exact_span"},
    "span_or_envelope": {"exact_span", "envelope"},
    "temporal_ref": {"temporal_ref"},
}


class SemanticRegistry:
    """Read-only definitions; callers still validate evidence and source authority."""

    __slots__ = (
        "_digest",
        "_owners",
        "_pack_domains",
        "_predicates",
        "_properties",
        "_roles",
        "_withdrawn",
    )

    def __init__(
        self,
        predicates: Iterable[PredicateSpec],
        properties: Iterable[PropertySpec],
        roles: Iterable[RoleSpec],
        *,
        withdrawn: Iterable[tuple[str, str]] = (),
        owners: Mapping[tuple[str, str], str] | None = None,
        pack_domains: Mapping[str, str] | None = None,
    ) -> None:
        self._predicates = _index(predicates)
        self._properties = _index(properties)
        self._roles = _index(roles)
        self._withdrawn = frozenset(withdrawn)
        self._owners = MappingProxyType(dict(owners or {}))
        self._pack_domains = MappingProxyType(dict(pack_domains or {}))
        self._check_references()
        payload: dict[str, object] = {
            label: [spec.model_dump(mode="json") for _, spec in sorted(index.items())]
            for label, index in (
                ("predicates", self.predicates),
                ("properties", self.properties),
                ("roles", self.roles),
            )
        }
        payload["withdrawn"] = sorted(self._withdrawn)
        if self._pack_domains:  # RM-91: a pack's top domain changes what claims resolve to
            payload["pack_domains"] = dict(sorted(self._pack_domains.items()))
        self._digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _check_references(self) -> None:
        names = {("predicate", n) for n in self.predicates}
        names |= {("property", n) for n in self.properties} | {("role", n) for n in self.roles}
        if not self._withdrawn <= names:
            raise ValueError("withdrawal names an unknown definition")
        views: dict[str, str] = {}
        families: dict[str, PredicateSpec] = {}
        for predicate in self.predicates.values():
            if not set(predicate.role_names) <= self.roles.keys():
                raise ValueError(f"unknown role in {predicate.name}")
            if predicate.inverse_view:
                view = predicate.inverse_view
                if view in self.predicates or view in views:
                    raise ValueError(f"inverse view {view} collides with a predicate or view")
                views[view] = predicate.name
            if predicate.event_family:
                first = families.setdefault(predicate.event_family, predicate)
                if first.role_names != predicate.role_names:
                    raise ValueError(f"event family {predicate.event_family} must share roles")
            if predicate.affects_state:
                target = self.predicates.get(predicate.affects_state)
                if target is None or target.semantics != "state":
                    raise ValueError(f"{predicate.name} must affect a registered state")
            for key in predicate.required_qualifiers:
                prop = self.properties.get(key)
                if (
                    prop is None
                    or prop.category != "qualifier"
                    or predicate.name not in (prop.predicates)
                ):
                    raise ValueError(f"unknown required qualifier {key} in {predicate.name}")
        for prop in self.properties.values():
            for name in prop.predicates:
                target_predicate = self.predicates.get(name)
                if prop.category == "qualifier":
                    if target_predicate is None:
                        raise ValueError(f"qualifier targets unknown predicate: {prop.name}/{name}")
                    continue
                if target_predicate is None or target_predicate.property_category != prop.category:
                    raise ValueError(f"property/predicate category mismatch: {prop.name}/{name}")
                if not set(prop.value_kinds) <= set(target_predicate.value_kinds):
                    raise ValueError(f"property value kinds exceed predicate: {prop.name}")

    @classmethod
    def from_packs(cls, packs: Sequence[PackManifest]) -> SemanticRegistry:
        """Assemble packs; dependencies must be present and digest-pinned.

        Name conflicts across packs are rejected (no last-loaded-wins). A
        predicate's roles must come from its own pack or a declared dependency.
        """
        by_id: dict[str, PackManifest] = {}
        for pack in packs:
            if pack.pack_id in by_id:
                raise ValueError(f"duplicate pack {pack.pack_id}")
            by_id[pack.pack_id] = pack
        owners: dict[tuple[str, str], str] = {}
        for pack in packs:
            for dep in pack.dependencies:
                found = by_id.get(dep.pack_id)
                if found is None or found.digest != dep.digest:
                    raise ValueError(f"pack {pack.pack_id} needs {dep.pack_id} at a pinned digest")
            for key in sorted(pack.definition_names()):
                if key in owners:
                    raise ValueError(
                        f"{key[0]} {key[1]} conflicts: {owners[key]} and {pack.pack_id}"
                    )
                owners[key] = pack.pack_id
        for pack in packs:
            visible = {pack.pack_id} | {dep.pack_id for dep in pack.dependencies}
            for spec in pack.predicates:
                for role in spec.role_names:
                    if owners.get(("role", role)) not in visible:
                        raise ValueError(f"{spec.name} uses role {role} outside its dependencies")
            for prop in pack.properties:
                for name in prop.predicates:
                    if owners.get(("predicate", name)) not in visible:
                        raise ValueError(f"{prop.name} targets {name} outside its dependencies")
        return cls(
            (spec for pack in packs for spec in pack.predicates),
            (spec for pack in packs for spec in pack.properties),
            (spec for pack in packs for spec in pack.roles),
            withdrawn=((item.kind, item.name) for pack in packs for item in pack.withdrawn),
            owners=owners,
            pack_domains={p.pack_id: p.domain for p in packs if p.domain is not None},
        )

    @property
    def predicates(self) -> Mapping[str, PredicateSpec]:
        return self._predicates

    @property
    def properties(self) -> Mapping[str, PropertySpec]:
        return self._properties

    @property
    def roles(self) -> Mapping[str, RoleSpec]:
        return self._roles

    @property
    def digest(self) -> str:
        return self._digest

    def owner(self, kind: str, name: str) -> str | None:
        return self._owners.get((kind, name))

    def pack_domain(self, kind: str, name: str) -> str | None:
        """RM-91: the top domain of the pack owning a definition (None = none declared)."""
        owner = self._owners.get((kind, name))
        return self._pack_domains.get(owner) if owner is not None else None

    def is_withdrawn(self, kind: str, name: str) -> bool:
        return (kind, name) in self._withdrawn

    def sensitivity_for(self, predicate: str, attribute_key: str | None = None) -> frozenset[str]:
        labels = set(self.predicates[predicate].sensitivity)
        if attribute_key is not None:
            labels |= set(self.properties[attribute_key].sensitivity)
        return frozenset(labels)

    def _active(self, kind: str, name: str) -> None:
        if (kind, name) in self._withdrawn:
            raise ValueError(f"withdrawn {kind} {name} cannot be used for new work")

    def validate_compatibility(
        self,
        predicate: str,
        *,
        subject_type: str,
        value_kind: str,
        object_entity_type: str | None = None,
        attribute_key: str | None = None,
        unit: str | None = None,
        currency: str | None = None,
        amount: Decimal | None = None,
        role_names: Iterable[str] = (),
        context: Mapping[str, str] | None = None,
        qualifiers: Mapping[str, QualifierValue] | None = None,
        source_origin: str | None = None,
    ) -> PredicateSpec:
        """Check declared types/units/roles only, not grounding or asserted truth.

        ``amount`` is a Decimal supplied by the typed-value compiler, never a
        guessed conversion. Unresolved types must not be coerced to pass this gate.
        """
        spec = self.predicates[predicate]
        self._active("predicate", predicate)
        if subject_type not in spec.subject_types or value_kind not in spec.value_kinds:
            raise ValueError("incompatible subject or value kind")
        if source_origin is not None and source_origin not in spec.allowed_source_origins:
            raise ValueError("source origin is not allowed for this predicate")
        if value_kind == "entity":
            if spec.object_entity_types and object_entity_type not in spec.object_entity_types:
                raise ValueError("incompatible or unresolved object entity type")
        elif object_entity_type is not None:
            raise ValueError("non-entity value cannot carry an entity type")
        counts = Counter(role_names)
        for name, count in counts.items():
            role = self.roles.get(name)
            if name not in spec.role_names or role is None:
                raise ValueError("missing or unsupported predicate role")
            self._active("role", name)
            if role.max_bindings is not None and count > role.max_bindings:
                raise ValueError(f"role cardinality exceeded for {name}")
        if not set(spec.required_roles) <= counts.keys():
            raise ValueError("missing or unsupported predicate role")
        self._check_qualifiers(spec, qualifiers or {})
        if spec.property_category is None:
            if attribute_key is not None:
                raise ValueError("predicate does not accept a property key")
            return spec
        if attribute_key is None:
            raise ValueError("property predicate requires a registered property key")
        prop = self.properties[attribute_key]
        self._active("property", attribute_key)
        if predicate not in prop.predicates or subject_type not in prop.subject_types:
            raise ValueError("property is incompatible with predicate or subject")
        if value_kind not in prop.value_kinds:
            raise ValueError("incompatible property value kind")
        if any(not (context or {}).get(key) for key in prop.context_requirements):
            raise ValueError("property requires explicit context qualifiers")
        if prop.name == "capacity":
            capacity_units = {
                "volume": {"mL", "L", "m3"},
                "data": {"B", "KB", "MB", "GB"},
                "seating": {"seat"},
            }
            if unit not in capacity_units.get((context or {}).get("capacity_kind", ""), set()):
                raise ValueError("capacity kind and unit disagree")
        if prop.allowed_units and unit not in prop.allowed_units:
            raise ValueError("unsupported or missing explicit unit")
        if unit is not None and unit not in prop.allowed_units:
            raise ValueError("unit is not permitted for this property")
        if prop.requires_currency:
            if (
                currency is None
                or len(currency) != 3
                or not currency.isascii()
                or not currency.isupper()
                or not currency.isalpha()
            ):
                raise ValueError("currency requires an explicit three-letter code")
        elif currency is not None:
            raise ValueError("currency is not permitted for this property")
        if value_kind == "number":
            if not isinstance(amount, Decimal) or not amount.is_finite():
                raise ValueError("numeric property requires a finite Decimal")
            if prop.nonnegative_integer and (amount < 0 or amount != amount.to_integral_value()):
                raise ValueError("count requires a nonnegative integer")
        return spec

    def _check_qualifiers(
        self, spec: PredicateSpec, qualifiers: Mapping[str, QualifierValue]
    ) -> None:
        missing = set(spec.required_qualifiers) - qualifiers.keys()
        if missing:
            raise ValueError(f"missing required qualifier: {sorted(missing)[0]}")
        for key, value in qualifiers.items():
            prop = self.properties.get(key)
            if prop is None or prop.category != "qualifier" or spec.name not in prop.predicates:
                raise ValueError(f"qualifier {key} is not registered for {spec.name}")
            self._active("property", key)
            if value.kind not in prop.value_kinds:
                raise ValueError(f"incompatible qualifier value kind for {key}")
            if value.kind == "number":
                if value.amount is None or not value.amount.is_finite() or value.text is not None:
                    raise ValueError(f"qualifier {key} requires a finite Decimal")
            elif value.kind == "boolean":
                if value.flag is None:
                    raise ValueError(f"qualifier {key} requires a boolean")
            elif not value.text:
                raise ValueError(f"qualifier {key} requires a value")
            if (
                prop.allowed_values
                and value.kind == "text"
                and value.text not in prop.allowed_values
            ):
                raise ValueError(f"qualifier {key} value is not one of the allowed values")

    def validate_role_bindings(
        self, predicate: str, bindings: Iterable[RoleBindingCheck]
    ) -> PredicateSpec:
        """Per-slot types, cardinality and evidence requirement (RM-47).

        Passing this check does not prove that the cited evidence exists or
        supports the binding; the canonical compiler verifies spans/envelopes.
        """
        spec = self.predicates[predicate]
        self._active("predicate", predicate)
        counts: Counter[str] = Counter()
        for binding in bindings:
            role = self.roles.get(binding.role)
            if role is None or binding.role not in spec.role_names:
                raise ValueError(f"role {binding.role} is not declared for {predicate}")
            self._active("role", binding.role)
            counts[binding.role] += 1
            if role.max_bindings is not None and counts[binding.role] > role.max_bindings:
                raise ValueError(f"role cardinality exceeded for {binding.role}")
            if binding.value_kind not in role.value_kinds:
                raise ValueError(f"incompatible value kind for role {binding.role}")
            if binding.value_kind == "entity":
                if role.entity_types and binding.entity_type not in role.entity_types:
                    raise ValueError(f"incompatible or unresolved entity type for {binding.role}")
            elif binding.entity_type is not None:
                raise ValueError("non-entity binding cannot carry an entity type")
            if binding.evidence not in _EVIDENCE_OK[role.evidence_requirement]:
                raise ValueError(
                    f"role {binding.role} requires {role.evidence_requirement} evidence"
                )
            if binding.value_kind == "number":
                if binding.amount is None or not binding.amount.is_finite():
                    raise ValueError(f"role {binding.role} requires a finite Decimal")
                if role.requires_unit and not binding.unit:
                    raise ValueError(f"role {binding.role} requires an explicit unit")
            elif binding.unit is not None or binding.amount is not None:
                raise ValueError("only numeric bindings carry a unit or amount")
        if not set(spec.required_roles) <= counts.keys():
            raise ValueError("missing required predicate role")
        return spec


def _read_builtin(pack_id: str) -> PackManifest:
    text = files("memspine.semantics").joinpath(BUILTIN_PACK_FILES[pack_id]).read_text("utf-8")
    manifest = parse_pack(text, source="builtin", expected_digest=BUILTIN_PACK_DIGESTS[pack_id])
    if manifest.pack_id != pack_id:
        raise ValueError(f"packaged file for {pack_id} declares {manifest.pack_id}")
    return manifest


@lru_cache(maxsize=1)
def load_builtin_packs() -> tuple[PackManifest, ...]:
    """All packaged packs, including proposed ones that are not active by default."""
    return tuple(_read_builtin(pack_id) for pack_id in BUILTIN_PACK_FILES)


@lru_cache(maxsize=1)
def load_builtin_registry() -> SemanticRegistry:
    """Default-active packaged definitions, without provider or model imports."""
    return SemanticRegistry.from_packs([p for p in load_builtin_packs() if p.default_active])


def load_registry(
    pack_ids: Sequence[str], extra_packs: Sequence[PackManifest] = ()
) -> SemanticRegistry:
    """Explicitly selected packaged packs plus pinned plugin/config packs.

    Selecting a pack here is not activation: activating a registry digest for
    ingestion is an effective-policy revision (RM-43) owned by BI-06.
    """
    packaged = {pack.pack_id: pack for pack in load_builtin_packs()}
    unknown = [pack_id for pack_id in pack_ids if pack_id not in packaged]
    if unknown:
        raise ValueError(f"unknown packaged pack: {unknown[0]}")
    return SemanticRegistry.from_packs([packaged[p] for p in pack_ids] + list(extra_packs))
