"""Flow 4: read (stages C* of docs/v2/PIPELINE_STAGE_INVENTORY.md): search, assemble, read.

Vector leg, lexical leg, fusion, the assembled and dated context, the budget, the relevance gate (abstain) and
namespace isolation. Hash embedder, no reranker, no model call.
"""

from __future__ import annotations

import itertools
import re
from datetime import UTC, datetime
from typing import Any

from flows._common import Ctx, Step

NAME = "read"
TITLE = "Read: vector leg, lexical leg, fusion, assembled dated context, gate/abstain, namespace isolation"

CONFIG: dict[str, Any] = {
    "read": {
        "rerank": "off",
        "record_access": False,
        "render": "dated",
        "relevance_gate": "store_calibrated",
    },
}

OTHER = "flowcheck-other"
ROWS = {
    "move": ("Bram moved to Utrecht in April.", datetime(2024, 4, 10, tzinfo=UTC)),
    "cello": ("Ilse teaches cello at a school in Leiden.", datetime(2024, 3, 2, tzinfo=UTC)),
    "bike": ("Tomasz repairs bicycles on Saturdays.", datetime(2024, 3, 9, tzinfo=UTC)),
    "strings": ("Noor brought the cello strings on Friday.", datetime(2024, 5, 3, tzinfo=UTC)),
    "locker": (
        "Bram keeps his gym locker code, 4471, in a notebook.",
        datetime(2024, 2, 1, tzinfo=UTC),
    ),
}
FOREIGN = "Zoltan stores his cello at the Leiden hall; the hall code is 9988."


def _ids(ctx: Ctx) -> dict[str, str]:
    return ctx.state["ids"]


async def prepare(ctx: Ctx) -> None:
    eng = ctx.engine
    ids: dict[str, str] = {}
    for key, (text, when) in ROWS.items():
        ids[key] = (
            await eng.write(text, namespace=ctx.ns, memory_type="episodic", valid_from=when)
        ).record_id
    ids["foreign"] = (
        await eng.write(
            FOREIGN,
            namespace=OTHER,
            memory_type="episodic",
            valid_from=datetime(2024, 5, 5, tzinfo=UTC),
        )
    ).record_id
    ctx.state["ids"] = ids
    ctx.state["name_of"] = {v: k for k, v in ids.items()}
    ctx.show("records", {k: v[:8] for k, v in ids.items()})
    ctx.expect("five records in the namespace", 5, len(await eng.list_records(namespace=ctx.ns)))
    ctx.expect("one record in the other namespace", 1, len(await eng.list_records(namespace=OTHER)))


async def vector_leg(ctx: Ctx) -> None:
    eng, ids, names = ctx.engine, _ids(ctx), ctx.state["name_of"]
    vec = (await eng._embedder.embed(["Ilse teaches cello at a school"]))[0]
    hits = await eng._vector_leg(ctx.ns, list(vec), 5)
    ctx.show(
        "vector hits (private _vector_leg)",
        [(names.get(h.record_id), round(h.score, 3)) for h in hits],
    )
    ctx.expect("the closest text ranks first", ids["cello"], hits[0].record_id)
    ctx.check("scores are descending", all(a.score >= b.score for a, b in itertools.pairwise(hits)))
    ctx.check(
        "the other namespace's record is not in the leg",
        ids["foreign"] not in {h.record_id for h in hits},
    )
    ctx.state["vec_hits"] = hits


async def lexical_leg(ctx: Ctx) -> None:
    eng, ids, names = ctx.engine, _ids(ctx), ctx.state["name_of"]
    cello = await eng._lexical_leg(ctx.ns, "cello", 5)
    rare = await eng._lexical_leg(ctx.ns, "bicycles", 5)
    foreign = await eng._lexical_leg(ctx.ns, "Zoltan", 5)
    ctx.show("'cello'", [(names.get(h.record_id), round(h.score, 3)) for h in cello])
    ctx.show("'bicycles'", [(names.get(h.record_id), round(h.score, 3)) for h in rare])
    ctx.show("'Zoltan' (a word only the other namespace has)", [h.record_id for h in foreign])
    ctx.expect(
        "a word finds exactly the records that contain it",
        {ids["cello"], ids["strings"]},
        {h.record_id for h in cello},
    )
    ctx.expect("a rare word finds its one record", [ids["bike"]], [h.record_id for h in rare])
    ctx.expect("a word from another namespace finds nothing", [], foreign)


async def fusion(ctx: Ctx) -> None:
    from memspine.services.lexical.base import RRF_K, rrf_fuse

    eng, ids, names = ctx.engine, _ids(ctx), ctx.state["name_of"]
    query = "cello Leiden"
    vec = (await eng._embedder.embed([query]))[0]
    vhits = await eng._vector_leg(ctx.ns, list(vec), 5)
    lhits = await eng._lexical_leg(ctx.ns, query, 5)
    fused = rrf_fuse(vhits, lhits)
    ctx.show("vector rank", [names.get(h.record_id) for h in vhits])
    ctx.show("lexical rank", [names.get(h.record_id) for h in lhits])
    ctx.show("RRF fused", [(names.get(r), round(s, 4)) for r, s in fused])
    vr = {h.record_id: i + 1 for i, h in enumerate(vhits)}
    lr = {h.record_id: i + 1 for i, h in enumerate(lhits)}
    cello = ids["cello"]
    want = 1 / (RRF_K + vr[cello]) + 1 / (RRF_K + lr[cello])
    ctx.check(
        "the fused score of a record in both legs is the sum of 1/(k+rank) per leg",
        abs(dict(fused)[cello] - want) < 1e-9,
        (round(dict(fused)[cello], 5), round(want, 5)),
    )
    ctx.expect("the record both legs agree on ranks first", cello, fused[0][0])
    hits = await eng.search(query, namespace=ctx.ns, top_k=5)
    ctx.show(
        "engine.search (fusion + gates + score)",
        [(names.get(r.record_id), round(s, 3)) for r, s in hits],
    )
    ctx.expect("public search puts the same record first", cello, hits[0][0].record_id)
    ctx.check(
        "search scores are descending", all(a[1] >= b[1] for a, b in itertools.pairwise(hits))
    )
    ctx.expect("top_k is respected", 2, len(await eng.search(query, namespace=ctx.ns, top_k=2)))


async def assemble(ctx: Ctx) -> None:
    eng, ids = ctx.engine, _ids(ctx)
    ctx_ = await eng.assemble("Where did Bram move?", namespace=ctx.ns, budget_tokens=2000)
    ctx.show(
        "assembled",
        {
            "records": len(ctx_.records),
            "tokens_used": ctx_.tokens_used,
            "boundary_index": ctx_.boundary_index,
            "abstained": ctx_.abstained,
        },
    )
    for r in ctx_.records:
        ctx.show("  record", r.content)
    ctx.check(
        "the context holds the namespace's records and is not abstained",
        not ctx_.abstained and len(ctx_.records) == 5,
    )
    ctx.check(
        "tokens_used is positive and inside the budget",
        0 < ctx_.tokens_used <= 2000,
        ctx_.tokens_used,
    )
    ctx.check(
        "the relevant record is in the context", any("Utrecht" in r.content for r in ctx_.records)
    )
    pat = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) [A-Z][a-z]{2}\] ")
    ctx.check(
        "every record is rendered with its date", all(pat.match(r.content) for r in ctx_.records)
    )
    truth = {text: when.strftime("%Y-%m-%d") for text, when in ROWS.values()}
    ctx.check(
        "each rendered date is the record's valid_from (the message date, not today)",
        all(
            any(
                r.content.startswith(f"[{day} ") and text in r.content
                for text, day in truth.items()
            )
            for r in ctx_.records
        ),
    )
    ctx.check(
        "no record of the other namespace is in it",
        ids["foreign"] not in {r.record_id for r in ctx_.records}
        and not any("Zoltan" in r.content for r in ctx_.records),
    )


async def budget(ctx: Ctx) -> None:
    eng = ctx.engine
    big = await eng.assemble("Who teaches cello?", namespace=ctx.ns, budget_tokens=2000)
    small = await eng.assemble("Who teaches cello?", namespace=ctx.ns, budget_tokens=40)
    ctx.show("budget 2000", {"records": len(big.records), "tokens": big.tokens_used})
    ctx.show(
        "budget 40",
        {
            "records": len(small.records),
            "tokens": small.tokens_used,
            "text": [r.content[:50] for r in small.records],
        },
    )
    ctx.check("a small budget keeps fewer records", 0 < len(small.records) < len(big.records))
    ctx.check("and stays inside the budget", small.tokens_used <= 40, small.tokens_used)
    ctx.check(
        "the records kept are the relevant ones (cello), not the leftovers",
        any("cello" in r.content for r in small.records),
        [r.content[:30] for r in small.records],
    )


async def gate(ctx: Ctx) -> None:
    eng = ctx.engine
    cases = [
        ("Ilse teaches cello at a school in Leiden", False),
        ("Where did Bram move?", False),
        ("quantum chromodynamics lattice", True),
        ("Tell me about Zoltan", True),
    ]
    for q, abstain in cases:
        res = await eng.read(q, namespace=ctx.ns)
        c = res.context
        ctx.show(
            f"read({q!r})",
            {
                "mode": res.mode,
                "abstained": c.abstained,
                "records": len(c.records),
                "tokens": c.tokens_used,
            },
        )
        ctx.expect(f"abstained for {q!r}", abstain, c.abstained)
        if abstain:
            ctx.check(
                "an abstained context is empty and costs no tokens",
                not c.records and c.tokens_used == 0,
            )
        else:
            ctx.check("an answered context carries records", len(c.records) > 0)


async def isolation(ctx: Ctx) -> None:
    eng, ids, names = ctx.engine, _ids(ctx), ctx.state["name_of"]
    queries = ["Zoltan cello Leiden hall code 9988", "cello Leiden", "hall code"]
    leaked: list[str] = []
    for q in queries:
        hits = await eng.search(q, namespace=ctx.ns, top_k=10)
        asm = await eng.assemble(q, namespace=ctx.ns, budget_tokens=2000)
        red = await eng.read(q, namespace=ctx.ns)
        vec = (await eng._embedder.embed([q]))[0]
        legs = [
            *await eng._vector_leg(ctx.ns, list(vec), 10),
            *await eng._lexical_leg(ctx.ns, q, 10),
        ]
        seen = (
            {r.record_id for r, _ in hits}
            | {r.record_id for r in asm.records}
            | {r.record_id for r in red.context.records}
            | {h.record_id for h in legs}
        )
        if ids["foreign"] in seen:
            leaked.append(q)
        ctx.show(
            f"{q!r}",
            {"search": len(hits), "assemble": len(asm.records), "read": len(red.context.records)},
        )
    ctx.expect(
        "no verb in the first namespace ever returns the other namespace's record", [], leaked
    )
    back = await eng.search("cello Leiden", namespace=OTHER, top_k=10)
    ctx.show(
        "search in the other namespace", [(names.get(r.record_id), round(s, 3)) for r, s in back]
    )
    ctx.expect(
        "the other namespace sees only its own record",
        [ids["foreign"]],
        [r.record_id for r, _ in back],
    )
    ctx.expect(
        "a namespace that holds nothing returns nothing",
        [],
        await eng.search("cello", namespace="flowcheck-empty", top_k=5),
    )


STEPS = [
    Step(
        "F4.1",
        "prepare: five dated records in one namespace, one in another",
        prepare,
        "api/engine.py:write",
    ),
    Step(
        "F4.2",
        "C8 vector leg (private _vector_leg)",
        vector_leg,
        "read/pipeline/legs.py:_vector_leg",
    ),
    Step(
        "F4.3",
        "C9 lexical (BM25) leg (private _lexical_leg)",
        lexical_leg,
        "read/pipeline/legs.py:_lexical_leg",
    ),
    Step(
        "F4.4",
        "C16 fusion: reciprocal-rank fusion of the legs, then search()",
        fusion,
        "services/lexical/base.py:rrf_fuse, read/pipeline/router.py:_search",
    ),
    Step(
        "F4.5",
        "C23/C25 assemble: records, tokens, dates rendered",
        assemble,
        "compose/pack.py:AssemblyPolicy.assemble, compose/pipeline/render.py:_render",
    ),
    Step(
        "F4.6",
        "C23 budget: a small token budget keeps the relevant records",
        budget,
        "compose/pack.py, compose/pipeline/windows.py",
    ),
    Step(
        "F4.7",
        "C28 relevance gate: read() abstains off-topic, answers on-topic",
        gate,
        "read/pipeline/gate.py:_calibrated_gate",
    ),
    Step(
        "F4.8",
        "namespace isolation across search, assemble, read and both legs",
        isolation,
        "read/pipeline/router.py, store scope",
    ),
]
