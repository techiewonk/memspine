"""F11 numeric (docs/BI01_CONTRACT_REWRITE_PLAN.md "Expanded synthetic fixtures (RM-54)").

Explicit distance 5 km and price EUR 12.34 are typed property values with an explicit unit
or currency. A mass unit as a distance, a missing or malformed currency, a fractional count
and a guessed (binary float) amount are rejected. The registry gate is checked directly; the
end-to-end path proves that the rules-only profile turns an explicit measurement into a typed
claim (ADR-088 amendment 2026-11-02) and never into a converted or guessed one: an
approximate value, a missing unit or currency, or a value of no stated dimension stays an
unresolved annotation."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from scenario_harness import (
    ScriptedExtractor,
    assert_erased,
    assert_replay,
    by_predicate,
    candidate,
    claims,
    msg,
    send,
    settle,
    support,
    tick,
    unresolved,
)
from scenario_harness import boot as _boot

from memspine.semantics import RoleBindingCheck, load_builtin_registry
from memspine.semantics.registry import SemanticRegistry, load_builtin_packs

HIKE = "The hike is 5 km long."
TICKET = "The ticket costs EUR 12.34."
QUERY = "How long is the hike"


def _measure(key: str, **kw: Any) -> None:
    load_builtin_registry().validate_compatibility(
        "has_measurement",
        subject_type="event",
        value_kind="number",
        attribute_key=key,
        context={"count_scope": "this trip"},
        **kw,
    )


def test_f11_typed_units_and_currency() -> None:
    _measure("distance", unit="km", amount=Decimal("5"))
    _measure("price", currency="EUR", amount=Decimal("12.34"))
    load_builtin_registry().validate_compatibility(
        "has_quantity",
        subject_type="event",
        value_kind="number",
        attribute_key="item_count",
        context={"count_scope": "tickets bought"},
        amount=Decimal("3"),
    )


@pytest.mark.parametrize(
    ("key", "kw", "message"),
    [
        ("distance", {"unit": "kg", "amount": Decimal("5")}, "unit"),  # kg-as-distance
        ("distance", {"amount": Decimal("5")}, "unit"),  # no explicit unit
        ("price", {"amount": Decimal("12.34")}, "currency"),  # missing currency
        ("price", {"currency": "eur", "amount": Decimal("12.34")}, "currency"),
        ("price", {"currency": "EURO", "amount": Decimal("12.34")}, "currency"),
        ("price", {"currency": "EUR", "unit": "km", "amount": Decimal("1")}, "unit"),
        ("distance", {"unit": "km", "amount": 5.0}, "Decimal"),  # a guessed float amount
        ("distance", {"unit": "km", "amount": Decimal("NaN")}, "Decimal"),
    ],
)
def test_f11_rejects_untyped_numbers(key: str, kw: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _measure(key, **kw)


def test_f11_rejects_fractional_and_negative_counts() -> None:
    registry = load_builtin_registry()
    for amount in (Decimal("2.5"), Decimal("-1")):
        with pytest.raises(ValueError, match="nonnegative integer"):
            registry.validate_compatibility(
                "has_quantity",
                subject_type="event",
                value_kind="number",
                attribute_key="item_count",
                context={"count_scope": "tickets"},
                amount=amount,
            )
    with pytest.raises(ValueError, match="context"):  # a count needs its explicit scope
        registry.validate_compatibility(
            "has_quantity",
            subject_type="event",
            value_kind="number",
            attribute_key="item_count",
            amount=Decimal("3"),
        )


def test_f11_amount_roles_need_an_explicit_unit() -> None:
    # the amount role is declared by the proposed pack (purchased / sold / paid), off by default
    registry = SemanticRegistry.from_packs(load_builtin_packs())
    spec = next(p for p in registry.predicates.values() if "amount" in p.role_names)
    others = [
        RoleBindingCheck(
            role=name,
            value_kind=registry.roles[name].value_kinds[0],
            entity_type=(registry.roles[name].entity_types or (None,))[0],
            evidence="exact_span",
        )
        for name in spec.required_roles
        if name != "amount"
    ]
    amount = RoleBindingCheck(
        role="amount", value_kind="number", evidence="exact_span", amount=Decimal("12.34")
    )
    with pytest.raises(ValueError, match="explicit unit"):
        registry.validate_role_bindings(spec.name, [*others, amount])
    with_unit = amount.model_copy(update={"unit": "EUR"})
    assert registry.validate_role_bindings(spec.name, [*others, with_unit]).name == spec.name


async def test_f11_numbers_become_typed_never_guessed_facts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f11.db")
    try:
        cut = await tick()
        [hike, ticket] = await send(
            eng,
            [
                msg("n-1", "Mira", HIKE, "2026-10-01T09:00Z"),
                msg("n-2", "Mira", TICKET, "2026-10-01T09:01Z"),
            ],
        )
        await settle(eng)
        # ADR-088 amendment 2026-11-02: an explicit measurement is a typed claim with its unit
        # or currency and the Decimal amount as written; nothing is converted or guessed
        found = by_predicate(await claims(eng), "has_measurement")
        assert [
            (c.subject, c.attribute_key, c.object, c.amount, c.unit, c.currency, c.principal)
            for c in found
        ] == [
            ("hike", "length", "5", Decimal("5"), "km", None, "Mira"),
            ("ticket", "price", "12.34", Decimal("12.34"), None, "EUR", "Mira"),
        ]
        assert all(isinstance(c.amount, Decimal) for c in found)
        assert [c.sources for c in found] == [(hike.record_id,), (ticket.record_id,)]
        assert unresolved(eng) == []
        assert (hike.content, ticket.content) == (HIKE, TICKET)  # numbers kept verbatim
        hits = await eng.search(QUERY, namespace="a", top_k=5, recorded_before=cut)
        assert hits == []
        await assert_replay(eng, ["Mira"])
        await assert_erased(eng, hike, [found[0].record_id], QUERY)
        assert [c.subject for c in await claims(eng)] == ["ticket"]
    finally:
        await eng.stop()


async def test_f11_unclear_numbers_stay_unresolved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f11u.db")
    try:
        texts = {
            "The hike is about 5 km long.": "approximate_value",
            "The ticket costs 12.34.": "missing_currency",
            "The ticket costs $12.34.": "missing_currency",
            "The hike is 5 kg long.": "missing_unit",
            "The hike is 5 km.": "ambiguous_measurement",
        }
        sent = await send(
            eng,
            [msg(f"u-{i}", "Mira", t, f"2026-10-01T09:0{i}Z") for i, t in enumerate(texts)],
        )
        assert await claims(eng) == []  # no converted, coerced or guessed value
        reasons = {u.source.record_id: (u.reason, u.asserted) for u in unresolved(eng)}
        assert reasons == {
            r.record_id: (reason, False) for r, reason in zip(sent, texts.values(), strict=True)
        }
    finally:
        await eng.stop()


async def test_f11_typed_measurement_compiles_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ex = ScriptedExtractor()
    ex.on(
        HIKE,
        lambda r, s: [
            candidate(
                r,
                s,
                predicate="has_measurement",
                obj="5",
                object_kind="number",
                subject="hike",
                subject_type="event",
                evidence=support(r, HIKE),
                cardinality_scope={"valid_time": "2026-10-01", "context": "this trip"},
                attribute_key="distance",
                unit="km",
                amount=Decimal("5"),
            )
        ],
    )
    eng = await _boot(monkeypatch, tmp_path / "f11x.db", extractor=ex)
    try:
        await send(eng, [msg("x-1", "Mira", HIKE, "2026-10-01T09:00Z")])
        assert unresolved(eng) == []
        [measure] = by_predicate(await claims(eng), "has_measurement")
        assert measure.object == "5"
        assert (measure.attribute_key, measure.unit, measure.currency) == ("distance", "km", None)
        assert measure.amount == Decimal("5") and isinstance(measure.amount, Decimal)
    finally:
        await eng.stop()
