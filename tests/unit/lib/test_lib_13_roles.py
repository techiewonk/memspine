"""LIB-13: role policy, echo guard (I-8) and routing."""

from __future__ import annotations

import pytest

from memspine.ingest.roles import (
    EchoConfig,
    RecallMarker,
    RolePolicy,
    RoleRule,
    RoleTurn,
    detect_echo,
    normalise_tokens,
    route,
    wrap_recall,
)

MEM = RecallMarker(
    id="m1",
    text="Ilse lives in Tromso and works as a ferry engineer on the northern route",
    family="person:ilse",
)
OTHER = RecallMarker(
    id="m2", text="Kenji prefers green tea without sugar in the evening", family="person:kenji"
)


def test_defaults() -> None:
    p = RolePolicy()
    assert p.rule("assistant").derive_use == "proposal"
    assert p.rule("assistant").src_class == "assistant_proposed"
    assert p.rule("tool").derive_use == "evidence"
    assert p.rule("system").derive_use == "excluded" and not p.rule("system").extract
    assert p.rule("user").derive_use == "stated" and p.rule("user").base_trust == 0.7
    assert p.echo.enabled
    for role in ("user", "assistant", "tool", "system", "operator", "document", "external"):
        assert route(RoleTurn(role=role, content="x"), p).raw


def test_policy_validation() -> None:
    with pytest.raises(ValueError):
        RolePolicy(
            rules={"user": RoleRule(derive_use="stated", src_class="user_stated", base_trust=0.7)}
        )
    with pytest.raises(ValueError):
        RoleRule(derive_use="excluded", src_class="tool", base_trust=0.1)


def test_no_recall_list_is_no_echo() -> None:
    assert detect_echo("Ilse lives in Tromso", []).kind == "none"
    assert detect_echo("", [MEM]).kind == "none"


def test_verbatim_echo() -> None:
    v = detect_echo(MEM.text, [MEM, OTHER])
    assert v.kind == "overlap" and v.matched_ids == ("m1",)
    assert v.scores["m1"] == 1.0 and v.inherited_families == ("person:ilse",)


def test_marker_echo_even_without_recall_list() -> None:
    v = detect_echo("As noted: " + wrap_recall("m9", "anything at all"), [])
    assert v.kind == "marked" and v.matched_ids == ("m9",)


def test_partial_overlap_below_threshold_is_not_echo() -> None:
    turn = "Ilse lives in Tromso, so a fjord cruise would be lovely for her this summer, shall I book one?"
    assert detect_echo(turn, [MEM]).kind == "none"


def test_case_punctuation_insensitive() -> None:
    v = detect_echo(
        "ILSE lives in TROMSO -- and works as a ferry engineer, on the northern route!", [MEM]
    )
    assert v.kind == "overlap"


def test_short_span_inside_long_turn_by_containment() -> None:
    filler = " ".join(f"filler{i}" for i in range(60))
    v = detect_echo(f"{filler} {MEM.text} {filler}", [MEM])
    assert v.kind == "overlap" and v.scores["m1"] >= 0.9
    # a memory too short for containment is not matched inside a long turn
    tiny = RecallMarker(id="t", text="tea please")
    assert detect_echo(f"{filler} tea please {filler}", [tiny]).kind == "none"


def test_multilingual() -> None:
    ja_text = "\u7530\u4e2d\u3055\u3093\u306f\u6771\u4eac\u306e\u5927\u5b66\u3067\u7269\u7406\u5b66\u3092\u6559\u3048\u3066\u3044\u307e\u3059"
    ja_other = "\u7530\u4e2d\u3055\u3093\u306f\u4eac\u90fd\u3067\u30e9\u30fc\u30e1\u30f3\u304c\u597d\u304d\u3067\u3059"
    ja = RecallMarker(id="ja", text=ja_text)
    assert detect_echo(ja_text + "\u3002", [ja]).kind == "overlap"
    assert detect_echo(ja_other, [ja]).kind == "none"
    hi_text = "\u0906\u0930\u094d\u092f\u093e \u0939\u0930 \u0938\u0941\u092c\u0939 \u091b\u0939 \u092c\u091c\u0947 \u092a\u093e\u0930\u094d\u0915 \u092e\u0947\u0902 \u0926\u094c\u0921\u093c\u0928\u0947 \u091c\u093e\u0924\u0940 \u0939\u0948"
    hi = RecallMarker(id="hi", text=hi_text)
    assert detect_echo(hi_text, [hi]).kind == "overlap"
    de = RecallMarker(
        id="de",
        text="J\u00fcrgen wohnt in M\u00fcnster und f\u00e4hrt t\u00e4glich mit dem Fahrrad",
    )
    shout = "J\u00dcRGEN wohnt in M\u00dcNSTER und f\u00e4hrt t\u00e4glich mit dem Fahrrad."
    assert detect_echo(shout, [de]).kind == "overlap"
    assert normalise_tokens("\uff21\uff22\uff23") == ["abc"]  # NFKC


def test_disabled_and_threshold_config() -> None:
    assert detect_echo(MEM.text, [MEM], EchoConfig(enabled=False)).kind == "none"
    half = "Ilse lives in Tromso and works as a ferry engineer, plus unrelated words here and there today"
    assert detect_echo(half, [MEM], EchoConfig(threshold=0.99)).kind == "none"
    assert detect_echo(half, [MEM], EchoConfig(threshold=0.3)).kind == "overlap"


def test_assistant_echo_routing() -> None:
    p = RolePolicy()
    v = detect_echo(MEM.text, [MEM])
    r = route(RoleTurn(role="assistant", content=MEM.text), p, v)
    assert r.raw and r.echo and not r.extraction and not r.count_as_support
    assert r.derive_use == "excluded"
    assert r.inherits_family_of == ("m1",) and r.inherited_families == ("person:ilse",)
    assert any("echo" in x for x in r.reasons)


def test_user_copy_paste_is_assent_not_support() -> None:
    p = RolePolicy()
    v = detect_echo(MEM.text, [MEM])
    r = route(RoleTurn(role="user", content=MEM.text), p, v)
    assert r.assent_candidate and not r.echo and not r.extraction and not r.count_as_support
    assert r.derive_use == "stated"


def test_plain_routing() -> None:
    p = RolePolicy()
    a = route(RoleTurn(role="assistant", content="I suggest tea"), p)
    assert (
        a.extraction and a.derive_use == "proposal" and not a.count_as_support and a.firewall_first
    )
    u = route(RoleTurn(role="user", content="I like tea"), p)
    assert u.extraction and u.count_as_support and u.base_trust == 0.7
    s = route(RoleTurn(role="system", content="You are helpful"), p)
    assert s.raw and not s.extraction and s.derive_use == "excluded"
    t = route(RoleTurn(role="tool", content="{}"), p)
    assert t.extraction and t.src_class == "tool" and t.base_trust == 0.4


def test_deterministic() -> None:
    recalled = [OTHER, MEM]
    turn = "so " + MEM.text
    runs = [detect_echo(turn, recalled).model_dump() for _ in range(5)]
    assert all(r == runs[0] for r in runs)
    assert detect_echo(turn, recalled) == detect_echo(turn, list(reversed(recalled)))
