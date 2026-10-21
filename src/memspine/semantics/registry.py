"""Versioned vocabulary and compatibility checks, never a fact writer."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from decimal import Decimal
from functools import lru_cache
from importlib.resources import files
from types import MappingProxyType

from memspine.semantics.specs import PredicateSpec, PropertySpec, RoleSpec


def _index[T: (PredicateSpec, PropertySpec, RoleSpec)](items: Iterable[T]) -> Mapping[str, T]:
    result: dict[str, T] = {}
    for item in items:
        if item.version != 1:
            raise ValueError(f"unsupported semantic definition version: {item.name}")
        if item.name in result:
            raise ValueError(f"duplicate semantic definition: {item.name}")
        result[item.name] = item
    return MappingProxyType(result)


class SemanticRegistry:
    """Read-only definitions; callers still validate evidence and source authority."""

    __slots__ = ("_digest", "_predicates", "_properties", "_roles")

    def __init__(
        self,
        predicates: Iterable[PredicateSpec],
        properties: Iterable[PropertySpec],
        roles: Iterable[RoleSpec],
    ) -> None:
        self._predicates = _index(predicates)
        self._properties = _index(properties)
        self._roles = _index(roles)
        for predicate in self.predicates.values():
            if not set(predicate.role_names) <= self.roles.keys():
                raise ValueError(f"unknown role in {predicate.name}")
        for prop in self.properties.values():
            for name in prop.predicates:
                target_predicate = self.predicates.get(name)
                if target_predicate is None or target_predicate.property_category != prop.category:
                    raise ValueError(f"property/predicate category mismatch: {prop.name}/{name}")
                if not set(prop.value_kinds) <= set(target_predicate.value_kinds):
                    raise ValueError(f"property value kinds exceed predicate: {prop.name}")
        payload = {
            label: [spec.model_dump(mode="json") for _, spec in sorted(index.items())]
            for label, index in (
                ("predicates", self.predicates),
                ("properties", self.properties),
                ("roles", self.roles),
            )
        }
        self._digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

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
    ) -> PredicateSpec:
        """Check declared types/units/roles only, not grounding or asserted truth.

        ``amount`` is a Decimal supplied by the typed-value compiler, never a
        guessed conversion. Unresolved types must not be coerced to pass this gate.
        """
        spec = self.predicates[predicate]
        if subject_type not in spec.subject_types or value_kind not in spec.value_kinds:
            raise ValueError("incompatible subject or value kind")
        if value_kind == "entity":
            if spec.object_entity_types and object_entity_type not in spec.object_entity_types:
                raise ValueError("incompatible or unresolved object entity type")
        elif object_entity_type is not None:
            raise ValueError("non-entity value cannot carry an entity type")
        names = tuple(role_names)
        if len(set(names)) != len(names):
            raise ValueError("duplicate role; role cardinalities must be explicit")
        if not set(spec.required_roles) <= set(names) or not set(names) <= set(spec.role_names):
            raise ValueError("missing or unsupported predicate role")
        if spec.property_category is None:
            if attribute_key is not None:
                raise ValueError("predicate does not accept a property key")
            return spec
        if attribute_key is None:
            raise ValueError("property predicate requires a registered property key")
        prop = self.properties[attribute_key]
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


@lru_cache(maxsize=1)
def load_builtin_registry() -> SemanticRegistry:
    """Load packaged definitions, without provider construction or model imports."""
    raw = json.loads(
        files("memspine.semantics").joinpath("catalog.json").read_text(encoding="utf-8")
    )
    if set(raw) != {"version", "predicates", "properties", "roles"} or raw["version"] != 1:
        raise ValueError("unsupported semantic catalogue version/fields")
    return SemanticRegistry(
        (PredicateSpec.model_validate_json(json.dumps(value)) for value in raw["predicates"]),
        (PropertySpec.model_validate_json(json.dumps(value)) for value in raw["properties"]),
        (RoleSpec.model_validate_json(json.dumps(value)) for value in raw["roles"]),
    )
