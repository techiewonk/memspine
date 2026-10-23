"""RM-91: topical domains from four sources, with the source recorded."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from memspine.config.schema import MemspineConfig
from memspine.semantics import PackManifest, SemanticRegistry, load_builtin_registry, load_registry
from memspine.semantics.domains import (
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
    assert core.predicates["resides_in"].domain == "location/residence"
    assert core.predicates["employed_by"].domain == "work"
    assert core.properties["price"].domain == "finance"
    proposed = load_registry(["memspine.core", "memspine.proposed"])
    assert proposed.predicates["has_condition"].domain == "health/condition"


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
    assert got is not None and (got.domain, got.source) == ("finance", "property")
    got = resolve_domain(predicate="resides_in", registry=registry)
    assert got is not None and (got.domain, got.source) == ("location/residence", "predicate")
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
