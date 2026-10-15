from __future__ import annotations

import random
from typing import Any

from memspine.read.obligations import (
    EvidenceBundle,
    EvidenceFact,
    ObligationReport,
    QueryDescription,
    check,
    derive_obligations,
)


def F(i: str, entity: str, attribute: str, value: str | None = None, **kw: Any) -> EvidenceFact:
    return EvidenceFact(fact_id=i, entity=entity, attribute=attribute, value=value, **kw)


def B(*facts: EvidenceFact, **kw: Any) -> EvidenceBundle:
    return EvidenceBundle(bundle_id="b1", facts=facts, **kw)


def run(q: QueryDescription, *bundles: EvidenceBundle) -> ObligationReport:
    return check(derive_obligations(q), bundles)


def test_single_met_and_unmet() -> None:
    q = QueryDescription(kind="single", entities=("Ilse",), attribute="hobby")
    assert run(q, B(F("f1", "Ilse", "hobby", "chess"))).verdict == "answerable"
    r = run(q, B(F("f1", "Bram", "hobby", "chess")))
    assert r.verdict == "abstain" and r.follow_ups[0].entity == "Ilse"


def test_join_states() -> None:
    q = QueryDescription(kind="join", entities=("Ilse",), relation="sister", attribute="city")
    link = F("l", "Ilse", "sister", related_entity="Mara")
    assert run(q, B(link, F("c", "Mara", "city", "Gent"))).verdict == "answerable"
    part = run(q, B(link))
    assert part.verdict == "abstain" and part.results[0].status == "partly_met"
    assert part.results[0].holes[0].kind == "missing_attribute"
    assert part.follow_ups[0].entity == "Mara" and part.follow_ups[0].attribute == "city"
    none = run(q, B(F("c", "Mara", "city", "Gent")))
    assert (
        none.results[0].status == "unmet" and none.results[0].holes[0].kind == "missing_entity_link"
    )


def test_join_multiple_links_all_covered() -> None:
    q = QueryDescription(kind="join", entities=("Ilse",), relation="sister", attribute="city")
    facts = (
        F("l1", "Ilse", "sister", related_entity="Mara"),
        F("l2", "Ilse", "sister", related_entity="Noor"),
        F("c1", "Mara", "city", "Gent"),
    )
    r = run(q, B(*facts))
    assert r.results[0].status == "partly_met" and r.results[0].holes[0].entity == "Noor"


def test_duration() -> None:
    q = QueryDescription(kind="duration", entities=("Ilse",), attribute="job")
    both = B(
        F("s", "Ilse", "job", time_anchor="2020-03", anchor_role="start"),
        F("e", "Ilse", "job", time_anchor="2023-05", anchor_role="end"),
    )
    assert run(q, both).verdict == "answerable"
    assert run(q, B(F("d", "Ilse", "job", duration="three years"))).verdict == "answerable"
    r = run(q, B(F("s", "Ilse", "job", time_anchor="2020-03", anchor_role="start")))
    assert r.verdict == "abstain" and r.results[0].holes[0].kind == "missing_end_anchor"
    assert r.follow_ups[0].time_window == "after:2020-03"
    r = run(q, B(F("e", "Ilse", "job", time_anchor="2023-05", anchor_role="end")))
    assert r.results[0].holes[0].kind == "missing_start_anchor"
    r = run(q, B())
    assert {h.kind for h in r.results[0].holes} == {"missing_start_anchor", "missing_end_anchor"}


def test_list_and_count_caveat() -> None:
    facts = (
        F("a", "Ilse", "pet", "cat", session_id="s1"),
        F("b", "Ilse", "pet", "Dog", session_id="s2"),
        F("c", "Ilse", "pet", "dog", session_id="s3"),
    )
    for kind in ("list", "count"):
        q = QueryDescription(kind=kind, entities=("Ilse",), attribute="pet")  # type: ignore[arg-type]
        r = run(q, B(*facts))
        assert r.verdict == "answer_with_caveat" and r.caveats == ("at least 2",)
        assert r.results[0].holes[0].kind == "possible_unseen_members"
        assert run(q, B(*facts, exhaustive=True)).verdict == "answerable"
        assert run(q, B()).verdict == "abstain"


def test_count_stated_and_conflict() -> None:
    q = QueryDescription(kind="count", entities=("Ilse",), attribute="pet")
    assert run(q, B(F("a", "Ilse", "pet", stated_count=2))).verdict == "answerable"
    r = run(
        q,
        B(
            F("a", "Ilse", "pet", stated_count=1),
            F("b", "Ilse", "pet", "cat"),
            F("c", "Ilse", "pet", "dog"),
        ),
    )
    assert (
        r.verdict == "answer_with_caveat"
        and r.results[0].holes[0].kind == "stated_count_below_members"
    )
    lst = QueryDescription(kind="list", entities=("Ilse",), attribute="pet")
    assert run(lst, B(F("a", "Ilse", "pet", "cat", list_complete=True))).verdict == "answerable"


def test_exhaustive_scope() -> None:
    q = QueryDescription(kind="list", entities=("Ilse",), attribute="pet")
    f = F("a", "Ilse", "pet", "cat")
    assert run(q, B(f, exhaustive=True, scope_attribute="hobby")).verdict == "answer_with_caveat"
    assert run(q, B(f, exhaustive=True, scope_entity="ILSE")).verdict == "answerable"


def test_comparison_and_order() -> None:
    for kind in ("comparison", "order"):
        q = QueryDescription(kind=kind, entities=("Trip A", "Trip B"), attribute="date")  # type: ignore[arg-type]
        ok = B(
            F("a", "Trip A", "date", "x", time_anchor="2021-01"),
            F("b", "Trip B", "date", "y", time_anchor="2022-02"),
        )
        assert run(q, ok).verdict == "answerable"
        r = run(q, B(F("a", "Trip A", "date", "x", time_anchor="2021-01")))
        assert r.verdict == "abstain" and r.follow_ups[0].entity == "Trip B"
    one = QueryDescription(kind="order", entities=("Trip A",), attribute="date")
    r = run(one, B(F("a", "Trip A", "date", time_anchor="2021")))
    assert r.results[0].holes[0].kind == "too_few_operands"


def test_latest() -> None:
    q = QueryDescription(kind="latest", entities=("Ilse",), attribute="city")
    old = F("o", "Ilse", "city", "Gent", valid_from="2020", superseded_by="n")
    new = F("n", "Ilse", "city", "Oslo", valid_from="2023")
    r = run(q, B(old, new))
    assert r.verdict == "answer_with_caveat" and r.results[0].holes[0].kind == "unverified_recency"
    assert r.follow_ups[0].time_window == "after:2023"
    assert run(q, B(old, new, recency_checked=True)).verdict == "answerable"
    checked = F("n", "Ilse", "city", "Oslo", valid_from="2023", recency_checked=True)
    assert run(q, B(old, checked)).verdict == "answerable"
    dangling = run(q, B(old))
    assert (
        dangling.verdict == "abstain" and dangling.results[0].holes[0].kind == "missing_successor"
    )
    clash = run(q, B(F("x", "Ilse", "city", "Gent"), F("y", "Ilse", "city", "Oslo")))
    assert clash.results[0].holes[0].kind == "conflicting_values" and clash.verdict == "abstain"
    assert run(q, B()).verdict == "abstain"


def test_derive_mutable_and_also() -> None:
    q = QueryDescription(
        kind="single",
        entities=("Ilse",),
        attribute="city",
        time_view="current",
        attribute_mutable=True,
    )
    assert [o.kind for o in derive_obligations(q)] == ["latest"]
    q2 = QueryDescription(
        kind="join",
        entities=("Ilse",),
        relation="sister",
        attribute="city",
        also=("latest", "join"),
    )
    assert [o.kind for o in derive_obligations(q2)] == ["join", "latest"]


def test_mixed_obligations() -> None:
    q = QueryDescription(kind="list", entities=("Ilse",), attribute="pet", also=("latest",))
    r = run(q, B(F("a", "Ilse", "pet", "cat", recency_checked=True)))
    assert [x.status for x in r.results] == [
        "partly_met",
        "met",
    ] and r.verdict == "answer_with_caveat"
    q = QueryDescription(kind="duration", entities=("Ilse",), attribute="pet", also=("list",))
    assert run(q, B(F("a", "Ilse", "pet", "cat"))).verdict == "abstain"  # duration unmet


def test_multilingual_names() -> None:
    q = QueryDescription(kind="join", entities=("Zoë",), relation="Schwester", attribute="Stadt")
    r = run(
        q, B(F("l", "ZOË", "schwester", related_entity="Łucja"), F("c", "łucja", "STADT", "Kraków"))
    )
    assert r.verdict == "answerable"
    q = QueryDescription(kind="single", entities=("李雷",), attribute="城市")
    assert run(q, B(F("a", "李雷", "城市", "北京"))).verdict == "answerable"
    q = QueryDescription(
        kind="single", entities=("\uff21\uff4c\uff49\uff43\uff45",), attribute="x"
    )  # full-width
    assert run(q, B(F("a", "alice", "x", "1"))).verdict == "answerable"


def test_determinism_order_independent() -> None:
    q = QueryDescription(kind="list", entities=("Ilse",), attribute="pet", also=("latest",))
    facts = [F(f"f{i}", "Ilse", "pet", v) for i, v in enumerate(["cat", "dog", "owl", "cat"])]
    base = run(q, B(*facts))
    for seed in range(5):
        random.Random(seed).shuffle(facts)
        assert run(q, B(*facts)) == base
        two = [EvidenceBundle(bundle_id=f"b{i}", facts=(f,)) for i, f in enumerate(facts)]
        assert run(q, *two) == run(q, *reversed(two))
        assert run(q, *two).results[0].holes[0].detail.startswith("3 distinct")


def test_follow_ups_deduped() -> None:
    q = QueryDescription(kind="list", entities=("Ilse",), attribute="pet", also=("count",))
    r = run(q, B(F("a", "Ilse", "pet", "cat")))
    assert len(r.follow_ups) == 1
