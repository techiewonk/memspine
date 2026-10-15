# ruff: noqa: E501
"""Flow 14: associative graph (associate, related), reflection, and exemplar classification."""

from __future__ import annotations

from flows._common import Ctx, Step

NAME = "graph"
TITLE = "Graph and derived knowledge: associate, related (ppr/bfs/rrf), reflect, add_exemplar, classify"
CONFIG = {
    "memories": {
        "associative": {"enabled": True},
        "reflective": {"enabled": True},
        "procedural": {"enabled": True},
    }
}


async def link_chain(ctx: Ctx) -> None:
    e = ctx.engine
    texts = {
        "a": "Bram sails a wooden ketch called Marigold.",
        "b": "Marigold winters in the harbour at Terschelling.",
        "c": "The harbour at Terschelling holds an autumn regatta.",
        "d": "Ilse won the autumn regatta two years ago.",
        "z": "Tomasz bakes sourdough on Sundays.",
    }
    ids = {}
    for k, t in texts.items():
        ids[k] = (await e.write(t, namespace=ctx.ns, memory_type="semantic")).record_id
    ctx.state["ids"] = ids
    ctx.show("ids", {k: v[:8] for k, v in ids.items()})
    await e.associate(ids["a"], ids["b"], namespace=ctx.ns, rel="winters_at", weight=1.0)
    await e.associate(ids["b"], ids["c"], namespace=ctx.ns, rel="hosts", weight=1.0)
    await e.associate(ids["c"], ids["d"], namespace=ctx.ns, rel="won_at", weight=1.0)
    ctx.check("three explicit links created without error", True)
    try:
        await e.associate(ids["a"], "no-such-id", namespace=ctx.ns)
        bad = False
    except Exception as exc:
        bad = True
        ctx.show("link to a missing record", f"{type(exc).__name__}: {exc}")
    ctx.check("linking to a record that does not exist is refused", bad)


async def related_default(ctx: Ctx) -> None:
    e = ctx.engine
    ids = ctx.state["ids"]
    got = await e.related(ids["a"], namespace=ctx.ns, k=10)
    got_ids = [r.record_id for r in got]
    ctx.show("related(a) default strategy", [r.content[:30] for r in got])
    ctx.check("the directly linked record (b) is related to a", ids["b"] in got_ids)
    ctx.check("the seed itself is not returned", ids["a"] not in got_ids)
    ctx.check("an unlinked record (z) is not related to a", ids["z"] not in got_ids)
    ctx.check("graph reaches multi-hop (c) via the chain", ids["c"] in got_ids)


async def related_strategies(ctx: Ctx) -> None:
    e = ctx.engine
    ids = ctx.state["ids"]
    seen = {}
    for strat in ("ppr", "bfs", "rrf"):
        got = await e.related(ids["a"], namespace=ctx.ns, k=10, strategy=strat)
        seen[strat] = [r.record_id for r in got]
        ctx.show(f"related(a, strategy={strat})", [r.content[:30] for r in got])
        ctx.check(f"{strat}: finds b", ids["b"] in seen[strat])
        if strat == "rrf":  # rrf fuses graph ranks with similarity ranks, so an unlinked record may trail the linked ones
            ctx.check("rrf: the linked chain outranks the unlinked z", ids["z"] not in seen[strat] or seen[strat].index(ids["z"]) > seen[strat].index(ids["b"]))
        else:
            ctx.check(f"{strat}: excludes unlinked z", ids["z"] not in seen[strat])
    bfs1 = await e.related(ids["a"], namespace=ctx.ns, k=1, strategy="bfs")
    ctx.expect("k bounds the result size", 1, len(bfs1))
    ctx.expect("bfs k=1 returns the nearest neighbour (b)", ids["b"], bfs1[0].record_id)


async def related_gates(ctx: Ctx) -> None:
    e = ctx.engine
    ids = ctx.state["ids"]
    other = "ilse-ns"
    x = await e.write("Ilse keeps a lighthouse logbook.", namespace=other, memory_type="semantic")
    try:
        await e.associate(ids["a"], x.record_id, namespace=ctx.ns)
        refused = False
    except Exception as exc:
        refused = True
        ctx.show("cross-namespace link", f"{type(exc).__name__}: {exc}")
    ctx.check("cross-namespace link is refused", refused)
    await e.forget(ids["b"], namespace=ctx.ns)
    got = [r.record_id for r in await e.related(ids["a"], namespace=ctx.ns, k=10)]
    ctx.check("a forgotten record is no longer returned by related", ids["b"] not in got)


async def reflect(ctx: Ctx) -> None:
    e = ctx.engine
    ids = ctx.state["ids"]
    r = await e.reflect("Bram's ketch Marigold is tied to the Terschelling regatta scene.", [ids["a"], ids["c"]], namespace=ctx.ns)
    ctx.state["refl"] = r.record_id
    ctx.show("reflection", {"type": r.memory_type, "depth": r.reflection_depth, "parents": r.source.parents, "trust": r.trust})
    ctx.expect("memory type", "reflective", r.memory_type)
    ctx.expect("parents are the declared sources", sorted([ids["a"], ids["c"]]), sorted(r.source.parents))
    ctx.expect("depth from raw parents", 1, r.reflection_depth)
    pa = await e.get_record(ids["a"], namespace=ctx.ns)
    ctx.check("reflection trust is capped at the least trusted parent", r.trust <= min(pa.trust, 1.0), r.trust)
    r2 = await e.reflect("A pattern: Bram's boat-related facts recur around harbours.", [r.record_id], namespace=ctx.ns)
    ctx.expect("a reflection on a reflection has depth 2", 2, r2.reflection_depth)
    try:
        await e.reflect("Too deep.", [r2.record_id], namespace=ctx.ns)
        capped = False
    except Exception as exc:
        capped = True
        ctx.show("depth 3 reflection", f"{type(exc).__name__}: {exc}")
    ctx.check("reflection depth is hard-capped at 2", capped)
    try:
        await e.reflect("From nowhere.", ["no-such-id"], namespace=ctx.ns)
        missing = False
    except Exception as exc:
        missing = True
        ctx.show("reflect on a missing parent", f"{type(exc).__name__}: {exc}")
    ctx.check("a missing parent is refused", missing)


async def exemplars(ctx: Ctx) -> None:
    e = ctx.engine
    data = [
        ("Please move my dentist appointment to Friday.", "reschedule"),
        ("Can we shift the meeting with Ilse to next week?", "reschedule"),
        ("I want to cancel my gym membership entirely.", "cancel"),
        ("Cancel the subscription to the cheese club.", "cancel"),
    ]
    for text, label in data:
        r = await e.add_exemplar(text, label, namespace=ctx.ns, group="intent")
        ctx.state.setdefault("ex", []).append(r.record_id)
    ctx.show("last exemplar", {"kind_tags": r.tags, "group": r.group_id, "type": r.memory_type})
    ctx.expect("four exemplars stored", 4, len(ctx.state["ex"]))
    ctx.expect("exemplars live in the mapping group", "mapping:intent", r.group_id)
    ctx.expect("exemplars are procedural records", "procedural", r.memory_type)


async def classify(ctx: Ctx) -> None:
    e = ctx.engine
    out = await e.classify("Please move the dentist appointment to Tuesday", namespace=ctx.ns, group="intent")
    ctx.show("classify reschedule-like", {"label": out["label"], "margin": out["margin"], "votes": out["votes"]})
    ctx.expect("label", "reschedule", out["label"])
    out2 = await e.classify("I want to cancel the cheese club membership", namespace=ctx.ns, group="intent")
    ctx.show("classify cancel-like", {"label": out2["label"], "votes": out2["votes"]})
    ctx.expect("label", "cancel", out2["label"])
    ctx.check("the voting exemplars are returned", len(out["exemplars"]) > 0)
    none = await e.classify("anything", namespace=ctx.ns, group="no-such-group")
    ctx.expect("an empty group gives no label", None, none["label"])


STEPS = [
    Step("F14.1", "associate: build an a-b-c-d chain; reject a missing target", link_chain, "derive/pipeline/graph.py:associate, memories/associative"),
    Step("F14.2", "related (default strategy): neighbours, multi-hop, no seed, no unlinked", related_default, "derive/pipeline/graph.py:related"),
    Step("F14.3", "related with ppr / bfs / rrf and the k bound", related_strategies, "memories/associative (strategy)"),
    Step("F14.4", "related/associate gates: cross-namespace refused, forgotten hidden", related_gates, "derive/pipeline/graph.py"),
    Step("F14.5", "reflect: parents, depth, trust cap, depth cap 2", reflect, "derive/pipeline/graph.py:_reflect"),
    Step("F14.6", "add_exemplar into a mapping group", exemplars, "memories/procedural:add_exemplar"),
    Step("F14.7", "classify: kNN vote over the group's exemplars", classify, "ops/memory:classify"),
]
