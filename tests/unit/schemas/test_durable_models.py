# ruff: noqa: E501
"""Durable tier: frozen fixtures, round trips, upcaster pass-through and the semantic invariants (ER-32)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from memspine.schemas import collect_models, kind_id, latest_version, upcast
from memspine.schemas.durable import (
    AssertionEnvelope,
    AssertionKey,
    Assessment,
    ErasedIds,
    ErasureManifest,
    Evidence,
    Hole,
    Interval,
    JobLease,
    LineageEdge,
    OutboxRow,
    OwnerScope,
    Progress,
    RetentionBoundary,
    Span,
    Support,
    TemporalRef,
    TimeAxes,
    TurnRef,
    can_supersede,
    commit_allowed,
    inherit_trust,
    integration_outcome,
    is_tentative,
    occurrence_id,
    write_id,
)
from memspine.schemas.durable.assertion import Mention

FIXTURES = Path(__file__).parent / "fixtures" / "durable"
UTC = UTC


def u(*a: int) -> datetime:
    return datetime(*a, tzinfo=UTC)


def _durable_kinds():
    return [c for c in collect_models()["durable"] if kind_id(c)]


# --- fixtures ----------------------------------------------------------------------------------------------


def test_every_durable_kind_has_a_frozen_fixture_per_version():
    kinds = _durable_kinds()
    assert {kind_id(c) for c in kinds} >= {
        "memory_event",
        "assertion_envelope",
        "lineage_edge",
        "outbox_row",
        "job_lease",
        "progress",
        "erasure_manifest",
        "retention_boundary",
    }
    for cls in kinds:
        kind = kind_id(cls)
        assert cls.model_fields["schema_version"].default == latest_version(kind)
        for version in range(1, latest_version(kind) + 1):
            assert (FIXTURES / f"{kind}.v{version}.json").is_file(), (
                f"missing fixture {kind} v{version}"
            )


@pytest.mark.parametrize("cls", _durable_kinds(), ids=lambda c: kind_id(c))
def test_fixture_round_trips_unchanged(cls):
    path = FIXTURES / f"{kind_id(cls)}.v1.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    assert upcast(kind_id(cls), raw) == raw  # v1 is current: the chain is a no-op
    obj = cls.model_validate(raw)
    assert obj.model_dump(mode="json") == raw
    again = cls.model_validate_json(obj.model_dump_json())
    assert again.model_dump(mode="json") == raw


def test_unknown_field_fixture_keeps_the_field_verbatim():
    raw = json.loads((FIXTURES / "assertion_envelope.v1.json").read_text(encoding="utf-8"))
    assert raw["novel_field"] == {"added_by": "a-future-release", "n": [1, 2]}
    obj = AssertionEnvelope.model_validate(raw)
    assert obj.model_dump(mode="json")["novel_field"] == raw["novel_field"]
    assert upcast("assertion_envelope", raw)["novel_field"] == raw["novel_field"]


# --- temporal (S6) -----------------------------------------------------------------------------------------


def test_exact_date_with_basis_none_is_rejected():
    with pytest.raises(ValidationError, match="basis 'none'"):
        TemporalRef(at=u(2026, 1, 1), precision="day", basis="none")
    with pytest.raises(ValidationError):
        TemporalRef(at=u(2026, 1, 1), precision="unknown", basis="explicit")


def test_phrase_without_anchor_stays_undated():
    ref = TemporalRef(text="last spring")
    assert ref.at is None and ref.basis == "none" and ref.precision == "unknown"


def test_precision_basis_consistency():
    with pytest.raises(ValidationError):
        TemporalRef(precision="year")  # precision without a date
    with pytest.raises(ValidationError):
        TemporalRef(basis="source_time", at=u(2026, 5, 1), precision="month")  # no anchor
    with pytest.raises(ValidationError):
        TemporalRef(basis="explicit")  # needs a date
    with pytest.raises(ValidationError):
        TemporalRef(approximate=True)
    ok = TemporalRef(
        at=u(2026, 5, 1),
        precision="month",
        approximate=True,
        basis="source_time",
        anchor=u(2026, 6, 3),
    )
    assert ok.approximate


def test_relation_needs_basis_relation_and_anchor_to_resolve():
    rel = {"relation": "after", "target_assertion_id": "rec-9"}
    unresolved = TemporalRef(text="after the move", basis="relation", relation=rel)
    assert unresolved.at is None
    with pytest.raises(ValidationError):
        TemporalRef(basis="relation", relation=rel, at=u(2026, 1, 1), precision="day")  # no anchor
    with pytest.raises(ValidationError):
        TemporalRef(basis="explicit", relation=rel, at=u(2026, 1, 1), precision="day")
    with pytest.raises(ValidationError):
        TemporalRef(basis="relation")


def test_duration_and_interval():
    ref = TemporalRef(text="for three weeks", duration={"value": 3, "unit": "week"})
    assert ref.duration.unit == "week"
    with pytest.raises(ValidationError):
        TemporalRef(duration={"value": 0, "unit": "week"})
    a = TemporalRef(at=u(2026, 5, 1), precision="month", basis="explicit")
    b = TemporalRef(at=u(2026, 4, 1), precision="month", basis="explicit")
    with pytest.raises(ValidationError, match="before its start"):
        Interval(start=a, end=b)
    with pytest.raises(ValidationError):
        Interval()
    assert Interval(start=a).precision == "month"
    assert TimeAxes().event_precision == "unknown"


def test_naive_datetimes_are_refused():
    with pytest.raises(ValidationError):
        TemporalRef(at=datetime(2026, 1, 1), precision="day", basis="explicit")


# --- evidence (S4) -----------------------------------------------------------------------------------------

TURN = TurnRef(source_id="src-1", source_version=1, seg_index=0)


def test_support_and_binding_evidence_must_cite_a_turn():
    with pytest.raises(ValidationError, match="must cite a turn"):
        Evidence(role="support", source_family="f")
    with pytest.raises(ValidationError, match="must cite a turn"):
        Evidence(role="binding", source_family="f")
    ctx = Evidence(role="context", source_family="f")
    assert not ctx.counts_as_evidence
    assert Evidence(role="binding", turn=TURN, source_family="f").counts_as_evidence


def test_span_order_and_family_required():
    with pytest.raises(ValidationError):
        Span(start=5, end=2)
    with pytest.raises(ValidationError):
        Evidence(role="context", source_family="")


# --- assessment (S5) ---------------------------------------------------------------------------------------


def _assess(trust=0.5, modality="asserted", binding="bound"):
    return Assessment(source_trust=trust, binding=binding, modality=modality)


def test_can_supersede_needs_all_three():
    cur = _assess(0.6)
    assert can_supersede(_assess(0.6), cur)
    assert can_supersede(_assess(0.9), cur)
    assert not can_supersede(_assess(0.5), cur)  # lower trust
    assert not can_supersede(_assess(0.9, modality="planned"), cur)
    assert not can_supersede(_assess(0.9, modality="hypothetical"), cur)
    assert not can_supersede(_assess(0.9, modality="reported"), cur)
    assert not can_supersede(_assess(0.9, binding="partly"), cur)
    assert not can_supersede(_assess(0.9, binding="unbound"), cur)


def test_integration_outcome_and_tentative():
    cur = _assess(0.6)
    assert integration_outcome(_assess(0.8), cur) == "supersede"
    assert integration_outcome(_assess(0.4), cur) == "contest"  # lower trust: both kept
    assert integration_outcome(_assess(0.9, modality="planned"), cur) == "coexist_tentative"
    assert is_tentative(_assess(modality="hypothetical"))
    assert is_tentative(_assess(binding="unbound"))
    assert not is_tentative(_assess())


def test_can_supersede_accepts_envelopes():
    env = AssertionEnvelope.model_validate_json(
        (FIXTURES / "assertion_envelope.v1.json").read_text(encoding="utf-8")
    )
    assert can_supersede(env, env)
    with pytest.raises(TypeError):
        can_supersede(object(), env)


def test_support_counts_agree_and_hint_is_bounded():
    with pytest.raises(ValidationError):
        Support(independent=2, families=["a", "a"])
    with pytest.raises(ValidationError):
        Assessment(
            source_trust=0.5, binding="bound", modality="asserted", model_confidence_hint=1.5
        )
    with pytest.raises(ValidationError):
        Assessment(source_trust=0.5, binding="bound", modality="maybe")


def test_inherit_trust_is_bounded_by_parents():
    assert inherit_trust([0.9, 0.4], 0.8) == 0.4
    assert inherit_trust([], 0.8) == 0.8


# --- assertion envelope (ER-32) ----------------------------------------------------------------------------


def _envelope(**over):
    scope = OwnerScope(tenant="t", owner="o")
    key = AssertionKey(src="a", rel="likes", dst="b", kind="state")
    base = {
        "write_id": write_id(scope, "c", 0, "x"),
        "assertion_key": key,
        "occurrence_id": occurrence_id("e", ["s#0"], key),
        "scope": scope,
        "text": "A likes B.",
        "kind": "state",
        "evidence": [Evidence(role="support", turn=TURN, source_family="spk")],
        "assessment": _assess(),
        "src_class": "user_stated",
    }
    base.update(over)
    return AssertionEnvelope(**base)


def test_asserted_item_requires_support_evidence():
    assert _envelope()
    ctx = Evidence(role="context", source_family="spk")
    bind = Evidence(role="binding", turn=TURN, source_family="spk")
    with pytest.raises(ValidationError, match="at least one support or binding"):
        _envelope(evidence=[ctx])
    with pytest.raises(ValidationError, match="requires support evidence"):
        _envelope(evidence=[bind])
    with pytest.raises(ValidationError, match="at least one support or binding"):
        _envelope(evidence=[])
    # a non-asserted item still needs a citation, but binding alone suffices
    assert _envelope(evidence=[bind], assessment=_assess(modality="planned"))


def test_derived_envelope_may_cite_through_lineage():
    env = _envelope(evidence=[], src_class="derived", parents=["rec-1"])
    assert env.parents == ["rec-1"]
    with pytest.raises(ValidationError):
        _envelope(evidence=[], src_class="derived", parents=[])


def test_time_precision_must_match_event_precision():
    month = TimeAxes(
        event=Interval(start=TemporalRef(at=u(2026, 5, 1), precision="month", basis="explicit"))
    )
    assert _envelope(
        time=month,
        assessment=Assessment(
            source_trust=0.5, binding="bound", modality="asserted", time_precision="month"
        ),
    )
    with pytest.raises(ValidationError, match="disagrees"):
        _envelope(time=month)
    with pytest.raises(ValidationError, match="disagrees"):
        _envelope(
            assessment=Assessment(
                source_trust=0.5, binding="bound", modality="asserted", time_precision="day"
            )
        )


def test_reserved_names_are_never_stored_as_extras():
    assert _envelope(extras={"mood": "calm"})
    with pytest.raises(ValidationError, match="reserved"):
        _envelope(extras={"trust": 1.0})
    with pytest.raises(ValidationError, match="reserved"):
        _envelope(extras={"nested": [{"user_id": "x"}]})


def test_mention_ids_distinct_and_exposed():
    a = Mention(surface="A", mention_id="m1")
    b = Mention(surface="B", mention_id="m2")
    assert _envelope(subject=a, object=b).mention_ids == ["m1", "m2"]
    with pytest.raises(ValidationError, match="distinct"):
        _envelope(subject=a, object=Mention(surface="B", mention_id="m1"))


# --- identity (S3) -----------------------------------------------------------------------------------------


def test_write_id_collapses_retries_and_separates_repeats():
    s = OwnerScope(tenant="t", owner="o")
    first = write_id(s, "conv-1", 3, "ok")
    assert write_id(s, "conv-1", 3, "ok") == first  # same turn sent twice
    assert write_id(s, "conv-2", 3, "ok") != first  # same words, later session
    assert write_id(s, "conv-1", 4, "ok") != first
    assert write_id(OwnerScope(tenant="t", owner="p"), "conv-1", 3, "ok") != first
    t = u(2026, 1, 1, 10)
    assert write_id(s, "conv-1", t, "ok") == write_id(s, "conv-1", t, "ok")
    assert write_id(s, "conv-1", t, "ok") != first
    with pytest.raises(ValueError):
        write_id(s, "conv-1", datetime(2026, 1, 1), "ok")


def test_occurrence_id_is_idempotent_and_order_free():
    key = AssertionKey(src="a", rel="r", dst="b", kind="state")
    one = occurrence_id("evt", ["t2", "t1"], key)
    assert occurrence_id("evt", ["t1", "t2", "t1"], key) == one
    assert occurrence_id("evt", ["t1", "t3"], key) != one  # later statement: new occurrence
    assert occurrence_id("evt2", ["t1", "t2"], key) != one
    assert occurrence_id("evt", ["t1", "t2"], key.model_copy(update={"dst": "c"})) != one
    with pytest.raises(ValueError):
        occurrence_id("evt", [], key)


def test_scope_is_tenant_plus_owner_only():
    assert set(OwnerScope.model_fields) == {"tenant", "owner", "owner_kind"}
    assert "session_id" not in OwnerScope.model_fields


# --- jobs (S8) ---------------------------------------------------------------------------------------------


def test_leased_row_needs_lease_fields_and_epoch():
    scope = OwnerScope(tenant="t", owner="o")
    with pytest.raises(ValidationError, match="leased row"):
        OutboxRow(id="1", scope=scope, kind="derivation", status="leased", lease_owner="w")
    with pytest.raises(ValidationError):
        OutboxRow(id="1", scope=scope, kind="nonsense")
    assert OutboxRow(id="1", scope=scope, kind="derivation").status == "pending"


def test_stale_epoch_commit_is_refused():
    assert commit_allowed(4, 4)
    assert not commit_allowed(3, 4)


def test_lease_times_and_epoch():
    scope = OwnerScope(tenant="t", owner="o")
    kw = {
        "scope": scope,
        "job_id": "j",
        "owner": "w",
        "epoch": 1,
        "acquired_at": u(2026, 1, 1, 1),
        "heartbeat_at": u(2026, 1, 1, 2),
        "lease_until": u(2026, 1, 1, 3),
    }
    assert JobLease(**kw)
    with pytest.raises(ValidationError):
        JobLease(**{**kw, "epoch": 0})
    with pytest.raises(ValidationError):
        JobLease(**{**kw, "lease_until": u(2026, 1, 1, 2)})


def test_progress_holes():
    scope = OwnerScope(tenant="t", owner="o")
    hole = {"offset": 3, "reason": "raised", "attempts": 1, "last_error": "boom"}
    p = Progress(scope=scope, job_kind="x", watermark=5, holes=[hole])
    assert not p.complete and len(p.open_holes) == 1
    waived = Progress(scope=scope, job_kind="x", watermark=5, holes=[{**hole, "waived": True}])
    assert waived.complete
    with pytest.raises(ValidationError, match="beyond the watermark"):
        Progress(scope=scope, job_kind="x", watermark=2, holes=[hole])
    with pytest.raises(ValidationError, match="duplicate"):
        Progress(scope=scope, job_kind="x", watermark=5, holes=[hole, hole])
    with pytest.raises(ValidationError, match="sorted"):
        Progress(scope=scope, job_kind="x", watermark=9, holes=[{**hole, "offset": 8}, hole])
    assert Hole(offset=0, reason="malformed").attempts == 0


# --- erasure and retention (S11) ---------------------------------------------------------------------------


def test_erasure_manifest_holds_ids_only():
    scope = OwnerScope(tenant="t", owner="o")
    kw = {
        "manifest_id": "m",
        "scope": scope,
        "erasure_generation": 1,
        "erased_at": u(2026, 1, 1),
        "ids": ErasedIds(record_ids=["r"]),
    }
    assert ErasureManifest(**kw)
    with pytest.raises(ValidationError, match="no content"):
        ErasureManifest(**kw, content="the erased text")
    with pytest.raises(ValidationError, match="no content"):
        ErasureManifest(**kw, details={"snippet": "x"})
    with pytest.raises(ValidationError, match="at least one id"):
        ErasureManifest(**{**kw, "ids": ErasedIds()})
    with pytest.raises(ValidationError):
        ErasureManifest(**{**kw, "erasure_generation": 0})


def test_retention_boundary_modes():
    scope = OwnerScope(tenant="t", owner="o")
    assert RetentionBoundary(scope=scope, mode="full", rebuild="full")
    assert RetentionBoundary(scope=scope, mode="ephemeral", rebuild="unavailable")
    with pytest.raises(ValidationError, match="implies rebuild"):
        RetentionBoundary(scope=scope, mode="rolling", rebuild="full", boundary_seq=1)
    with pytest.raises(ValidationError, match="needs a boundary"):
        RetentionBoundary(scope=scope, mode="rolling", rebuild="limited")
    with pytest.raises(ValidationError, match="keeps lineage"):
        RetentionBoundary(
            scope=scope, mode="rolling", rebuild="limited", boundary_seq=1, lineage_kept=False
        )
    with pytest.raises(ValidationError, match="no boundary"):
        RetentionBoundary(scope=scope, mode="full", rebuild="full", boundary_seq=3)


def test_lineage_edge_rules():
    assert LineageEdge(child="a", parent="b", role="merge_member")
    with pytest.raises(ValidationError, match="own parent"):
        LineageEdge(child="a", parent="a", role="support")
    with pytest.raises(ValidationError):
        LineageEdge(child="a", parent="b", role="evidence")  # split into support/binding


@pytest.mark.parametrize("modality", ["planned", "hypothetical", "reported", "question"])
def test_negated_flag_allowed_on_tentative_modalities(modality: str) -> None:
    a = Assessment(source_trust=0.5, binding="bound", modality=modality, negated=True)
    assert a.negated is True
    assert Assessment(source_trust=0.5, binding="bound", modality=modality).negated is False


@pytest.mark.parametrize("modality", ["asserted", "negated"])
def test_negated_flag_invalid_with_asserted_or_negated(modality: str) -> None:
    with pytest.raises(ValidationError, match="negated=True is invalid"):
        Assessment(source_trust=0.5, binding="bound", modality=modality, negated=True)


def test_change_hint_defaults_false_on_envelope() -> None:
    assert "change_hint" in AssertionEnvelope.model_fields
    assert AssertionEnvelope.model_fields["change_hint"].default is False
