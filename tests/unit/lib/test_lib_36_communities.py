from __future__ import annotations

import itertools
import random

from memspine.derive.communities import (
    CommunityConfig,
    CommunityEdge,
    GraphDelta,
    community_id,
    detect_communities,
    plan_summary_jobs,
    summary_jobs,
    update,
)


def E(a, b, w=1.0, aid=None, **kw):
    return CommunityEdge(src=a, dst=b, weight=w, assertion_ids=(aid,) if aid else (), **kw)


def clique(names, prefix, w=1.0):
    return [E(a, b, w, f"{prefix}-{a}-{b}") for a, b in itertools.combinations(names, 2)]


TEAM = ["Bram", "Noor", "Tomasz", "Aiko"]
CHOIR = ["Greta", "Pieter", "Sanne", "Joris"]


def two_clusters():
    return clique(TEAM, "w") + clique(CHOIR, "c") + [E("Aiko", "Greta", 0.1, "bridge")]


def groups(res):
    return sorted(c.members for c in res.communities)


def test_two_clear_clusters():
    res = detect_communities(two_clusters())
    assert res.converged
    assert groups(res) == sorted([tuple(sorted(TEAM)), tuple(sorted(CHOIR))])


def test_ids_are_hash_of_sorted_members():
    res = detect_communities(two_clusters())
    for c in res.communities:
        assert c.id == community_id(reversed(c.members))
        assert c.id.startswith("com_") and len(c.id) == 20


def test_disconnected_never_merge_and_small_dropped():
    edges = [*clique(TEAM, "w"), E("Lone", "Pair", 1.0, "p")]
    res = detect_communities(edges, CommunityConfig(min_size=3))
    assert groups(res) == [tuple(sorted(TEAM))]
    assert ("Lone", "Pair") in groups(detect_communities(edges))


def test_hub_exclusion_prevents_gluing():
    ilse = "Ilse"
    edges = two_clusters()[:-1]
    edges += [E(ilse, n, 5.0, f"h-{n}") for n in TEAM + CHOIR]
    glued = detect_communities(edges)
    split = detect_communities(edges, CommunityConfig(hubs=frozenset({ilse})))
    assert len(glued.communities) == 1
    assert groups(split) == sorted([tuple(sorted(TEAM)), tuple(sorted(CHOIR))])
    assert all(ilse not in c.members for c in split.communities)
    team = next(c for c in split.communities if "Bram" in c.members)
    assert "h-Bram" in team.assertion_ids  # hub edge still evidence for the member


def test_hub_downweight_keeps_clusters():
    edges = two_clusters()[:-1] + [E("Ilse", n, 0.2, f"h-{n}") for n in TEAM + CHOIR]
    res = detect_communities(edges, CommunityConfig(hubs=frozenset({"Ilse"}), hub_weight=0.1))
    team = next(c for c in res.communities if "Bram" in c.members)
    assert "Greta" not in team.members


def test_tentative_and_closed_ignored_by_default():
    base = clique(TEAM, "w")
    extra = [
        E("Aiko", "Greta", 3.0, "t", tentative=True),
        E("Aiko", "Pieter", 3.0, "x", valid_to="2025-01-01"),
    ]
    res = detect_communities(base + extra)
    assert groups(res) == [tuple(sorted(TEAM))]
    inc = detect_communities(
        base + extra, CommunityConfig(include_tentative=True, include_closed=True)
    )
    assert any("Greta" in c.members for c in inc.communities)


def test_degenerate_edges_ignored():
    res = detect_communities(
        [E("A", "A"), E("A", "B", 0.0), E("A", "B", float("nan")), E("A", "B", -1)]
    )
    assert res.communities == ()


def test_permutation_stability():
    edges = [*two_clusters(), E("Bram", "Noor", 0.5, "dup")]
    ref = detect_communities(edges)
    rng = random.Random(7)
    for _ in range(20):
        shuffled = edges[:]
        rng.shuffle(shuffled)
        flipped = [
            e.model_copy(update={"src": e.dst, "dst": e.src}) if rng.random() < 0.5 else e
            for e in shuffled
        ]
        assert detect_communities(flipped) == ref


def test_determinism_and_top_entities():
    assert detect_communities(two_clusters()) == detect_communities(two_clusters())
    star = [E("Hub2", n, 1.0 + i, f"s{i}") for i, n in enumerate(["a", "b", "c"])]
    c = detect_communities(star).communities[0]
    assert c.top_entities[0] == "Hub2"
    assert c.assertion_ids == ("s2", "s1", "s0")


def test_oversized_flagged_and_not_planned():
    cfg = CommunityConfig(max_community_size=3)
    res = detect_communities(clique(TEAM, "w"), cfg)
    assert res.communities[0].oversized
    ch = update([], GraphDelta(), edges=clique(TEAM, "w"), config=cfg)
    assert plan_summary_jobs(ch, 10_000).jobs == ()


def test_update_first_run_all_created():
    ch = update([], GraphDelta(), edges=two_clusters())
    assert len(ch.created) == 2 and not ch.changed and not ch.removed


def test_update_no_change_is_unchanged():
    first = update([], GraphDelta(), edges=two_clusters())
    again = update(first.communities, GraphDelta(), edges=two_clusters())
    assert again.needs_summary == () and len(again.unchanged) == 2 and again.id_map == {}


def test_small_membership_change_keeps_id_but_marks_changed():
    members = ["Bram", "Noor", "Tomasz", "Aiko", "Marit"]
    first = update([], GraphDelta(), edges=clique(members, "w"))
    old_id = first.communities[0].id
    grown = clique([*members, "Fenna"], "w")
    ch = update(first.communities, GraphDelta(touched_entities=frozenset({"Fenna"})), edges=grown)
    assert [c.id for c in ch.communities] == [old_id]
    assert ch.changed == (old_id,) and ch.created == () and ch.removed == ()
    assert ch.id_map == {community_id([*members, "Fenna"]): old_id}
    assert "Fenna" in ch.communities[0].members


def test_large_change_gets_new_id_and_retires_old():
    first = update([], GraphDelta(), edges=clique(TEAM, "w"))
    other = clique(["Xan", "Yara", "Zed", "Wim"], "z")
    ch = update(first.communities, GraphDelta(), edges=other)
    assert len(ch.created) == 1 and ch.removed == (first.communities[0].id,)


def test_erased_assertion_marks_changed():
    edges = clique(TEAM, "w")
    first = update([], GraphDelta(), edges=edges)
    remaining = [e for e in edges if "w-Bram-Aiko" not in e.assertion_ids]
    delta = GraphDelta(removed_assertion_ids=frozenset({"w-Bram-Aiko"}))
    ch = update(first.communities, delta, edges=remaining)
    assert ch.changed == (first.communities[0].id,)
    assert "w-Bram-Aiko" not in ch.communities[0].assertion_ids


def test_summary_jobs_lineage_trust_and_budget():
    edges = clique(TEAM, "w") + clique([*CHOIR, "Wouter"], "c")
    ch = update([], GraphDelta(), edges=edges)
    trust = {a: 0.9 for e in edges for a in e.assertion_ids}
    trust["w-Bram-Aiko"] = 0.4
    plan = plan_summary_jobs(ch, 100_000, trust_of=trust)
    assert len(plan.jobs) == 2
    assert plan.jobs[0].reason == "created" and len(plan.jobs[0].members) == 5  # larger first
    team = next(j for j in plan.jobs if "Bram" in j.members)
    assert team.trust_cap == 0.4
    assert set(team.lineage) == {a for a in trust if a.startswith("w-")}
    choir = next(j for j in plan.jobs if "Greta" in j.members)
    assert choir.trust_cap == 0.9

    tight = plan_summary_jobs(ch, plan.jobs[0].estimated_tokens)
    assert len(tight.jobs) == 1 and len(tight.deferred) == 1
    assert summary_jobs(ch, 0) == []
    assert summary_jobs(ch, 100_000, trust_of=trust) == list(plan.jobs)


def test_input_truncated_lineage_complete():
    ch = update([], GraphDelta(), edges=clique(TEAM, "w"))
    job = summary_jobs(ch, 100_000, max_input_tokens=320)[0]
    assert len(job.input_assertion_ids) == 3 and len(job.lineage) == 6
    assert job.input_assertion_ids == job.lineage[:3]


def test_only_changed_get_jobs():
    first = update([], GraphDelta(), edges=two_clusters())
    grown = [*two_clusters(), E("Noor", "Mees", 1.0, "m1"), E("Bram", "Mees", 1.0, "m2")]
    ch = update(first.communities, GraphDelta(touched_entities=frozenset({"Mees"})), edges=grown)
    jobs = summary_jobs(ch, 100_000)
    assert len(jobs) == 1 and "Mees" in jobs[0].members and jobs[0].reason == "changed"
