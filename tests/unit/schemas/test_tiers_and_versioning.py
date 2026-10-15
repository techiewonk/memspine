"""Tier base classes, extra-field round trips, upcaster registry, reserved names."""

from __future__ import annotations

from typing import ClassVar

import pytest

from memspine.schemas import (
    DuplicateUpcasterError,
    DurableModel,
    InternalModel,
    NewerThanKnownError,
    PublicModel,
    UnknownKindError,
    UpcastError,
    UpcastGapError,
    reserved_hits,
    upcast,
    upcaster,
    versioning,
)
from memspine.schemas.steps.shared import ItemV1, Scope


@pytest.fixture()
def clean_registry():
    saved = (dict(versioning._UPCASTERS), dict(versioning._DECLARED))
    versioning.reset_registry()
    yield
    versioning.reset_registry()
    versioning._UPCASTERS.update(saved[0])
    versioning._DECLARED.update(saved[1])


# --- extra fields round-trip on every tier (S1-3) --------------------------------------------------------


class _D(DurableModel):
    kind: ClassVar[str] = "test_tier_durable"
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
    item = ItemV1.model_validate(
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
    assert _D.kind == "test_tier_durable"
    assert _D(name="a").schema_version == 1
    assert versioning.latest_version("test_tier_durable") == 1


def test_public_has_semver():
    assert _P.__semver__ == "1.2.3"


# --- upcasters (S1-2) --------------------------------------------------------------------------------------


def test_upcast_chain_applies_in_order_and_passes_unknown_fields(clean_registry):
    @upcaster("rec", 1)
    def _v1(p):
        p["a"] = p.get("a", 0) + 1
        return p

    @upcaster("rec", 2, consumes=("old",))
    def _v2(p):
        p = dict(p)
        p["new"] = p.pop("old")
        return p

    src = {"schema_version": 1, "a": 0, "old": "x", "mystery": {"k": [1]}}
    out = upcast("rec", src)
    assert out == {"schema_version": 3, "a": 1, "new": "x", "mystery": {"k": [1]}}
    assert src["schema_version"] == 1 and "old" in src  # input untouched


def test_upcast_current_version_is_identity_copy(clean_registry):
    @upcaster("rec", 1)
    def _v1(p):
        return p

    payload = {"schema_version": 2, "x": 1}
    out = upcast("rec", payload)
    assert out == payload and out is not payload


def test_upcast_gap(clean_registry):
    @upcaster("rec", 2)
    def _v2(p):
        return p

    with pytest.raises(UpcastGapError):
        upcast("rec", {"schema_version": 1})
    # starting above the gap still works
    assert upcast("rec", {"schema_version": 2})["schema_version"] == 3


def test_upcast_newer_than_known(clean_registry):
    @upcaster("rec", 1)
    def _v1(p):
        return p

    with pytest.raises(NewerThanKnownError):
        upcast("rec", {"schema_version": 3})


def test_duplicate_registration(clean_registry):
    @upcaster("rec", 1)
    def _a(p):
        return p

    with pytest.raises(DuplicateUpcasterError):

        @upcaster("rec", 1)
        def _b(p):
            return p


def test_unknown_kind_and_bad_version(clean_registry):
    with pytest.raises(UnknownKindError):
        upcast("nothing", {"schema_version": 1})

    @upcaster("rec", 1)
    def _a(p):
        return p

    for bad in ({}, {"schema_version": "1"}, {"schema_version": 0}, {"schema_version": True}):
        with pytest.raises(UpcastError):
            upcast("rec", bad)


def test_upcaster_may_not_silently_drop_unknown_fields(clean_registry):
    @upcaster("rec", 1)
    def _a(p):
        return {"schema_version": 1, "kept": p["kept"]}

    with pytest.raises(UpcastError, match="dropped fields"):
        upcast("rec", {"schema_version": 1, "kept": 1, "extra": 2})


def test_durable_subclass_declares_latest(clean_registry):
    class _X(DurableModel):
        kind: ClassVar[str] = "declared_kind"
        schema_version: int = 3

    assert versioning.latest_version("declared_kind") == 3
    with pytest.raises(UpcastGapError):
        upcast("declared_kind", {"schema_version": 1})
    assert upcast("declared_kind", {"schema_version": 3, "u": 1}) == {"schema_version": 3, "u": 1}


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
