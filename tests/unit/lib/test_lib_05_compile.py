"""LIB-05: ExtractionItem -> AssertionEnvelope compiler. Invented names only."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from memspine.derive.compile import (
    CompileContext,
    RejectionCode,
    WindowLine,
    compile_items,
    map_modality,
    normalise_kind,
)
from memspine.schemas.durable import OwnerScope, TemporalRef

SCOPE = OwnerScope(tenant="acme", owner="zorbi")
T0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
REC = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)


def window() -> dict[int, WindowLine]:
    return {
        1: WindowLine(
            "t-1", "src-1", 1, 0, "user", "zorbi", T0, "UTC", "She lives in Quillon now."
        ),
        2: WindowLine("t-2", "src-1", 1, 1, "assistant", "bot", T0, "UTC", "Noted, Quillon."),
        3: WindowLine("t-3", "src-1", 1, 2, "user", "zorbi", T0, "UTC", "I run on Tuesdays", True),
    }


def ctx(**kw: Any) -> CompileContext:
    base: dict[str, Any] = {
        "scope": SCOPE,
        "source_id": "src-1",
        "window": window(),
        "session_id": "sess-9",
        "recorded_at": REC,
        "epoch": 3,
        "erasure_generation": 2,
    }
    base.update(kw)
    return CompileContext(**base)


def item(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "s": "Mirela",
        "r": "lives in",
        "o": "Quillon",
        "text": "Mirela lives in Quillon",
        "kind": "state",
        "mode": "asserted",
        "neg": False,
        "lines": [1],
        "span": "lives in Quillon",
    }
    base.update(kw)
    return base


def one(**kw: Any):
    res = compile_items([item(**kw)], ctx())
    assert not res.rejections, res.rejections
    return res.envelopes[0]


def test_happy_path_maps_content_and_system_fields() -> None:
    env = one()
    assert env.text == "Mirela lives in Quillon"
    assert env.subject.surface == "Mirela" and env.subject.entity_id is None
    assert env.object.surface == "Quillon"
    assert env.attribute == "lives in"
    assert env.kind == "state"
    assert env.scope == SCOPE
    assert env.epoch == 3 and env.erasure_generation == 2
    assert env.provenance.session_id == "sess-9" and env.provenance.source_id == "src-1"
    assert env.time.recorded_at == REC and env.time.source_time == T0
    assert env.src_class == "user_stated" and env.assessment.source_trust == 0.8
    assert env.assessment.binding == "bound" and env.assessment.modality == "asserted"
    assert env.status == "active" and env.verified is False and env.record_id is None
    assert env.speaker == "zorbi"
    ev = env.evidence[0]
    assert ev.role == "support" and ev.turn.seg_index == 0 and ev.source_family == "zorbi"
    assert (ev.span.start, ev.span.end) == (4, 20) and ev.span.quote == "lives in Quillon"
    assert env.mention_ids[0] != env.mention_ids[1]


def test_context_line_becomes_binding_evidence_never_support() -> None:
    env = one(lines=[3, 1])
    assert [e.role for e in env.evidence] == ["support", "binding"]
    assert env.evidence[1].turn.seg_index == 2
    # occurrence ids ignore context lines (ER-11: context is not a new occurrence)
    assert env.occurrence_id == one(lines=[1]).occurrence_id


def test_context_only_rejected() -> None:
    res = compile_items([item(lines=[3])], ctx())
    assert res.rejections[0].code is RejectionCode.CONTEXT_ONLY and not res.envelopes


def test_uncited_and_unknown_alias() -> None:
    bad_missing = item()
    del bad_missing["lines"]
    res = compile_items([item(lines=[]), bad_missing, item(lines=[1, 99])], ctx())
    assert [r.code for r in res.rejections] == [
        RejectionCode.UNCITED,
        RejectionCode.UNCITED,
        RejectionCode.CITED_TURN_NOT_IN_WINDOW,
    ]


@pytest.mark.parametrize("name", ["tenant", "user_id", "Trust", "epoch", "recorded_at", "id"])
def test_reserved_names_reject_item_keep_siblings(name: str) -> None:
    res = compile_items([item(**{name: "x"}), item(text="Mirela likes tea")], ctx())
    assert len(res.envelopes) == 1 and res.envelopes[0].text == "Mirela likes tea"
    rej = res.rejections[0]
    assert rej.index == 0 and rej.code is RejectionCode.RESERVED_FIELD and rej.paths


def test_reserved_name_nested_rejects() -> None:
    res = compile_items([item(meta={"deep": [{"scope": "x"}]})], ctx())
    assert res.rejections[0].code is RejectionCode.RESERVED_FIELD
    assert res.rejections[0].paths == ("meta.deep[0].scope",)


def test_unknown_fields_go_to_extras_sorted() -> None:
    env = one(zeta=1, alpha={"k": [1, 2]})
    assert list(env.extras) == ["alpha", "zeta"]
    assert env.extras["alpha"] == {"k": [1, 2]}


def test_invalid_fields() -> None:
    cases = [
        item(mode="shouted"),
        item(neg="yes"),
        item(lines=["a"]),
        item(kind=3),
        {"text": "x", "lines": [1]},
        "not an object",
        item(chg="yes"),
    ]
    res = compile_items(cases, ctx())
    assert [r.code for r in res.rejections] == [RejectionCode.INVALID_FIELD] * len(cases)
    assert [r.index for r in res.rejections] == list(range(len(cases)))


def test_empty_statement() -> None:
    res = compile_items([item(text="   ")], ctx())
    assert res.rejections[0].code is RejectionCode.EMPTY_STATEMENT


def test_confidence_is_hint_only() -> None:
    env = one(conf=0.9)
    assert env.assessment.model_confidence_hint == 0.9 and "conf" not in env.extras
    assert env.assessment.source_trust == 0.8
    env2 = one(confidence="high")
    assert env2.assessment.model_confidence_hint is None and env2.extras["confidence"] == "high"
    assert one(conf=7).extras["conf"] == 7


@pytest.mark.parametrize(
    ("mode", "neg", "reported", "modality", "negated"),
    [
        ("asserted", False, False, "asserted", False),
        ("asserted", True, False, "negated", False),
        ("planned", False, False, "planned", False),
        ("planned", True, False, "planned", True),
        ("hoped", False, False, "hypothetical", False),
        ("hedged", True, False, "hypothetical", True),
        ("asked", False, False, "question", False),
        ("asked", True, False, "question", True),
        ("asserted", False, True, "reported", False),
        ("planned", True, True, "reported", True),
    ],
)
def test_modality_mapping(
    mode: str, neg: bool, reported: bool, modality: str, negated: bool
) -> None:
    assert map_modality(mode, neg, reported) == (modality, negated)
    kw: dict[str, Any] = {"mode": mode, "neg": neg}
    if reported:
        kw["reported_speaker"] = "Anna"
    a = one(**kw).assessment
    assert (a.modality, a.negated) == (modality, negated)


def test_planned_negated_uses_the_schema_fields_not_extras() -> None:
    env = one(mode="planned", neg=True)
    assert env.assessment.modality == "planned" and env.assessment.negated is True
    assert "neg" not in env.extras and "mode" not in env.extras


def test_reported_speaker_becomes_envelope_speaker() -> None:
    env = one(reported_speaker=" Anna ")
    assert env.speaker == "Anna" and env.assessment.modality == "reported"
    assert env.evidence[0].source_family == "zorbi"
    assert "reported_speaker" not in env.extras and "speaker" not in env.extras
    blank = one(reported_speaker="  ")
    assert blank.assessment.modality == "asserted" and blank.speaker == "zorbi"


def test_item_speaker_is_fallback_only() -> None:
    no_speaker = {1: WindowLine("t-1", "src-1", 1, 0, "user", None, T0, "UTC", "x")}
    env = compile_items([item(speaker="Pim", lines=[1])], ctx(window=no_speaker)).envelopes[0]
    assert env.speaker == "Pim"
    assert one(speaker="Pim").speaker == "zorbi"


def test_change_hint_is_a_field() -> None:
    assert one(chg=True).change_hint is True
    assert "chg" not in one(chg=True).extras
    assert one(chg=False).change_hint is False
    assert one().change_hint is False


def test_trust_from_weakest_support_role() -> None:
    env = one(lines=[1, 2])
    assert env.assessment.source_trust == 0.4 and env.src_class == "assistant_proposed"
    assert env.assessment.support.independent == 2
    assert one(lines=[2]).speaker == "bot"


def test_source_label_is_only_a_hint() -> None:
    env = one(source="user_confirmed", lines=[2])
    assert env.src_class == "assistant_proposed" and env.extras["source_hint"] == "user_confirmed"


def test_kind_registry_cap_and_refusal() -> None:
    capped = compile_items([item(kind="Weird Things")], ctx(kind_registry=lambda k: "other"))
    assert capped.envelopes[0].kind == "other"
    assert capped.notes[0].code == "kind_capped"
    refused = compile_items([item()], ctx(kind_registry=lambda k: None))
    assert refused.rejections[0].code is RejectionCode.KIND_CAPPED
    assert (
        normalise_kind("Events") == "event" and normalise_kind("Place Of Work") == "place_of_work"
    )
    assert normalise_kind("status") == "status"


def test_time_text_kept_by_default() -> None:
    env = one(when="last spring", until="next year")
    ev = env.time.event
    assert ev.start.text == "last spring" and ev.start.at is None and ev.start.basis == "none"
    assert env.assessment.time_precision == "unknown"
    assert one().time.event is None


def test_time_anchor_injected_and_failures_soft() -> None:
    calls: list[tuple[str, Any, Any]] = []

    def anchor(text: str, at: Any, tz: Any) -> TemporalRef:
        calls.append((text, at, tz))
        return TemporalRef(text=text, at=T0, precision="day", basis="source_time", anchor=at)

    env = compile_items([item(when="today")], ctx(time_anchor=anchor)).envelopes[0]
    assert calls == [("today", T0, "UTC")]
    assert env.assessment.time_precision == "day" == env.time.event_precision

    def boom(text: str, at: Any, tz: Any) -> TemporalRef:
        raise RuntimeError("x")

    res = compile_items([item(when="today")], ctx(time_anchor=boom))
    assert res.envelopes[0].time.event.start.basis == "none"
    assert res.notes[0].code == "time_anchor_failed"


def test_binding_levels() -> None:
    assert one().assessment.binding == "bound"
    assert one(s=None).assessment.binding == "partly"
    ctx2 = ctx(window={1: WindowLine("t-1", "src-1", 1, 0, "document", None, None, None, "x")})
    env = compile_items([item(s=None, lines=[1])], ctx2).envelopes[0]
    assert env.assessment.binding == "unbound" and env.subject is None


def test_span_not_found_kept_in_extras() -> None:
    env = one(span="totally absent")
    assert env.evidence[0].span is None and env.extras["span_text"] == "totally absent"


def test_missing_relation_never_collides_across_statements() -> None:
    a = one(r=None, o=None, text="Mirela likes tea")
    b = one(r=None, o=None, text="Mirela likes coffee")
    assert a.assertion_key != b.assertion_key
    assert a.attribute is None


# --- parsing ---
def test_parse_array_object_single_and_fenced() -> None:
    arr = json.dumps([item()])
    assert len(compile_items(arr, ctx()).envelopes) == 1
    assert len(compile_items(json.dumps({"items": [item()]}), ctx()).envelopes) == 1
    assert len(compile_items(json.dumps(item()), ctx()).envelopes) == 1
    assert len(compile_items(f"```json\n{arr}\n```", ctx()).envelopes) == 1


def test_valid_empty_is_not_a_parse_error() -> None:
    res = compile_items("[]", ctx())
    assert res.ok and res.envelopes == () and res.item_count == 0


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ('[{"text": "cut off', "not_json"),
        ("sure, here you go", "not_json"),
        ("42", "bad_structure"),
        ('{"foo": 1}', "bad_structure"),
        ('{"items": [], "extra": 1}', "bad_structure"),
    ],
)
def test_unparsable_output_sets_parse_error(raw: str, code: str) -> None:
    res = compile_items(raw, ctx())
    assert res.parse_error is not None and res.parse_error.code == code
    assert not res.ok and res.envelopes == () and res.rejections == ()


# --- determinism ---
def test_idempotent_and_ordered() -> None:
    raw = json.dumps([item(), item(text="Mirela likes tea", r="likes", o="tea"), item(zz=1)])
    a, b = compile_items(raw, ctx()), compile_items(raw, ctx())
    assert [e.model_dump_json() for e in a.envelopes] == [e.model_dump_json() for e in b.envelopes]
    assert [e.text for e in a.envelopes][:2] == ["Mirela lives in Quillon", "Mirela likes tea"]


def test_duplicate_items_rejected_second_time() -> None:
    res = compile_items([item(), item()], ctx())
    assert len(res.envelopes) == 1 and res.rejections[0].code is RejectionCode.DUPLICATE


def test_ids_vary_with_inputs() -> None:
    base = one()
    later = compile_items([item()], ctx(session_id="sess-10")).envelopes[0]
    assert later.write_id != base.write_id and later.occurrence_id == base.occurrence_id
    other_turn = compile_items(
        [item()],
        ctx(window={1: WindowLine("t-77", "src-1", 1, 5, "user", "zorbi", T0, "UTC", "x")}),
    ).envelopes[0]
    assert other_turn.occurrence_id != base.occurrence_id
    other_scope = compile_items(
        [item()], ctx(scope=OwnerScope(tenant="acme", owner="other"))
    ).envelopes[0]
    assert other_scope.write_id != base.write_id


def test_accepts_itemv1_instances() -> None:
    from memspine.schemas.steps.shared import ExtractionItem

    res = compile_items([ExtractionItem(**item())], ctx())
    assert len(res.envelopes) == 1
