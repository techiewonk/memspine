"""Tier base classes, extra-field round trips, current schema validation, reserved names."""

from __future__ import annotations

from typing import ClassVar

import pytest

from memspine.schemas import (
    DurableModel,
    InternalModel,
    NewerThanKnownError,
    PublicModel,
    SchemaValidationError,
    UnknownKindError,
    UnsupportedSchemaVersionError,
    reserved_hits,
    validate_payload,
    versioning,
)
from memspine.schemas.steps.shared import ExtractionItem, Scope


@pytest.fixture()
def clean_registry():
    saved = dict(versioning._DECLARED)
    versioning.reset_registry()
    yield
    versioning.reset_registry()
    versioning._DECLARED.update(saved)


# --- extra fields round-trip on every tier (S1-3) --------------------------------------------------------


class _D(DurableModel):
    __kind__: ClassVar[str] = "test_tier_durable"
    name: str


class _P(PublicModel):
    __semver__: ClassVar[str] = "1.2.3"
    name: str


class _I(InternalModel):
    name: str


@pytest.mark.parametrize("cls", [_D, _P, _I])
def test_extra_fields_round_trip(cls):
    payload = {"name": "x", "novel": {"a": [1, 2]}, "other": 3}
    obj = cls.model_validate(payload)
    dumped = obj.model_dump()
    assert dumped["novel"] == {"a": [1, 2]}
    assert dumped["other"] == 3
    again = cls.model_validate_json(obj.model_dump_json())
    assert again.model_dump() == dumped


def test_real_internal_model_keeps_extras():
    item = ExtractionItem.model_validate(
        {
            "text": "t",
            "kind": "fact",
            "mode": "asserted",
            "neg": False,
            "lines": [1],
            "span": "s",
            "zzz": 1,
        }
    )
    assert item.model_dump()["zzz"] == 1
    assert Scope.model_validate({"tenant": "t", "weird": True}).model_dump()["weird"] is True


def test_durable_has_schema_version_and_kind():
    assert _D.__kind__ == "test_tier_durable"
    assert _D(name="a").schema_version == 1
    assert versioning.latest_version("test_tier_durable") == 1


def test_public_has_semver():
    assert _P.__semver__ == "1.2.3"


# --- strict current-format validation ---------------------------------------------------------------


def test_current_payload_is_copied_without_transformation(clean_registry):
    versioning.declare_kind("rec", 1)
    payload = {"schema_version": 1, "unknown": {"values": [1]}}
    result = validate_payload("rec", payload)
    assert result == payload and result is not payload
    result["unknown"]["values"].append(2)
    assert payload["unknown"]["values"] == [1]


def test_missing_invalid_and_future_schema_versions_are_rejected(clean_registry):
    versioning.declare_kind("rec", 1)
    for value in (None, True, "1", 0, -1):
        with pytest.raises(SchemaValidationError):
            validate_payload("rec", {"schema_version": value})
    with pytest.raises(SchemaValidationError):
        validate_payload("rec", {})
    with pytest.raises(NewerThanKnownError):
        validate_payload("rec", {"schema_version": 2})
    with pytest.raises(UnknownKindError):
        validate_payload("unknown", {"schema_version": 1})


def test_earlier_payload_is_rejected_without_transformation(clean_registry):
    versioning.declare_kind("rec", 2)
    with pytest.raises(UnsupportedSchemaVersionError):
        validate_payload("rec", {"schema_version": 1})


def test_durable_subclass_declares_current_kind(clean_registry):
    class _X(DurableModel):
        __kind__: ClassVar[str] = "declared_kind"

    assert versioning.latest_version("declared_kind") == 1
    assert validate_payload("declared_kind", {"schema_version": 1}) == {"schema_version": 1}


# --- reserved names (SESSIONS_01_02 condition 1) -------------------------------------------------------------


def test_reserved_exact_and_id_suffix():
    payload = {"text": "t", "tenant": "a", "user_id": "u", "fine": 1}
    assert reserved_hits(payload) == ["tenant", "user_id"]


def test_reserved_nested_dicts_and_lists():
    payload = {
        "items": [{"text": "a", "recorded_at": 1}, {"nested": {"lineage": []}}],
        "ok": {"fingerprint": "f"},
    }
    assert reserved_hits(payload) == [
        "items[0].recorded_at",
        "items[1].nested.lineage",
        "ok.fingerprint",
    ]


def test_reserved_no_false_hit_on_lookalikes():
    payload = {
        "identity_text": "x",
        "idempotent": 1,
        "valid": True,
        "userland": 1,
        "statuses": 2,
        "tenantId": 1,
        "provenance_note": "x",
        "text": "id and user are values, not keys",
    }
    assert reserved_hits(payload) == []


def test_reserved_is_case_insensitive_and_covers_full_list():
    assert reserved_hits({"Tenant": 1, "USER_ID": 2}) == ["Tenant", "USER_ID"]
    for name in [
        "id",
        "scope",
        "tenant",
        "user",
        "trust",
        "provenance",
        "verified",
        "status",
        "epoch",
        "generation",
        "recorded_at",
        "lineage",
        "fingerprint",
    ]:
        assert reserved_hits({name: 1}) == [name]
    assert reserved_hits({"x_id": 1, "_id": 1}) == ["x_id", "_id"]
