"""Flow 7: forget, erasure, verification (soft forget, hard forget, per-subject erasure)."""

from __future__ import annotations

from flows._common import Ctx, Step

NAME = "forget"
TITLE = "Forget and erasure: soft forget, hard forget + verify, per-subject erasure"


async def write_records(ctx: Ctx) -> None:
    a = await ctx.engine.write(
        "Bram's locker code at the gym is 4471.", namespace=ctx.ns, memory_type="episodic"
    )
    b = await ctx.engine.write("Bram likes rye bread.", namespace=ctx.ns, memory_type="episodic")
    c = await ctx.engine.write(
        "Ilse teaches cello at a school in Leiden.",
        namespace=ctx.ns,
        memory_type="episodic",
        entity="Ilse",
    )
    ctx.state.update(a=a.record_id, b=b.record_id, c=c.record_id)
    ctx.show("written", [a.record_id, b.record_id, c.record_id])
    hits = await ctx.engine.search("locker code", namespace=ctx.ns, top_k=3)
    ctx.show("search 'locker code'", [(r.record_id[:8], round(s, 3)) for r, s in hits])
    ctx.check(
        "the locker record is findable before forget",
        any(r.record_id == a.record_id for r, _ in hits),
    )


async def soft_forget(ctx: Ctx) -> None:
    await ctx.engine.forget(ctx.state["b"], namespace=ctx.ns)
    rec = await ctx.engine.get_record(ctx.state["b"], namespace=ctx.ns)
    ctx.show(
        "record after soft forget",
        {"status": getattr(rec, "status", None), "content": getattr(rec, "content", None)},
    )
    hits = await ctx.engine.search("rye bread", namespace=ctx.ns, top_k=3)
    ctx.check(
        "soft-forgotten record no longer retrieved",
        all(r.record_id != ctx.state["b"] for r, _ in hits),
    )


async def hard_forget(ctx: Ctx) -> None:
    await ctx.engine.forget(ctx.state["a"], namespace=ctx.ns, hard=True, reason="user asked")
    hits = await ctx.engine.search("locker code 4471", namespace=ctx.ns, top_k=3)
    ctx.check(
        "hard-forgotten record no longer retrieved",
        all(r.record_id != ctx.state["a"] for r, _ in hits),
    )


async def verify(ctx: Ctx) -> None:
    proof = await ctx.engine.verify_forget(ctx.state["a"], namespace=ctx.ns, probe="4471")
    ctx.show("verify_forget", proof)
    ctx.check("verify_forget reports the erasure", bool(proof))


async def erase_subject(ctx: Ctx) -> None:
    gone = await ctx.engine.erase_subject("Ilse", namespace=ctx.ns, reason="subject request")
    ctx.show("erased ids", gone)
    hits = await ctx.engine.search("cello Leiden", namespace=ctx.ns, top_k=3)
    ctx.check(
        "Ilse's record no longer retrieved", all(r.record_id != ctx.state["c"] for r, _ in hits)
    )


STEPS = [
    Step("F7.1", "write three records and find one", write_records, "api/engine.py:write, search"),
    Step(
        "F7.2",
        "soft forget (status change, hidden from reads)",
        soft_forget,
        "api/engine.py:forget",
    ),
    Step(
        "F7.3",
        "hard forget (erasure + log redaction)",
        hard_forget,
        "api/engine.py:forget(hard=True), trust/ redaction",
    ),
    Step("F7.4", "verify the erasure across every store", verify, "api/engine.py:verify_forget"),
    Step("F7.5", "per-subject erasure", erase_subject, "api/engine.py:erase_subject"),
]
