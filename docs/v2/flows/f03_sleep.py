"""Flow 3: sleep (offline path, stages B* of docs/v2/PIPELINE_STAGE_INVENTORY.md).

``engine.sleep()`` runs the maintenance cycle: consolidate (L2 summaries), standing answers (L5), edge compile,
decay. Everything here is deterministic: no model call is made by any stage that runs.
"""

from __future__ import annotations

from typing import Any

from flows._common import Ctx, Step
from flows.f01_ingest import CONFIG as INGEST_CONFIG
from flows.f01_ingest import install_offline_llm

NAME = "sleep"
TITLE = "Sleep: consolidate -> L2, standing answers -> L5, edge compile, decay, idempotence"

CONFIG: dict[str, Any] = INGEST_CONFIG

S1 = [
    "Bram: I live in Leiden with Ilse.",
    "Bram: Ilse teaches cello at the Noor school.",
    "Bram: Tomasz repairs our bicycles on Saturdays.",
    "Bram: My locker code at the gym is 4471.",
]
S2 = [
    "Bram: Now I live in Utrecht, close to Tomasz.",
    "Bram: Ilse still teaches cello in Leiden twice a week.",
    "Bram: The new flat has an address on Oudegracht 12.",
]

#: sleep stage -> inventory id (docs/v2/PIPELINE_STAGE_INVENTORY.md section B)
STAGE_IDS = {
    "consolidate": "B6",
    "mine_facts": "B7",
    "anticipate": "B7",
    "reflect_profile": "B8",
    "extract_graph": "B9",
    "pattern_observations": "B11",
    "standing_answers": "B11",
    "compile_edges": "B12",
    "summarize_entities": "B13",
    "reorganize": "B14",
    "check_watches": "B15",
    "session_lifecycle": "B15",
    "decay_sweep": "B16",
    "compress": "B16",
    "sleep_compute": "B16",
    "event_log_prune": "B16",
}


def _turns(lines: list[str], day: str) -> list[dict[str, str]]:
    return [{"role": "user", "name": "Bram", "content": t, "timestamp": day} for t in lines]


async def prepare(ctx: Ctx) -> None:
    install_offline_llm()
    eng, ns = ctx.engine, ctx.ns
    raw1 = await eng.ingest_messages(_turns(S1, "2024-03-01"), namespace=ns, session_id="S1")
    raw2 = await eng.ingest_messages(_turns(S2, "2024-04-10"), namespace=ns, session_id="S2")
    stats = await eng.flush_windows(ns, final=True)
    ctx.state["raw1"] = [r.record_id for r in raw1]
    ctx.state["raw2"] = [r.record_id for r in raw2]
    recs = await eng.list_records(namespace=ns)
    by: dict[str, int] = {}
    for r in recs:
        by[r.layer] = by.get(r.layer, 0) + 1
    ctx.show(
        "window flush",
        {k: stats.get(k) for k in ("windows_processed", "facts_deposited", "sessions_closed")},
    )
    ctx.show("records per layer before sleep", by)
    ctx.expect("only L0 and L1 exist before sleep", {"L0", "L1"}, set(by))
    ctx.expect("seven raw turns", 7, by.get("L0"))


async def run_sleep(ctx: Ctx) -> None:
    report = await ctx.engine.sleep()
    if hasattr(report, "wait"):  # offline.sleep_v2 returns a handle; it is off here
        report = await report.wait()
    ctx.state["report"] = report
    for stage, stats in report.items():
        ctx.show(f"{STAGE_IDS.get(stage, '?'):>3} {stage}", stats)
    ctx.check(
        "the report has one entry per stage, each with a status",
        bool(report) and all("status" in v for v in report.values()),
        len(report),
    )
    ctx.check(
        "no stage failed",
        all(v["status"] in ("ok", "skipped", "noop") for v in report.values()),
        {k: v["status"] for k, v in report.items() if v["status"] not in ("ok", "skipped", "noop")},
    )
    ctx.check(
        "every skipped stage says why",
        all(v.get("reason") for v in report.values() if v["status"] == "skipped"),
    )
    ctx.check(
        "the stages this flow depends on ran",
        all(
            report[s]["status"] == "ok"
            for s in ("consolidate", "standing_answers", "compile_edges", "decay_sweep")
        ),
    )
    ctx.show("stage order", list(report))
    ctx.check(
        "consolidate runs before standing answers, which run before edge compile",
        list(report).index("consolidate")
        < list(report).index("standing_answers")
        < list(report).index("compile_edges"),
    )


async def consolidate(ctx: Ctx) -> None:
    eng, ns = ctx.engine, ctx.ns
    recs = await eng.list_records(namespace=ns)
    summaries = [r for r in recs if r.layer == "L2" and r.source.channel == "consolidation"]
    raw1, raw2 = set(ctx.state["raw1"]), set(ctx.state["raw2"])
    by_id = {r.record_id: r for r in recs}
    ctx.show("consolidate report", ctx.state["report"]["consolidate"])
    for s in summaries:
        ctx.show(
            "L2 summary",
            {
                "id": s.record_id[:8],
                "text": s.content[:70],
                "parents": len(s.source.parents),
                "trust": s.trust,
            },
        )
    ctx.expect("one L2 summary per session", 2, len(summaries))
    ctx.expect(
        "the report counts the same summaries",
        len(summaries),
        ctx.state["report"]["consolidate"]["summaries"],
    )

    def session_of(rid: str) -> str:
        rec = by_id[rid]
        roots = {rid} if rid in raw1 | raw2 else set(rec.source.parents)
        return "S1" if roots & raw1 else "S2" if roots & raw2 else "?"

    ctx.check(
        "every summary has parents, all existing records",
        all(s.source.parents and set(s.source.parents) <= set(by_id) for s in summaries),
    )
    kinds = [{session_of(p) for p in s.source.parents} for s in summaries]
    ctx.check(
        "each summary's parents come from exactly one session",
        all(len(k) == 1 and "?" not in k for k in kinds),
        kinds,
    )
    ctx.check(
        "the two summaries cover different sessions", len({next(iter(k)) for k in kinds}) == 2
    )
    ctx.check(
        "summary trust <= its least-trusted parent",
        all(s.trust <= min(by_id[p].trust for p in s.source.parents) for s in summaries),
    )
    ctx.state["summaries"] = sorted(s.record_id for s in summaries)


async def standing(ctx: Ctx) -> None:
    eng, ns = ctx.engine, ctx.ns
    recs = await eng.list_records(namespace=ns)
    by_id = {r.record_id: r for r in recs}
    answers = [r for r in recs if r.layer == "L5"]
    ctx.show("standing report", ctx.state["report"]["standing_answers"])
    for a in answers:
        ctx.show(
            "L5",
            {
                "id": a.record_id[:8],
                "text": a.content[:80],
                "parents": [by_id[p].content[:35] for p in a.source.parents if p in by_id],
                "status": a.status.value,
            },
        )
    ctx.check(
        "at least one standing answer was built",
        bool(answers) and ctx.state["report"]["standing_answers"]["built"] >= 1,
        len(answers),
    )
    live = [a for a in answers if a.status.value == "activated"]
    top = live[0] if live else None
    latest = S2[-1].split(": ", 1)[1]
    ctx.check(
        "the standing answer is the latest value on the key (the stub keys every Bram line as 'bram said')",
        top is not None and latest in top.content,
        top.content[:80] if top else None,
    )
    ctx.check(
        "no older session's text leaks into the current answer",
        top is not None and not any(t.split(": ", 1)[1] in top.content for t in S1),
        top.content[:80] if top else None,
    )
    ctx.check(
        "it rests on live L1 facts (parents exist, are L1, are not archived)",
        top is not None
        and all(
            by_id[p].layer == "L1" and by_id[p].status.value == "activated"
            for p in top.source.parents
        ),
        [by_id[p].status.value for p in top.source.parents] if top else None,
    )
    older = [r for r in recs if r.layer == "L1" and S1[0].split(": ", 1)[1] in r.content]
    ctx.check(
        "the older value stays in the store as history (archived L1)",
        bool(older) and all(r.status.value == "archived" for r in older),
    )
    ctx.state["standing_ids"] = sorted(a.record_id for a in answers)


async def edges(ctx: Ctx) -> None:
    eng, ns = ctx.engine, ctx.ns
    report = ctx.state["report"]["compile_edges"]
    edge_list = await eng._graph.edge_list(ns)
    compiled = [e for e in edge_list if e.properties.get("compiled") == "edge_compiler_v2"]
    ctx.show("compile_edges report", report)
    ctx.show("edge counts", {"all": len(edge_list), "compiled": len(compiled)})
    for e in compiled[:3]:
        ctx.show(
            "compiled edge",
            {
                "src": e.src[:24],
                "rel": e.rel_type,
                "dst": e.dst,
                "kind": e.properties.get("edge_kind"),
                "record_id": str(e.properties.get("record_id"))[:8],
                "weight": e.weight,
            },
        )
    ctx.check(
        "the stage wrote edges", report["written"] > 0 and len(compiled) > 0, report["written"]
    )
    recs = {r.record_id: r for r in await eng.list_records(namespace=ns)}
    ctx.check(
        "every compiled edge names the L1 fact it came from (lineage by record_id)",
        all(
            str(e.properties.get("record_id")) in recs
            and recs[str(e.properties["record_id"])].layer == "L1"
            for e in compiled
        ),
    )
    ctx.check(
        "the subject role edge points at the entity node for Bram",
        any(e.rel_type.startswith("role:") and e.dst.endswith(":bram") for e in compiled),
        sorted({e.rel_type for e in compiled}),
    )
    ctx.check(
        "the superseded facts' edges were closed (report.closed > 0)",
        report["closed"] > 0,
        report["closed"],
    )
    nodes = await eng._graph.neighbors("ent:" + ns + ":bram", depth=1, namespace=ns)
    ctx.check("the entity node is reachable and has neighbours", len(nodes) > 0, len(nodes))


async def decay(ctx: Ctx) -> None:
    rep = ctx.state["report"]
    ctx.show("decay_sweep", rep["decay_sweep"])
    ctx.show("compress", rep["compress"])
    recs = await ctx.engine.list_records(namespace=ctx.ns)
    live = [r for r in recs if r.status.value == "activated"]
    ctx.expect("a fresh store has no tier transitions", 0, rep["decay_sweep"]["transitions"])
    ctx.check(
        "every live record is still in the hot tier",
        all(r.tier == "hot" for r in live),
        sorted({r.tier for r in live}),
    )
    ctx.expect("nothing was compressed", 0, rep["compress"]["compressed"])


async def idempotent(ctx: Ctx) -> None:
    eng, ns = ctx.engine, ctx.ns
    before = len(await eng.list_records(namespace=ns))
    edges_before = await eng._graph.edge_count()
    report = await eng.sleep()
    after = await eng.list_records(namespace=ns)
    ctx.show("second sleep: consolidate", report["consolidate"])
    ctx.show("second sleep: standing", report["standing_answers"])
    ctx.show("second sleep: compile_edges", report["compile_edges"])
    ctx.expect("a second cycle adds no records", before, len(after))
    ctx.expect(
        "the summaries are the same records",
        ctx.state["summaries"],
        sorted(
            r.record_id for r in after if r.layer == "L2" and r.source.channel == "consolidation"
        ),
    )
    ctx.expect(
        "the standing answers are the same records",
        ctx.state["standing_ids"],
        sorted(r.record_id for r in after if r.layer == "L5"),
    )
    ctx.expect("the edge count is unchanged", edges_before, await eng._graph.edge_count())


STEPS = [
    Step(
        "F3.1",
        "prepare: two sessions written and their windows flushed (L0 + L1 only)",
        prepare,
        "ingest/windows.py:flush_windows",
    ),
    Step(
        "F3.2",
        "engine.sleep(): the per-stage report (B0-B17)",
        run_sleep,
        "api/engine.py:sleep -> derive/pipeline/scheduler.py:sleep",
    ),
    Step(
        "F3.3",
        "B6 consolidate: one L2 summary per session, with parents",
        consolidate,
        "derive/pipeline/stages/consolidate.py",
    ),
    Step(
        "F3.4",
        "B11 standing answers: L5 current value resting on live L1 facts",
        standing,
        "derive/pipeline/stages/standing.py",
    ),
    Step(
        "F3.5",
        "B12 edge compile (graph.edge_compiler_v2): typed edges with lineage (private _graph)",
        edges,
        "derive/pipeline/stages/edge_compiler_v2.py",
    ),
    Step(
        "F3.6",
        "B16 decay / compress: no change on a fresh store",
        decay,
        "derive/pipeline/stages/maintenance.py, decay_v2.py",
    ),
    Step("F3.7", "a second sleep cycle is idempotent", idempotent, "derive/pipeline/scheduler.py"),
]
