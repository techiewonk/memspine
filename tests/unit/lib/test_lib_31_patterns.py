# ruff: noqa: E501
"""LIB-31: rule-based pattern observations (recurrence, frequency, trend, co-occurrence, preference)."""

from __future__ import annotations

import itertools
import random
from datetime import UTC, datetime, timedelta

import pytest

from memspine.derive.histories import value_history
from memspine.derive.integrate import CardinalityRegistry
from memspine.derive.patterns import (
    PatternConfig,
    find_observations,
    invalidate,
    invalidated_groups,
    trend_observations,
    usefulness,
)
from memspine.schemas.durable.assertion import AssertionEnvelope, Mention
from memspine.schemas.durable.assessment import Assessment, Support
from memspine.schemas.durable.common import OwnerScope, Provenance
from memspine.schemas.durable.evidence import Evidence, TurnRef
from memspine.schemas.durable.identity import AssertionKey
from memspine.schemas.durable.temporal import TimeAxes

_n = itertools.count(1)


def dt(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=UTC)


def make(
    day: str,
    *,
    kind: str = "climbing",
    value: str | int | float | None = None,
    attr: str | None = None,
    subject: str = "Ilse",
    session: str | None = None,
    family: str = "user",
    trust: float = 0.9,
    restated: int = 0,
    owner: str = "u1",
) -> AssertionEnvelope:
    n = next(_n)
    time = TimeAxes(source_time=dt(day), recorded_at=dt(day))
    return AssertionEnvelope(
        write_id=f"w{n}",
        assertion_key=AssertionKey(src=subject, rel=attr or kind, dst=str(value or n), kind=kind),
        occurrence_id=f"oc_{n}",
        scope=OwnerScope(tenant="t1", owner=owner),
        provenance=Provenance(session_id=session),
        text=f"{subject} {kind} {value if value is not None else ''} {n}",
        kind=kind,
        subject=Mention(surface=subject, mention_id=f"m_{subject}"),
        attribute=attr,
        value=value,
        time=time,
        evidence=[
            Evidence(
                role="support",
                turn=TurnRef(source_id="s", source_version=1, seg_index=n),
                source_family=family,
            )
        ],
        assessment=Assessment(
            source_trust=trust,
            binding="bound",
            modality="asserted",
            time_precision=time.event_precision,
            support=Support(restated=restated),
        ),
        src_class="user_stated",
    )


def days(start: str, offsets: list[int], **kw) -> list[AssertionEnvelope]:
    base = dt(start)
    return [make((base + timedelta(days=o)).date().isoformat(), **kw) for o in offsets]


def only(*kinds: str) -> PatternConfig:
    return PatternConfig(kinds=frozenset(kinds))


def reg() -> CardinalityRegistry:
    r = CardinalityRegistry()
    r.declare("t1", "weight", "one")
    return r


def weights(pairs) -> list[AssertionEnvelope]:
    return [make(d, kind="weight", attr="weight", value=v) for d, v in pairs]


# ------------------------------------------------------------------ recurrence


def test_weekly_recurrence_with_weekday():
    a = days("2026-03-03", [0, 7, 14, 21])  # Tuesdays
    (o,) = find_observations(a, config=only("recurrence"))
    assert o.kind == "recurrence"
    assert o.statement["period"] == "weekly"
    assert o.statement["weekday"] == 1
    assert o.statement["subject"] == "ilse"
    assert o.count == 4
    assert o.support_ids == tuple(sorted(x.occurrence_id for x in a))


def test_weekly_with_one_day_jitter_loses_weekday():
    (o,) = find_observations(days("2026-03-03", [0, 7, 15, 21]), config=only("recurrence"))
    assert o.statement["period"] == "weekly"
    assert "weekday" not in o.statement


def test_weekly_with_one_skipped_week():
    a = days("2026-03-03", [0, 7, 14, 28, 35, 42, 49])
    (o,) = find_observations(a, config=only("recurrence"))
    assert o.statement["period"] == "weekly"


def test_monthly_recurrence_same_day_of_month():
    a = [make(d) for d in ("2026-01-15", "2026-02-15", "2026-03-15", "2026-04-15")]
    (o,) = find_observations(a, config=only("recurrence"))
    assert o.statement["period"] == "monthly"
    assert o.statement["day_of_month"] == 15


def test_yearly_recurrence_month_and_day():
    a = [make(d) for d in ("2023-06-10", "2024-06-10", "2025-06-10")]
    (o,) = find_observations(a, config=only("recurrence"))
    assert o.statement["period"] == "yearly"
    assert (o.statement["month"], o.statement["day_of_month"]) == (6, 10)


def test_irregular_gaps_are_not_recurrence():
    assert find_observations(days("2026-03-01", [0, 3, 13, 14]), config=only("recurrence")) == []


def test_biweekly_is_not_reported_as_weekly():
    assert find_observations(days("2026-03-03", [0, 14, 28, 42]), config=only("recurrence")) == []


def test_recurrence_minimum_evidence():
    assert find_observations(days("2026-03-03", [0, 7]), config=only("recurrence")) == []
    cfg = PatternConfig(kinds=frozenset({"recurrence"}), min_recurrence=4)
    assert find_observations(days("2026-03-03", [0, 7, 14]), config=cfg) == []
    assert len(find_observations(days("2026-03-03", [0, 7, 14, 21]), config=cfg)) == 1


def test_same_day_repeats_are_restated_not_extra_occurrences():
    assert find_observations(days("2026-03-03", [0, 0, 0, 7]), config=only("recurrence")) == []
    b = days("2026-03-03", [0, 0, 7, 14], restated=1)
    (o,) = find_observations(b, config=only("recurrence"))
    assert o.count == 3
    assert o.restated == 1 + 4  # one same-day duplicate plus four restated marks
    assert len(o.support_ids) == 4  # lineage still cites the duplicate


def test_other_subjects_and_owners_do_not_mix():
    a = days("2026-03-03", [0, 7], subject="Ilse") + days("2026-03-17", [0], subject="Bram")
    b = days("2026-03-24", [0], subject="Ilse", owner="u2")
    assert find_observations(a + b, config=only("recurrence")) == []


def test_hidden_and_negated_are_ignored():
    a = days("2026-03-03", [0, 7, 14])
    a[2] = a[2].model_copy(update={"status": "hidden"})
    assert find_observations(a, config=only("recurrence")) == []
    c = days("2026-03-03", [0, 7, 14])
    c[1] = c[1].model_copy(
        update={"assessment": c[1].assessment.model_copy(update={"modality": "negated"})}
    )
    assert find_observations(c, config=only("recurrence")) == []


# ------------------------------------------------------------------ frequency


def test_frequency_in_window():
    (o,) = find_observations(days("2026-03-01", [0, 3, 9, 12, 20]), config=only("frequency"))
    assert o.statement["occurrences"] == 5
    assert o.statement["window_days"] == 30


def test_frequency_below_threshold_and_outside_window():
    assert find_observations(days("2026-03-01", [0, 3, 9]), config=only("frequency")) == []
    spread = days("2026-01-01", [0, 20, 40, 60, 80])
    assert find_observations(spread, config=only("frequency")) == []


def test_frequency_as_of_moves_window():
    a = days("2026-03-01", [0, 1, 2, 3])
    assert len(find_observations(a, config=only("frequency"), as_of=dt("2026-03-10"))) == 1
    assert find_observations(a, config=only("frequency"), as_of=dt("2026-06-10")) == []


def test_frequency_counts_distinct_days_only():
    assert find_observations(days("2026-03-01", [0, 0, 0, 0, 1]), config=only("frequency")) == []


# ------------------------------------------------------------------ trend


def test_rising_trend():
    a = weights([("2026-01-01", 70), ("2026-02-01", 72), ("2026-03-01", 75)])
    (o,) = find_observations(a, config=only("trend"), registry=reg())
    assert o.kind == "trend"
    assert o.statement["direction"] == "rising"
    assert o.statement["first_value"] == 70.0
    assert o.statement["last_value"] == 75.0
    assert o.statement["points"] == 3
    assert o.statement["slope_per_day"] > 0
    assert len(o.support_ids) == 3


def test_falling_trend_from_history_directly():
    a = weights([("2026-01-01", 80), ("2026-02-01", 78), ("2026-03-01", 75), ("2026-04-01", 71)])
    (o,) = trend_observations(value_history(a, reg()))
    assert o.statement["direction"] == "falling"
    assert trend_observations(value_history(a, reg()), only("recurrence")) == []


@pytest.mark.parametrize("vals", [(70, 75, 72), (70, 70, 75), (70, 71)])
def test_non_monotone_flat_or_short_is_no_trend(vals):
    a = weights([(f"2026-0{i + 1}-01", v) for i, v in enumerate(vals)])
    assert find_observations(a, config=only("trend"), registry=reg()) == []


def test_tiny_change_is_no_trend():
    a = weights([(f"2026-0{i + 1}-01", v) for i, v in enumerate((100.0, 100.1, 100.2))])
    assert find_observations(a, config=only("trend"), registry=reg()) == []


def test_non_numeric_values_are_no_trend():
    a = weights([(f"2026-0{i + 1}-01", v) for i, v in enumerate(("light", "medium", "heavy"))])
    assert find_observations(a, config=only("trend"), registry=reg()) == []


# ------------------------------------------------------------------ co-occurrence


def test_co_occurrence_same_session():
    a = []
    for i in range(3):
        a.append(make(f"2026-03-0{i + 1}", kind="coffee", session=f"s{i}"))
        a.append(make(f"2026-03-0{i + 1}", kind="reading", session=f"s{i}"))
    (o,) = find_observations(a, config=only("co_occurrence"))
    assert (o.statement["predicate_a"], o.statement["predicate_b"]) == ("coffee", "reading")
    assert o.statement["unit"] == "session"
    assert o.count == 3
    assert len(o.support_ids) == 6


def test_co_occurrence_falls_back_to_day():
    a = []
    for i in range(3):
        a.append(make(f"2026-03-0{i + 1}", kind="coffee"))
        a.append(make(f"2026-03-0{i + 1}", kind="reading"))
    (o,) = find_observations(a, config=only("co_occurrence"))
    assert o.statement["unit"] == "day"


def test_co_occurrence_needs_min_units_and_ratio():
    two = []
    for i in range(2):
        two.append(make(f"2026-03-0{i + 1}", kind="coffee", session=f"s{i}"))
        two.append(make(f"2026-03-0{i + 1}", kind="reading", session=f"s{i}"))
    assert find_observations(two, config=only("co_occurrence")) == []
    b = []
    for i in range(3):
        b.append(make(f"2026-03-0{i + 1}", kind="coffee", session=f"s{i}"))
        b.append(make(f"2026-03-0{i + 1}", kind="reading", session=f"s{i}"))
    for i in range(3, 9):
        b.append(make(f"2026-03-{i + 1:02d}", kind="reading", session=f"s{i}"))
    assert len(find_observations(b, config=only("co_occurrence"))) == 1  # ratio uses the rarer kind
    c = b + [make(f"2026-04-{i + 1:02d}", kind="coffee", session=f"c{i}") for i in range(10)]
    assert find_observations(c, config=only("co_occurrence")) == []


# ------------------------------------------------------------------ preference stability


def pref(value: str, day: str, session: str | None, **kw):
    return make(day, kind="preference", attr="drink", value=value, session=session, **kw)


def test_preference_stable_across_sessions():
    a = [pref("tea", f"2026-03-0{i + 1}", f"s{i}") for i in range(3)]
    (o,) = find_observations(a, config=only("preference_stability"))
    assert o.statement["value"] == "tea"
    assert o.statement["sessions"] == 3
    assert o.count == 3


def test_preference_needs_n_sessions_not_n_mentions():
    a = [pref("tea", "2026-03-01", "s0") for _ in range(5)] + [pref("tea", "2026-03-02", "s1")]
    assert find_observations(a, config=only("preference_stability")) == []
    nosess = [pref("tea", f"2026-03-0{i + 1}", None) for i in range(4)]
    assert find_observations(nosess, config=only("preference_stability")) == []


def test_preference_changed_later_is_not_stable():
    a = [pref("tea", f"2026-03-0{i + 1}", f"s{i}") for i in range(3)]
    a.append(pref("coffee", "2026-03-09", "s9"))
    assert find_observations(a, config=only("preference_stability")) == []


def test_preference_restated_by_one_family_is_capped_but_independent_is_not():
    one = [pref("tea", f"2026-03-0{i + 1}", f"s{i}") for i in range(9)]
    (o1,) = find_observations(one, config=only("preference_stability"))
    assert o1.confidence == 0.75
    assert o1.independent_sources == 1
    two = [
        pref("tea", f"2026-03-0{i + 1}", f"s{i}", family="user" if i % 2 else "partner")
        for i in range(9)
    ]
    (o2,) = find_observations(two, config=only("preference_stability"))
    assert o2.independent_sources == 2
    assert o2.confidence > 0.75


def test_preferences_are_not_mined_as_events():
    a = [pref("tea", f"2026-03-{i + 1:02d}", f"s{i}") for i in range(0, 28, 7)]
    got = find_observations(a, config=only("recurrence", "frequency", "co_occurrence"))
    assert got == []


# ------------------------------------------------------------------ confidence, trust, lineage


def test_confidence_grows_with_evidence_and_stays_below_one():
    c3 = find_observations(days("2026-03-03", [0, 7, 14]), config=only("recurrence"))[0]
    c8 = find_observations(days("2026-03-03", list(range(0, 56, 7))), config=only("recurrence"))[0]
    assert 0 < c3.confidence < c8.confidence < 1


def test_trust_is_at_most_min_parent_trust():
    a = days("2026-03-03", [0, 7, 14], trust=0.9)
    a[1] = a[1].model_copy(
        update={"assessment": a[1].assessment.model_copy(update={"source_trust": 0.4})}
    )
    (o,) = find_observations(a, config=only("recurrence"))
    assert o.trust == pytest.approx(0.4)


def test_first_and_last_seen():
    (o,) = find_observations(days("2026-03-03", [0, 7, 14]), config=only("recurrence"))
    assert (o.first_seen, o.last_seen) == (dt("2026-03-03"), dt("2026-03-17"))


# ------------------------------------------------------------------ invalidation


def test_invalidate_only_changed_groups():
    climb = days("2026-03-03", [0, 7, 14], kind="climbing")
    swim = days("2026-03-04", [0, 7, 14], kind="swimming")
    obs = find_observations(climb + swim, config=only("recurrence"))
    assert len(obs) == 2
    gone = climb[1].occurrence_id
    ids = invalidate(obs, [gone])
    assert len(ids) == 1
    hit = next(o for o in obs if o.observation_id == ids[0])
    assert "climbing" in hit.group_key
    assert invalidated_groups(obs, [gone]) == [hit.group_key]
    assert invalidate(obs, []) == []
    assert invalidate(obs, ["unknown"]) == []


def test_recompute_after_change_drops_or_refreshes_observation():
    a = days("2026-03-03", [0, 7, 14])
    (before,) = find_observations(a, config=only("recurrence"))
    assert find_observations(a[:2], config=only("recurrence")) == []
    (changed,) = find_observations(days("2026-03-03", [0, 7, 14, 21]), config=only("recurrence"))
    assert changed.group_key == before.group_key
    assert changed.members_fp != before.members_fp


# ------------------------------------------------------------------ usefulness, config, determinism


def test_usefulness():
    assert usefulness([], ["a"]) == 0.0
    assert usefulness(["a", "b"], ["a", "z"]) == 0.5


@pytest.mark.parametrize(
    "bad",
    [
        {"min_recurrence": 1},
        {"frequency_window_days": 0},
        {"restated_cap": 0.0},
        {"kinds": frozenset({"nope"})},
    ],
)
def test_config_validation(bad):
    with pytest.raises(ValueError):
        PatternConfig(**bad)


def test_disabled_kinds_produce_nothing():
    a = days("2026-03-03", [0, 7, 14, 21])
    assert find_observations(a, config=PatternConfig(kinds=frozenset())) == []


def test_determinism_under_shuffle_and_repeat():
    a = (
        days("2026-03-03", [0, 7, 14, 21], kind="climbing")
        + days("2026-03-03", [0, 7, 14, 21], kind="swimming")
        + [pref("tea", f"2026-03-0{i + 1}", f"s{i}") for i in range(3)]
    )
    base = find_observations(a)
    assert base
    assert base == find_observations(a)
    for seed in range(5):
        b = list(a)
        random.Random(seed).shuffle(b)
        assert find_observations(b) == base
    keys = [(o.group_key, o.observation_id) for o in base]
    assert keys == sorted(keys)
