"""Pure semantic vocabulary; registration never enables extraction or persistence."""

from memspine.semantics.cardinality import (
    CardinalityConflict,
    CardinalityReport,
    ScopedValue,
    detect_cardinality_conflicts,
)
from memspine.semantics.lifecycle import (
    LifecycleState,
    RescheduleLink,
    is_achieved,
    validate_transition,
)
from memspine.semantics.origins import SOURCE_ORIGINS, SourceOrigin, SourceProvenance
from memspine.semantics.packs import (
    PackDependency,
    PackManifest,
    WithdrawnDefinition,
    check_pack_upgrade,
    compute_pack_digest,
    load_config_pack,
    load_plugin_pack,
)
from memspine.semantics.registry import (
    QualifierValue,
    RoleBindingCheck,
    SemanticRegistry,
    load_builtin_packs,
    load_builtin_registry,
    load_registry,
)
from memspine.semantics.sensitivity import HandlingPolicy, is_at_least_as_strict, tighten
from memspine.semantics.specs import PredicateSpec, PropertySpec, RoleSpec
from memspine.semantics.views import EdgeView, StoredEdge, edge_views

__all__ = [
    "SOURCE_ORIGINS",
    "CardinalityConflict",
    "CardinalityReport",
    "EdgeView",
    "HandlingPolicy",
    "LifecycleState",
    "PackDependency",
    "PackManifest",
    "PredicateSpec",
    "PropertySpec",
    "QualifierValue",
    "RescheduleLink",
    "RoleBindingCheck",
    "RoleSpec",
    "ScopedValue",
    "SemanticRegistry",
    "SourceOrigin",
    "SourceProvenance",
    "StoredEdge",
    "WithdrawnDefinition",
    "check_pack_upgrade",
    "compute_pack_digest",
    "detect_cardinality_conflicts",
    "edge_views",
    "is_achieved",
    "is_at_least_as_strict",
    "load_builtin_packs",
    "load_builtin_registry",
    "load_config_pack",
    "load_plugin_pack",
    "load_registry",
    "tighten",
    "validate_transition",
]
