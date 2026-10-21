"""Pure semantic vocabulary; registration never enables extraction or persistence."""

from memspine.semantics.registry import SemanticRegistry, load_builtin_registry
from memspine.semantics.specs import PredicateSpec, PropertySpec, RoleSpec

__all__ = [
    "PredicateSpec",
    "PropertySpec",
    "RoleSpec",
    "SemanticRegistry",
    "load_builtin_registry",
]
