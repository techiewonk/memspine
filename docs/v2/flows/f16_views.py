# ruff: noqa: E501
"""Flow 16: views over a namespace (sessions, conversation, timeline, brief, stats, direct record reads)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from flows._common import Ctx, Step

NAME = "views"
TITLE = "Views: sessions, conversation, session_memories, timeline, brief, namespace_stats, get_record/envelope/list_records"
CONFIG = {"memories": {"reflective": {"enabled": True}}}

T0 = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)


async def seed(ctx: Ctx) -> None:
    e = ctx.engine
    # Day 1: a conversation of four turns (one session id), 09:00-09:03.
    day1 = [
        {"role": "user", "content": "Bram says he is planning a trip to Leiden in April.", "timestamp": (T0).isoformat()},
        {"role": "assistant", "content": "Leiden in April has tulip fields nearby.", "timestamp": (T0 + timedelta(minutes=1)).isoformat()},
        {"role": "user", "content": "Bram wants a train rather than a rental car.", "timestamp": (T0 + timedelta(minutes=2)).isoformat()},
        {"role": "assistant", "content": "A train from Amsterdam takes about forty minutes.", "timestamp": (T0 + timedelta(minutes=3)).isoformat()},
    ]
    r1 = await e.write_messages(day1, namespace=ctx.ns, session_id="conv-day1")
    # Day 3: a second conversation of two turns.
    d3 = T0 + timedelta(days=2)
    day3 = [
        {"role": "user", "content": "Ilse recommended a cheese market in Gouda.", "timestamp": d3.isoformat()},
        {"role": "assistant", "content": "The Gouda cheese market runs on Thursday mornings.", "timestamp": (d3 + timedelta(minutes=2)).isoformat()},
    ]
    r3 = await e.write_messages(day3, namespace=ctx.ns, session_id="conv-day3")
    ctx.state.update(day1=[r.record_id for r in r1], day3=[r.record_id for r in r3])
    ctx.show("turns written", {"day1": len(r1), "day3": len(r3)})
    ctx.expect("six turn records", 6, len(r1) + len(r3))
    ctx.check("turns are episodic", all(r.memory_type == "episodic" for r in [*r1, *r3]))


async def sessions(ctx: Ctx) -> None:
    ss = await ctx.engine.sessions(ctx.ns)
    ctx.show("sessions", [(s.start.isoformat(), s.size) for s in ss])
    ctx.expect("two sessions (a two-day silence splits them, default 30 min gap)", 2, len(ss))
    ctx.expect("session sizes", [4, 2], [s.size for s in ss])
    ctx.expect("first session holds the day-1 turns", sorted(ctx.state["day1"]), sorted(ss[0].record_ids))
    wide = await ctx.engine.sessions(ctx.ns, gap_minutes=60 * 24 * 7)
    ctx.expect("a one-week gap merges them into one session", 1, len(wide))
    tight = await ctx.engine.sessions(ctx.ns, gap_minutes=1)
    ctx.check("a one-minute gap splits more finely", len(tight) > 2, len(tight))


async def conversation(ctx: Ctx) -> None:
    e = ctx.engine
    conv = await e.conversation(ctx.ns, session_id="conv-day1")
    ctx.show("conv-day1", [(r.source.role, r.content[:34]) for r in conv])
    ctx.expect("the conversation holds exactly its four turns", sorted(ctx.state["day1"]), sorted(r.record_id for r in conv))
    ctx.expect("time order", [r.valid_from for r in conv], sorted(r.valid_from for r in conv))
    users = await e.conversation(ctx.ns, session_id="conv-day1", roles=["user"])
    ctx.expect("roles filter keeps only user turns", ["user", "user"], [r.source.role for r in users])
    ctx.expect("an unknown conversation is empty", [], await e.conversation(ctx.ns, session_id="nope"))


async def timeline(ctx: Ctx) -> None:
    e = ctx.engine
    tl = await e.timeline(ctx.ns)
    ctx.expect("timeline lists all six turns", 6, len(tl))
    ctx.expect("chronological", [r.valid_from for r in tl], sorted(r.valid_from for r in tl))
    win = await e.timeline(ctx.ns, start=T0 + timedelta(days=1), end=T0 + timedelta(days=4))
    ctx.show("window (day 2-4)", [r.content[:30] for r in win])
    ctx.expect("a window keeps only the day-3 turns", sorted(ctx.state["day3"]), sorted(r.record_id for r in win))


async def session_memories(ctx: Ctx) -> None:
    e = ctx.engine
    d1 = ctx.state["day1"]
    ref = await e.reflect("Bram prefers rail travel for the Leiden trip.", [d1[0], d1[2]], namespace=ctx.ns)
    ctx.state["refl"] = ref.record_id
    mem = await e.session_memories(ctx.ns, d1)
    ctx.show("derived from day-1 turns", [(r.memory_type, r.content[:40]) for r in mem])
    ctx.expect("exactly the reflection is derived from the day-1 turns", [ref.record_id], [r.record_id for r in mem])
    ctx.check("the raw turns themselves are not listed", all(r.record_id not in d1 for r in mem))
    ctx.expect("day-3 turns have no derived records", [], await e.session_memories(ctx.ns, ctx.state["day3"]))
    ctx.expect("no ids gives an empty list", [], await e.session_memories(ctx.ns, []))


async def brief(ctx: Ctx) -> None:
    e = ctx.engine
    blocks = await e.brief(ctx.ns, recent_turns=3)
    for b in blocks:
        ctx.show("block", b.content, 600)
    ctx.check("brief returns at least one block", len(blocks) >= 1)
    recent = [b for b in blocks if "LAST TURNS" in b.content]
    ctx.expect("one recent-turns block", 1, len(recent))
    lines = [ln for ln in recent[0].content.splitlines() if ln.startswith("- ")]
    ctx.expect("recent_turns=3 keeps three turns", 3, len(lines))
    ctx.check("the newest turn (Gouda market reply) is included", any("Thursday mornings" in ln for ln in lines))
    ctx.check("an old turn is left out", not any("train rather than" in ln for ln in lines))
    tiny = await e.brief(ctx.ns, budget_tokens=5)
    ctx.expect("a tiny token budget yields no block", 0, len(tiny))
    none = await e.brief(ctx.ns, recent_turns=0)
    ctx.expect("recent_turns=0 yields no recent block", 0, len([b for b in none if "LAST TURNS" in b.content]))


async def stats(ctx: Ctx) -> None:
    e = ctx.engine
    st = await e.namespace_stats(ctx.ns)
    ctx.show("namespace_stats", st)
    ctx.expect("live records = 6 turns + 1 reflection", 7, st["live"])
    ctx.expect("counts by memory type", {"episodic": 6, "reflective": 1}, st["by_type"])
    ctx.expect("two conversations", 2, st["conversations"])
    ctx.expect("nothing quarantined", 0, st["quarantined"])
    ctx.check("token estimate is positive", st["tokens_estimate"] > 0)
    ctx.expect("first event time is the day-1 start", T0, st["first"])
    await e.forget(ctx.state["day3"][0], namespace=ctx.ns)
    st2 = await e.namespace_stats(ctx.ns)
    ctx.expect("forgetting a turn lowers the live count", 6, st2["live"])
    ctx.expect("the forgotten turn shows under by_status", 1, st2["by_status"].get("archived", 0) + st2["by_status"].get("deleted", 0) + st2["by_status"].get("forgotten", 0))
    other = await e.namespace_stats("empty-ns")
    ctx.expect("an untouched namespace is empty", 0, other["records"])


async def direct_reads(ctx: Ctx) -> None:
    e = ctx.engine
    rid = ctx.state["day1"][0]
    rec = await e.get_record(rid, namespace=ctx.ns)
    ctx.show("get_record", {"id": rec.record_id[:8], "type": rec.memory_type, "layer": rec.layer, "content": rec.content})
    ctx.expect("get_record returns the record", rid, rec.record_id)
    ctx.expect("a missing id reads as None", None, await e.get_record("no-such-id", namespace=ctx.ns))
    ctx.expect("a foreign namespace reads as None", None, await e.get_record(rid, namespace="someone-else"))
    gone = await e.get_record(ctx.state["day3"][0], namespace=ctx.ns)
    ctx.check("get_record is not status-filtered (a forgotten record is still returned)", gone is not None and gone.status.value != "activated", gone.status.value if gone else None)
    env = await e.get_envelope(rid, namespace=ctx.ns)
    ctx.expect("no envelope for a record not written under write.envelope_v2", None, env)
    allr = await e.list_records(ctx.ns)
    ctx.expect("list_records is unfiltered by status (7 records incl. the forgotten turn)", 7, len(allr))
    ctx.expect("list_records by type", 6, len(await e.list_records(ctx.ns, "episodic")))
    ctx.expect("list_records by another type", 1, len(await e.list_records(ctx.ns, "reflective")))
    layers = sorted({r.layer for r in allr})
    ctx.show("layers present", layers)
    ctx.check("every record carries a layer tag", all(r.layer for r in allr))


STEPS = [
    Step("F16.1", "seed two conversations on two days", seed, "api/engine.py:write_messages"),
    Step("F16.2", "sessions: boundaries from the silence gap", sessions, "read/pipeline/timeview.py, memories/episodic/sessions.py:detect_sessions"),
    Step("F16.3", "conversation: one session id, time order, role filter", conversation, "api/handle.py:conversation"),
    Step("F16.4", "timeline: chronological, optional window", timeline, "read/pipeline/timeview.py:timeline"),
    Step("F16.5", "session_memories: derived records of given turns", session_memories, "api/handle.py:session_memories"),
    Step("F16.6", "brief: recent-turns block within a token budget", brief, "api/handle.py:brief"),
    Step("F16.7", "namespace_stats: counts by type/status, conversations, span", stats, "api/handle.py:namespace_stats"),
    Step("F16.8", "get_record, get_envelope, list_records", direct_reads, "api/engine.py:get_record, get_envelope, list_records"),
]
