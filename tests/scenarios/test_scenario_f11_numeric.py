"""F11 numeric (docs/BI01_CONTRACT_REWRITE_PLAN.md "Expanded synthetic fixtures (RM-54)").

Explicit distance 5 km and price EUR 12.34 are typed property values with an explicit unit
or currency. A mass unit as a distance, a missing or malformed currency, a fractional count
and a guessed (binary float) amount are rejected. The registry gate is checked directly; the
end-to-end path proves that the rules-only profile never turns numbers into guessed facts."""

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


async def test_f11_numbers_never_become_guessed_facts(
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
        assert await claims(eng) == []  # no conversion, no guessed measurement
        assert unresolved(eng) == []
        assert (hike.content, ticket.content) == (HIKE, TICKET)  # numbers kept verbatim
        hits = await eng.search(QUERY, namespace="a", top_k=5, recorded_before=cut)
        assert hits == []
        await assert_replay(eng, ["Mira"])
        await assert_erased(eng, hike, [], QUERY)
    finally:
        await eng.stop()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "RM-54: GroundedFactCandidate has no attribute_key, unit, currency or amount and "
        "compile_grounded_facts never passes them to validate_compatibility "
        "(derive/non_generative_facts.py), so every property predicate such as "
        "has_measurement is rejected as semantic_validation_failed"
    ),
)
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
            )
        ],
    )
    eng = await _boot(monkeypatch, tmp_path / "f11x.db", extractor=ex)
    try:
        await send(eng, [msg("x-1", "Mira", HIKE, "2026-10-01T09:00Z")])
        assert unresolved(eng) == []
        [measure] = by_predicate(await claims(eng), "has_measurement")
        assert measure.object == "5"
    finally:
        await eng.stop()
