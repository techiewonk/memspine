"""RM-91: topical domains from four sources, with the source recorded."""

from __future__ import annotations

import json
from importlib.resources import files
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from memspine.config.schema import MemspineConfig
from memspine.semantics import (
    FAMILY_DOMAINS,
    PackManifest,
    SemanticRegistry,
    load_builtin_packs,
    load_builtin_registry,
    load_registry,
)
from memspine.semantics.domains import (
    declared_domains,
    domain_covers,
    domain_in,
    resolve_domain,
    topic_hook,
    topic_tags,
)
from memspine.semantics.specs import PredicateSpec


def test_domain_covers_is_segment_bounded() -> None:
    assert domain_covers("health", "health/condition")
    assert domain_covers("health", "health")
    assert not domain_covers("health", "healthcare")
    assert not domain_in(None, ["health"])
    assert domain_in("work", ["health", "work"])


def test_builtin_vocabulary_declares_domains() -> None:
    core = load_builtin_registry()
    assert core.predicates["resides_in"].domain == "personal/residence"
    assert core.predicates["employed_by"].domain is None
    assert core.family_domain("predicate", "employed_by") == "work"
    assert core.family_domain("property", "price") == "finance"
    proposed = load_registry(["memspine.core", "memspine.proposed"])
    assert proposed.predicates["has_condition"].domain == "health/condition"


def test_every_builtin_definition_has_one_generic_family_domain() -> None:
    """RM-107 (ADR-094): every built-in predicate and property belongs to exactly one
    family; the family domain is from the fixed generic set; a definition's own domain is
    under it; built-in packs carry no top domain."""
    for pack in load_builtin_packs():
        assert pack.domain is None
        assert all(family.domain in FAMILY_DOMAINS for family in pack.families)
        for spec in (*pack.predicates, *pack.properties):
            kind = "predicate" if isinstance(spec, PredicateSpec) else "property"
            family = pack.family_of(kind, spec.name)
            assert family is not None and family.domain is not None, spec.name
            assert spec.domain is None or domain_covers(family.domain, spec.domain)
    full = load_registry(["memspine.core", "memspine.proposed"])
    assert full.family_domains <= set(FAMILY_DOMAINS)
    assert set(FAMILY_DOMAINS) <= set(declared_domains(full))


def _families_pack(families: list[dict[str, object]], **overrides: object) -> dict[str, object]:
    payload = json.loads(_custom_pack(None).model_dump_json())
    payload["families"] = families
    payload.update(overrides)
    return payload


def test_family_domain_validation() -> None:
    sport = {"name": "sport", "description": "sports", "predicates": ["plays_sport"]}
    # a custom family may omit its domain, and then falls back to the predicate/key defaults
    plain = PackManifest.model_validate_json(json.dumps(_families_pack([sport])))
    registry = load_registry(["memspine.core"], [plain])
    assert registry.family_of("predicate", "plays_sport") == "sport"
    assert resolve_domain(predicate="plays_sport", registry=registry) is None
    # an unknown family domain is rejected, in any pack
    with pytest.raises(ValidationError):
        PackManifest.model_validate_json(
            json.dumps(_families_pack([sport | {"domain": "leisure"}]))
        )
    with pytest.raises(ValidationError, match="unknown predicate"):
        PackManifest.model_validate_json(
            json.dumps(_families_pack([sport | {"predicates": ["nope"]}]))
        )
    with pytest.raises(ValidationError, match="two families"):
        PackManifest.model_validate_json(
            json.dumps(_families_pack([sport, sport | {"name": "again"}]))
        )
    pred = json.loads(_custom_pack(None).model_dump_json())["predicates"][0]
    with pytest.raises(ValidationError, match="outside family"):
        PackManifest.model_validate_json(
            json.dumps(
                _families_pack(
                    [sport | {"domain": "health"}], predicates=[pred | {"domain": "work/team"}]
                )
            )
        )
    # a declared family domain is the claim's domain (source ``family``)
    fam = PackManifest.model_validate_json(
        json.dumps(_families_pack([sport | {"domain": "health"}]))
    )
    got = resolve_domain(predicate="plays_sport", registry=load_registry(["memspine.core"], [fam]))
    assert got is not None and (got.domain, got.source) == ("health", "family")


def test_builtin_family_contract_is_enforced() -> None:
    core = json.loads(
        files("memspine.semantics").joinpath("catalog.json").read_text(encoding="utf-8")
    )
    PackManifest.model_validate_json(json.dumps(core))
    no_domain = json.loads(json.dumps(core))
    no_domain["families"][0]["domain"] = None
    with pytest.raises(ValidationError, match="declares no domain"):
        PackManifest.model_validate_json(json.dumps(no_domain))
    orphan = json.loads(json.dumps(core))
    orphan["families"][0]["predicates"] = orphan["families"][0]["predicates"][1:]
    with pytest.raises(ValidationError, match="belongs to no family"):
        PackManifest.model_validate_json(json.dumps(orphan))
    with pytest.raises(ValidationError, match="no top domain"):
        PackManifest.model_validate_json(json.dumps(core | {"domain": "personal"}))


def test_spec_domain_must_be_hierarchical_id() -> None:
    with pytest.raises(ValidationError):
        PredicateSpec(
            name="x_rel",
            version=1,
            subject_types=("person",),
            value_kinds=("entity",),
            semantics="relation",
            cardinality="many",
            transition_policy="append_only",
            allowed_source_origins=("raw_message",),
            domain="Health/",
        )


def _custom_pack(domain: str | None) -> PackManifest:
    pred = {
        "name": "plays_sport",
        "version": 1,
        "subject_types": ["person"],
        "value_kinds": ["text"],
        "semantics": "relation",
        "cardinality": "many",
        "transition_policy": "append_only",
        "allowed_source_origins": ["raw_message"],
    }
    payload = {
        "schema_version": 1,
        "pack_id": "acme.sports",
        "pack_version": 1,
        "source": "config",
        "status": "approved",
        "default_active": False,
        "description": "test pack",
        "domain": domain,
        "predicates": [pred],
    }
    return PackManifest.model_validate_json(json.dumps(payload))


def test_precedence_and_source_recording() -> None:
    registry = load_registry(["memspine.core"], [_custom_pack("leisure/sport")])
    # (c) a caller ingest tag wins over everything
    got = resolve_domain(tags=["domain:health/allergy"], predicate="resides_in", registry=registry)
    assert got is not None and (got.domain, got.source) == ("health/allergy", "ingest_tag")
    # (a) property key default beats the predicate default
    got = resolve_domain(predicate="has_attribute", attribute_key="price", registry=registry)
    assert got is not None and (got.domain, got.source) == ("finance", "family")
    got = resolve_domain(predicate="resides_in", registry=registry)
    assert got is not None and (got.domain, got.source) == ("personal/residence", "predicate")
    # (b) the pack's top domain when the predicate declares none
    got = resolve_domain(predicate="plays_sport", registry=registry)
    assert got is not None and (got.domain, got.source) == ("leisure/sport", "pack")
    # (d) GLiNER topic tag only when nothing else applies
    got = resolve_domain(tags=["gliner_topic:media/film"], predicate="knows", registry=registry)
    assert got is not None and (got.domain, got.source) == ("media/film", "gliner_topic")
    # nothing: unknown
    assert resolve_domain(predicate="knows", registry=registry) is None
    # a malformed caller tag is ignored, never coerced
    assert resolve_domain(tags=["domain:Bad Domain"], predicate="knows", registry=registry) is None


def test_pack_domain_changes_registry_digest() -> None:
    with_domain = SemanticRegistry.from_packs([_custom_pack("leisure")])
    without = SemanticRegistry.from_packs([_custom_pack(None)])
    assert with_domain.digest != without.digest
    assert with_domain.pack_domain("predicate", "plays_sport") == "leisure"
    assert without.pack_domain("predicate", "plays_sport") is None


def _with_topic(slot: object) -> SimpleNamespace:
    """A config stand-in carrying only ``model.extraction.topic`` (ADR-082)."""
    return SimpleNamespace(model=SimpleNamespace(extraction=SimpleNamespace(topic=slot)))


def test_topic_hook_is_off_unless_bound() -> None:
    calls: list[str] = []

    def stub(text: str) -> list[str]:
        calls.append(text)
        return ["health/condition", "Not Valid", "health/condition"]

    unbound = _with_topic(None)
    bound = _with_topic(SimpleNamespace(provider="gliner2"))
    assert topic_hook(unbound, stub) is None
    assert topic_hook(bound, None) is None
    tagger = topic_hook(bound, stub)
    assert tagger is stub
    assert topic_tags("I have asthma", tagger) == ["gliner_topic:health/condition"]
    assert topic_tags("I have asthma", None) == []
    assert calls == ["I have asthma"]


def test_gliner_topic_slot_is_accepted_by_config() -> None:
    runtime = {
        "provider_services": {
            "health_timeout_s": 5.0,
            "close_timeout_s": 10.0,
            "startup_timeout_s": None,
        }
    }
    cfg = MemspineConfig.model_validate(
        {
            "runtime": runtime,
            "model": {"extraction": {"topic": {"provider": "gliner2", "settings": {}}}},
        }
    )
    assert cfg.model.extraction.topic is not None
    assert cfg.model.extraction.topic.provider == "gliner2"
    assert topic_hook(cfg, lambda text: ["health"]) is not None
