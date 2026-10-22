"""RM-52 sensitivity, RM-53 packs and RM-50/58..63 vocabulary families."""

from __future__ import annotations

import itertools
import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from memspine.semantics import (
    HandlingPolicy,
    PackManifest,
    QualifierValue,
    SemanticRegistry,
    check_pack_upgrade,
    compute_pack_digest,
    is_at_least_as_strict,
    load_builtin_packs,
    load_builtin_registry,
    load_config_pack,
    load_plugin_pack,
    load_registry,
    tighten,
)
from memspine.semantics.packs import parse_pack
from memspine.semantics.registry import BUILTIN_PACK_DIGESTS
from memspine.semantics.sensitivity import SENSITIVITY_LABELS, effective_retention_days


@pytest.fixture
def registry() -> SemanticRegistry:
    return load_builtin_registry()


# ---- RM-52 ------------------------------------------------------------------

BASES = [
    HandlingPolicy(),
    HandlingPolicy(min_trust_tier=3, retention_days=30, legal_hold=True),
    HandlingPolicy(require_consent=True, exclude_from_shared=True, retention_days=7),
]


@pytest.mark.parametrize("base", BASES)
def test_every_label_combination_only_tightens(base):
    labels = sorted(SENSITIVITY_LABELS)
    for size in range(len(labels) + 1):
        for combo in itertools.combinations(labels, size):
            assert is_at_least_as_strict(tighten(base, combo), base)


def test_labels_never_clear_legal_hold_and_unknown_labels_fail():
    held = HandlingPolicy(legal_hold=True, retention_days=10)
    result = tighten(held, ["health"])
    assert result.legal_hold and effective_retention_days(result) is None
    assert not is_at_least_as_strict(HandlingPolicy(), held)
    with pytest.raises(ValueError, match="unknown"):
        tighten(HandlingPolicy(), ["gossip"])


def test_sensitive_vocabulary_carries_labels(registry):
    assert registry.sensitivity_for("has_attribute", "contact_channel") == {"contact"}
    assert registry.sensitivity_for("has_attribute", "preferred_contact_time") == {"contact"}
    assert registry.sensitivity_for("citizen_of") == {"identity"}
    assert registry.sensitivity_for("resides_in") == {"location"}
    assert registry.sensitivity_for("likes") == frozenset()
    full = load_registry(["memspine.core", "memspine.proposed"])
    for name in ("has_condition", "allergic_to", "intolerant_of", "takes_medication"):
        assert "health" in full.predicates[name].sensitivity
    assert "financial" in full.predicates["paid"].sensitivity


# ---- RM-53 ------------------------------------------------------------------


def _pack(**overrides):
    raw = {
        "schema_version": 1,
        "pack_id": "acme.legal",
        "pack_version": 1,
        "source": "config",
        "status": "approved",
        "default_active": False,
        "description": "custom vocabulary",
        "dependencies": [
            {"pack_id": "memspine.core", "digest": BUILTIN_PACK_DIGESTS["memspine.core"]}
        ],
        "predicates": [
            load_builtin_registry().predicates["cites"].model_dump(mode="json")
            | {"name": "overrules", "inverse_view": "overruled_by"}
        ],
        "properties": [],
        "roles": [],
        "withdrawn": [],
    }
    raw.update(overrides)
    return raw


def test_builtin_packs_are_pinned_and_proposed_pack_inactive():
    core, proposed = load_builtin_packs()
    assert core.digest == BUILTIN_PACK_DIGESTS["memspine.core"] and core.default_active
    assert proposed.status == "proposed" and not proposed.default_active
    assert "pending owner confirmation" in proposed.description
    assert "has_condition" not in load_builtin_registry().predicates
    full = load_registry(["memspine.core", "memspine.proposed"])
    assert (len(full.predicates), len(full.properties), len(full.roles)) == (105, 76, 35)
    assert full.owner("predicate", "purchased") == "memspine.proposed"
    assert full.owner("predicate", "cites") == "memspine.core"


def test_config_pack_loads_with_pin_and_rejects_bad_pin(tmp_path):
    manifest = PackManifest.model_validate_json(json.dumps(_pack()))
    path = tmp_path / "vocab.json"
    path.write_text(json.dumps(_pack()), encoding="utf-8")
    loaded = load_config_pack(path, expected_digest=manifest.digest)
    registry = load_registry(["memspine.core"], [loaded])
    assert registry.owner("predicate", "overrules") == "acme.legal"
    with pytest.raises(ValueError, match="pin"):
        load_config_pack(path, expected_digest="0" * 64)
    with pytest.raises(ValueError, match="declares source"):
        parse_pack(json.dumps(_pack()), source="plugin", expected_digest=manifest.digest)
    with pytest.raises(ValueError, match="memspine itself"):
        load_plugin_pack("memspine.semantics", "catalog.json", expected_digest=manifest.digest)


def test_digest_is_order_independent():
    pack = _pack(
        predicates=[
            load_builtin_registry().predicates[n].model_dump(mode="json")
            | {"name": f"x_{n}", "inverse_view": None}
            for n in ("cites", "about")
        ]
    )
    a = PackManifest.model_validate_json(json.dumps(pack))
    b = PackManifest.model_validate_json(
        json.dumps(pack | {"predicates": pack["predicates"][::-1]})
    )
    assert compute_pack_digest(a) == compute_pack_digest(b)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"pack_id": "memspine.mine"}, "reserved"),
        ({"source": "builtin"}, "reserved"),
        ({"status": "proposed", "default_active": True}, "proposed"),
        (
            {
                "withdrawn": [
                    {
                        "kind": "predicate",
                        "name": "cites",
                        "withdrawn_on": "2026-10-22",
                        "reason": "x",
                    }
                ]
            },
            "own definitions",
        ),
    ],
)
def test_manifest_contract_violations(overrides, match):
    with pytest.raises(ValidationError, match=match):
        PackManifest.model_validate_json(json.dumps(_pack(**overrides)))


def test_pack_cannot_enable_extraction():
    pred = _pack()["predicates"][0] | {"extraction_rule_ids": ["rule.x"]}
    with pytest.raises(ValidationError, match="extraction"):
        PackManifest.model_validate_json(json.dumps(_pack(predicates=[pred])))


def test_conflicts_and_missing_dependencies_rejected():
    clash = _pack()["predicates"][0] | {"name": "cites", "inverse_view": None}
    pack = PackManifest.model_validate_json(json.dumps(_pack(predicates=[clash])))
    with pytest.raises(ValueError, match="conflicts"):
        load_registry(["memspine.core"], [pack])
    lonely = PackManifest.model_validate_json(json.dumps(_pack()))
    with pytest.raises(ValueError, match="pinned digest"):
        SemanticRegistry.from_packs([lonely])
    stale = _pack(dependencies=[{"pack_id": "memspine.core", "digest": "f" * 64}])
    with pytest.raises(ValueError, match="pinned digest"):
        load_registry(["memspine.core"], [PackManifest.model_validate_json(json.dumps(stale))])
    uses_core_role = _pack()["predicates"][0] | {"role_names": ["topic"]}
    undeclared = _pack(dependencies=[], predicates=[uses_core_role])
    with pytest.raises(ValueError, match="outside its dependencies"):
        load_registry(["memspine.core"], [PackManifest.model_validate_json(json.dumps(undeclared))])


def test_withdrawal_stops_new_work_but_keeps_history(registry):
    v1 = PackManifest.model_validate_json(json.dumps(_pack()))
    v2 = PackManifest.model_validate_json(
        json.dumps(
            _pack(
                pack_version=2,
                withdrawn=[
                    {
                        "kind": "predicate",
                        "name": "overrules",
                        "withdrawn_on": "2026-10-22",
                        "reason": "replaced",
                    }
                ],
            )
        )
    )
    check_pack_upgrade(v1, v2)
    reg = load_registry(["memspine.core"], [v2])
    assert "overrules" in reg.predicates and reg.is_withdrawn("predicate", "overrules")
    with pytest.raises(ValueError, match="withdrawn"):
        reg.validate_compatibility(
            "overrules", subject_type="document", value_kind="entity", object_entity_type="work"
        )
    with pytest.raises(ValueError, match="remove"):
        check_pack_upgrade(v1, v1.model_copy(update={"pack_version": 2, "predicates": ()}))
    with pytest.raises(ValueError, match="reverted"):
        check_pack_upgrade(v2, v1.model_copy(update={"pack_version": 3}))
    with pytest.raises(ValueError, match="increase"):
        check_pack_upgrade(v2, v1)
    changed = v1.predicates[0].model_copy(update={"subject_types": ("document",)})
    with pytest.raises(ValueError, match="changed"):
        check_pack_upgrade(v1, v1.model_copy(update={"pack_version": 2, "predicates": (changed,)}))


# ---- vocabulary families ----------------------------------------------------

SELECTED = {
    "committed_to",
    "due_by",
    "requested_of",
    "decided",
    "works_on",
    "blocked_by",
    "cites",
    "about",
    "derived_from",
    "revision_of",
    "partner_of",
    "relative_of",
    "colleague_of",
    "acquainted_with",
    "mentor_of",
    "caregiver_of",
    "neighbour_of",
    "instructed",
    "restricted_by",
    "has_goal",
    "wishes",
    "citizen_of",
    "speaks",
    "affiliated_with",
}
SELECTED_KEYS = {
    "priority",
    "progress",
    "outcome",
    "locator",
    "publisher",
    "licence",
    "kinship_type",
    "relationship_type",
    "directive_polarity",
    "tone",
    "detail_level",
    "rule_strength",
    "target_value",
    "target_date",
    "occupation",
    "pronouns",
    "nationality",
    "contact_channel",
    "preferred_contact_time",
}


def test_selected_families_registered_without_extraction(registry):
    assert registry.predicates.keys() >= SELECTED
    assert registry.properties.keys() >= SELECTED_KEYS
    assert all(not registry.predicates[n].extraction_rule_ids for n in SELECTED)


def test_commitment_requires_obligor_and_action(registry):
    registry.validate_compatibility(
        "committed_to",
        subject_type="agent",
        value_kind="entity",
        object_entity_type="document",
        role_names=("obligor", "action", "beneficiary"),
        qualifiers={"priority": QualifierValue(kind="text", text="high")},
    )
    with pytest.raises(ValueError, match="role"):
        registry.validate_compatibility(
            "committed_to", subject_type="agent", value_kind="text", role_names=("obligor",)
        )
    with pytest.raises(ValueError, match="not registered"):
        registry.validate_compatibility(
            "committed_to",
            subject_type="agent",
            value_kind="text",
            role_names=("obligor", "action"),
            qualifiers={"locator": QualifierValue(kind="text", text="p. 4")},
        )


def test_instruction_is_directive_with_required_polarity(registry):
    spec = registry.predicates["instructed"]
    assert spec.statement_kind == "directive"
    kwargs = {
        "subject_type": "person",
        "value_kind": "text",
        "role_names": ("instructor", "addressee", "action", "condition"),
    }
    registry.validate_compatibility(
        "instructed",
        qualifiers={
            "directive_polarity": QualifierValue(kind="text", text="must_not"),
            "tone": QualifierValue(kind="text", text="formal"),
        },
        **kwargs,
    )
    with pytest.raises(ValueError, match="directive_polarity"):
        registry.validate_compatibility("instructed", **kwargs)
    with pytest.raises(ValueError, match="allowed values"):
        registry.validate_compatibility(
            "instructed",
            qualifiers={"directive_polarity": QualifierValue(kind="text", text="should")},
            **kwargs,
        )


def test_goal_targets_are_typed(registry):
    registry.validate_compatibility(
        "has_goal",
        subject_type="person",
        value_kind="text",
        qualifiers={
            "target_value": QualifierValue(kind="number", amount=Decimal(10)),
            "target_date": QualifierValue(kind="temporal", text="tref-1"),
        },
    )
    with pytest.raises(ValueError, match="value kind"):
        registry.validate_compatibility(
            "has_goal",
            subject_type="person",
            value_kind="text",
            qualifiers={"target_date": QualifierValue(kind="text", text="soon")},
        )
    with pytest.raises(ValueError, match="Decimal"):
        registry.validate_compatibility(
            "has_goal",
            subject_type="person",
            value_kind="text",
            qualifiers={"target_value": QualifierValue(kind="number", amount=Decimal("Infinity"))},
        )


def test_social_family_symmetry_and_mixed_objects(registry):
    for name in ("partner_of", "relative_of", "colleague_of", "acquainted_with", "neighbour_of"):
        assert registry.predicates[name].symmetric
    assert registry.predicates["mentor_of"].inverse_view == "mentee_of"
    assert registry.predicates["caregiver_of"].inverse_view == "cared_for_by"
    registry.validate_compatibility(
        "caregiver_of", subject_type="person", value_kind="entity", object_entity_type="species"
    )
    with pytest.raises(ValueError):
        registry.validate_compatibility(
            "partner_of", subject_type="person", value_kind="entity", object_entity_type="object"
        )
    registry.validate_compatibility(
        "relative_of",
        subject_type="person",
        value_kind="entity",
        object_entity_type="person",
        qualifiers={"kinship_type": QualifierValue(kind="text", text="cousin")},
    )


def test_contact_keys_are_subject_attributes_not_qualifiers(registry):
    registry.validate_compatibility(
        "has_attribute", subject_type="person", value_kind="text", attribute_key="pronouns"
    )
    with pytest.raises(ValueError, match="property"):
        registry.validate_compatibility(
            "has_attribute", subject_type="person", value_kind="text", attribute_key="priority"
        )


def test_proposed_families_are_attributed_aligned_and_possession_aware():
    full = load_registry(["memspine.core", "memspine.proposed"])
    for name in ("feels", "believes", "values", "worried_about"):
        assert full.predicates[name].statement_kind == "attributed"
    assert "tool_output" in full.predicates["attempted"].allowed_source_origins
    assert full.predicates["attempted"].lifecycle
    assert full.predicates["lost"].affects_state == "owns"
    assert full.predicates["purchased"].event_family == full.predicates["sold"].event_family
    full.validate_compatibility(
        "communicated_with",
        subject_type="person",
        value_kind="entity",
        object_entity_type="person",
        qualifiers={"speech_act": QualifierValue(kind="text", text="apologise")},
    )
    bad = full.predicates["lost"].model_copy(update={"affects_state": "likes_x"})
    with pytest.raises(ValueError, match="state"):
        SemanticRegistry([bad], [], [])
