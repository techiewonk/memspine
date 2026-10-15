"""LIB-35: edge compiler (n-ary role edges, projection, tentative tags, closing, A-B-A, retract)."""

from __future__ import annotations

import itertools
import random
from datetime import UTC, datetime
from typing import Any

from memspine.derive.edges import (
    GraphSnapshot,
    apply_delta,
    assertion_node_id,
    compile_edges,
    retract,
    visible_edges,
)
from memspine.schemas.durable.assertion import AssertionEnvelope, Mention
from memspine.schemas.durable.assessment import Assessment
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.evidence import Evidence, TurnRef
from memspine.schemas.durable.identity import AssertionKey
from memspine.schemas.durable.temporal import Interval, TemporalRef, TimeAxes

SCOPE = OwnerScope(tenant="t1", owner="u1")
_n = itertools.count(1)


def dt(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=UTC)


def ref(day: str) -> TemporalRef:
    return TemporalRef(at=dt(day), precision="day", basis="explicit")


def make(
    subject: str = "Ilse",
    rel: str = "lives_in",
    obj: str | None = "Porto",
    *,
    oid: str | None = None,
    day: str = "2026-01-10",
    start: str | None = None,
    end: str | None = None,
    modality: str = "asserted",
    binding: str = "bound",
    status: str = "active",
    value: Any = None,
    graph: dict | None = None,
    key_aliases: list[str] | None = None,
    scope: OwnerScope = SCOPE,
    resolved: bool = True,
) -> AssertionEnvelope:
    n = next(_n)
    ev = None
    if start or end:
        ev = Interval(start=ref(start) if start else None, end=ref(end) if end else None)
    time = TimeAxes(event=ev, source_time=dt(day), recorded_at=dt(day))
    return AssertionEnvelope(
        write_id=f"w{n}",
        assertion_key=AssertionKey(src=subject, rel=rel, dst=obj or "", kind="state"),
        key_aliases=key_aliases or [],
        occurrence_id=oid or f"oc_{n}",
        scope=scope,
        text=f"{subject} {rel} {obj}",
        kind="state",
        subject=Mention(
            surface=subject,
            mention_id=f"m_{subject}",
            entity_id=f"e_{subject}" if resolved else None,
        ),
        object=(
            Mention(surface=obj, mention_id=f"m_{obj}", entity_id=f"e_{obj}" if resolved else None)
            if obj
            else None
        ),
        attribute=rel,
        value=value,
        time=time,
        evidence=[
            Evidence(
                role="support",
                turn=TurnRef(source_id="s", source_version=1, seg_index=n),
                source_family="user",
            )
        ],
        assessment=Assessment(
            source_trust=0.9,
            binding=binding,  # type: ignore[arg-type]
            modality=modality,  # type: ignore[arg-type]
            time_precision=time.event_precision,
        ),
        src_class="user_stated",
        status=status,  # type: ignore[arg-type]
        graph=graph,
    )


def labels(delta, kind: str) -> list[str]:
    return sorted(e.label for e in delta.edges if e.kind == kind)


# ----------------------------------------------------------------- n-ary roles and projection


def test_three_role_fact_has_one_assertion_node_and_three_role_edges() -> None:
    env = make(
        "Ilse",
        "moved_to",
        "Porto",
        graph={"nary": {"roles": {"location": "e_Quillon"}}},
        start="2026-02-01",
    )
    d = compile_edges([env])
    anodes = [n for n in d.nodes if n.kind == "assertion"]
    assert len(anodes) == 1 and anodes[0].id == assertion_node_id(env)
    assert labels(d, "role") == ["location", "object", "subject", "time"]
    assert all(e.src == anodes[0].id for e in d.edges if e.kind == "role")
    loc = next(e for e in d.edges if e.label == "location")
    assert loc.dst == "e_Quillon"


def test_value_role_gets_value_node() -> None:
    env = make("Bram", "age", None, value=41)
    d = compile_edges([env])
    assert labels(d, "role") == ["subject", "value"]
    vn = next(n for n in d.nodes if n.kind == "value")
    assert vn.label == "41"
    assert not [e for e in d.edges if e.kind == "projection"]


def test_projection_edge_subject_to_object_labelled_by_relation() -> None:
    d = compile_edges([make("Ilse", "lives_in", "Porto")])
    proj = [e for e in d.edges if e.kind == "projection"]
    assert len(proj) == 1
    assert (proj[0].src, proj[0].label, proj[0].dst) == ("e_Ilse", "lives_in", "e_Porto")


def test_extra_roles_multiple_targets_sorted() -> None:
    env = make(graph={"nary": {"roles": {"with": ["e_Noor", "e_Aiko"], "subject": "e_X"}}})
    d = compile_edges([env])
    withs = [e.dst for e in d.edges if e.label == "with"]
    assert withs == sorted(withs) and set(withs) == {"e_Noor", "e_Aiko"}
    assert next(e.dst for e in d.edges if e.label == "subject") == "e_Ilse"  # fixed role wins


def test_unresolved_mention_gets_no_entity_edge() -> None:
    env = make(resolved=False)
    d = compile_edges([env])
    assert not [e for e in d.edges if e.kind == "projection"]
    assert not [e for e in d.edges if e.label in ("subject", "object")]
    assert {r for _, r, _ in d.unbound} == {"subject", "object"}


def test_entity_ids_mapping_and_set() -> None:
    env = make(resolved=False)
    d = compile_edges([env], {"m_Ilse": "ent1", "m_Porto": "ent2"})
    proj = next(e for e in d.edges if e.kind == "projection")
    assert (proj.src, proj.dst) == ("ent1", "ent2")
    d2 = compile_edges([make()], {"e_Ilse"})  # Porto not in the known set -> unbound
    assert not [e for e in d2.edges if e.kind == "projection"]
    assert any(r == "object" for _, r, _ in d2.unbound)


# ----------------------------------------------------------------- tentative (S5-3)


def test_asserted_bound_is_normal_edge() -> None:
    d = compile_edges([make()])
    assert d.edges and not any(e.tentative for e in d.edges)


def test_hypothetical_planned_and_unbound_are_tentative_and_hidden_by_default() -> None:
    for kw in (
        {"modality": "hypothetical"},
        {"modality": "planned"},
        {"binding": "unbound"},
        {"binding": "partly"},
        {"modality": "reported"},
        {"status": "tentative"},
    ):
        d = compile_edges([make(**kw)])
        assert d.edges and all(e.tentative for e in d.edges), kw
        assert visible_edges(d.edges) == []
        assert len(visible_edges(d.edges, include_tentative=True)) == len(d.edges)


def test_bound_negation_is_normal_but_flagged_negated() -> None:
    d = compile_edges([make(modality="negated")])
    assert all(not e.tentative and e.negated for e in d.edges)


# ----------------------------------------------------------------- status changes


def test_superseded_closes_with_valid_to_not_deleted() -> None:
    old = make(start="2025-01-01", end="2026-01-05", status="superseded")
    d = compile_edges([old])
    assert d.edges and all(e.closed and e.valid_to == dt("2026-01-05") for e in d.edges)
    assert all(e.valid_from == dt("2025-01-01") for e in d.edges)
    assert len(visible_edges(d.edges)) == len(d.edges)
    assert visible_edges(d.edges, include_closed=False) == []


def test_superseded_without_event_end_uses_source_time() -> None:
    d = compile_edges([make(status="superseded", day="2026-03-03")])
    assert all(e.closed and e.valid_to == dt("2026-03-03") for e in d.edges)


def test_active_stays_open() -> None:
    d = compile_edges([make(start="2026-01-01")])
    assert all(not e.closed and e.valid_to is None for e in d.edges)


def test_contested_keeps_both_flagged() -> None:
    a = make("Ilse", "lives_in", "Porto", status="contested")
    b = make("Ilse", "lives_in", "Quillon", status="contested")
    d = compile_edges([a, b])
    proj = [e for e in d.edges if e.kind == "projection"]
    assert len(proj) == 2 and all(e.contested and not e.closed for e in proj)


def test_hidden_contributes_nothing() -> None:
    env = make(status="hidden")
    d = compile_edges([env])
    assert not d.edges and not d.nodes
    assert d.skipped == (env.occurrence_id,)


# ----------------------------------------------------------------- A-B-A


def test_aba_new_occurrence_new_edge_not_reopen() -> None:
    a1 = make(
        "Ilse",
        "lives_in",
        "Porto",
        oid="oc_a1",
        start="2025-01-01",
        end="2025-06-01",
        status="superseded",
    )
    b = make(
        "Ilse",
        "lives_in",
        "Quillon",
        oid="oc_b",
        start="2025-06-01",
        end="2026-01-01",
        status="superseded",
    )
    a2 = make("Ilse", "lives_in", "Porto", oid="oc_a2", start="2026-01-01")
    d = compile_edges([a1, b, a2])
    porto = [e for e in d.edges if e.kind == "projection" and e.dst == "e_Porto"]
    assert len(porto) == 2
    assert porto[0].key != porto[1].key
    closed = [e for e in porto if e.closed]
    opened = [e for e in porto if not e.closed]
    assert len(closed) == 1 and len(opened) == 1
    assert closed[0].valid_to == dt("2025-06-01") and opened[0].valid_to is None
    assert closed[0].occurrence_id == "oc_a1" and opened[0].occurrence_id == "oc_a2"
    assert closed[0].assertion_id != opened[0].assertion_id


def test_same_occurrence_replays_to_same_keys() -> None:
    env = make(oid="oc_same")
    d1, d2 = compile_edges([env]), compile_edges([env.model_copy(deep=True)])
    assert d1 == d2
    assert len({e.key for e in d1.edges}) == len(d1.edges)


def test_same_day_events_stay_two() -> None:
    e1 = make("Ilse", "visited", "Porto", oid="oc_v1")
    e2 = make("Ilse", "visited", "Porto", oid="oc_v2")
    d = compile_edges([e1, e2])
    assert len([e for e in d.edges if e.kind == "projection"]) == 2


def test_other_scope_gives_other_keys() -> None:
    a = compile_edges([make(oid="oc_x")])
    b = compile_edges([make(oid="oc_x", scope=OwnerScope(tenant="t1", owner="u2"))])
    assert not {e.key for e in a.edges} & {e.key for e in b.edges}


def test_key_aliases_listed() -> None:
    env = make(key_aliases=["ak_old"], oid="oc_k")
    d = compile_edges([env])
    anode = next(n for n in d.nodes if n.kind == "assertion")
    assert len(anode.aliases) == 1 and anode.aliases[0] != anode.id
    role = next(e for e in d.edges if e.label == "subject")
    assert len(role.aliases) == 1 and role.aliases[0] != role.key


# ----------------------------------------------------------------- retract


def test_retract_removes_only_that_assertions_edges() -> None:
    a, b = make("Ilse", oid="oc_ra"), make("Bram", "knows", "Noor", oid="oc_rb")
    snap = apply_delta(GraphSnapshot(), compile_edges([a, b]))
    aid = assertion_node_id(a)
    after = apply_delta(snap, retract([aid]))
    assert all(e.assertion_id != aid for e in after.edges.values())
    assert any(e.assertion_id == assertion_node_id(b) for e in after.edges.values())
    assert aid not in after.nodes
    assert "e_Ilse" in after.nodes  # entity nodes belong to resolution


def test_retract_with_envelopes_lists_explicit_keys_and_aliases() -> None:
    a = make(oid="oc_re", key_aliases=["ak_old"], graph={"nary": {"roles": {"location": "e_Q"}}})
    full = compile_edges([a])
    d = retract([a.occurrence_id], [a])
    assert set(d.remove_edges) >= {e.key for e in full.edges}
    assert all(x in d.remove_edges for e in full.edges for x in e.aliases)
    assert assertion_node_id(a) in d.remove_nodes
    assert d.retracted_assertions == (assertion_node_id(a),)
    snap = apply_delta(apply_delta(GraphSnapshot(), full), d)
    assert not snap.edges and not [n for n in snap.nodes.values() if n.kind != "entity"]


def test_retract_hidden_envelope_still_enumerates() -> None:
    a = make(oid="oc_rh", status="hidden")
    d = retract([assertion_node_id(a)], [a])
    assert d.remove_edges


def test_retract_unknown_id_is_harmless() -> None:
    d = retract(["an_nothing"])
    assert d.retracted_assertions == ("an_nothing",) and not d.remove_edges
    assert apply_delta(GraphSnapshot(), d) == GraphSnapshot()


# ----------------------------------------------------------------- determinism and rebuild


def corpus() -> list[AssertionEnvelope]:
    return [
        make(
            "Ilse",
            "lives_in",
            "Porto",
            oid="o1",
            start="2025-01-01",
            end="2025-06-01",
            status="superseded",
        ),
        make(
            "Ilse",
            "lives_in",
            "Quillon",
            oid="o2",
            start="2025-06-01",
            end="2026-01-01",
            status="superseded",
        ),
        make("Ilse", "lives_in", "Porto", oid="o3", start="2026-01-01"),
        make("Bram", "might_visit", "Aiko", oid="o4", modality="hypothetical"),
        make("Noor", "age", None, oid="o5", value=33),
        make("Tomasz", "knows", "Noor", oid="o6", status="contested"),
        make("Tomasz", "knows", "Aiko", oid="o7", status="hidden"),
    ]


def test_output_independent_of_input_order() -> None:
    base = corpus()
    ref_delta = compile_edges(base)
    rng = random.Random(7)
    for _ in range(10):
        shuffled = base[:]
        rng.shuffle(shuffled)
        assert compile_edges(shuffled) == ref_delta
    assert list(ref_delta.edges) == sorted(ref_delta.edges, key=lambda e: e.key)
    assert list(ref_delta.nodes) == sorted(ref_delta.nodes, key=lambda n: n.id)


def test_rebuild_equals_incremental() -> None:
    envs = corpus()
    rebuilt = apply_delta(GraphSnapshot(), compile_edges(envs))
    inc = GraphSnapshot()
    for e in envs:
        inc = apply_delta(inc, compile_edges([e]))
    assert rebuilt == inc


def test_status_transition_updates_in_place() -> None:
    env = make(oid="oc_t", start="2025-01-01")
    snap = apply_delta(GraphSnapshot(), compile_edges([env]))
    closed = env.model_copy(
        update={
            "status": "superseded",
            "time": env.time.model_copy(
                update={"event": Interval(start=ref("2025-01-01"), end=ref("2025-09-01"))}
            ),
        }
    )
    snap2 = apply_delta(snap, compile_edges([closed]))
    assert set(snap2.edges) == set(snap.edges)  # same keys, closed not duplicated
    assert all(e.closed and e.valid_to == dt("2025-09-01") for e in snap2.edges.values())


def test_empty_input() -> None:
    d = compile_edges([])
    assert d.nodes == () and d.edges == ()


def test_extras_not_used_by_graph_logic() -> None:
    env = make(oid="oc_ex")
    noisy = env.model_copy(update={"extras": {"roles": {"subject": "evil"}, "tentative": False}})
    assert compile_edges([env]) == compile_edges([noisy])
