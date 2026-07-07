"""Working memory paging + cache-aware context assembly (M13.1, M12/E2).

Run:  uv run python examples/02_working_memory_and_assembly.py

Shows: a pinned persona, a bounded hot window that pages old turns out to
episodic memory, and `assemble()` producing a stability-ordered context with
the E2 cache boundary marked.
"""

from __future__ import annotations

import asyncio

from memspine import Engine


async def main() -> None:
    engine = Engine(
        template="base",
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"working": {"enabled": True, "policies": {"page_size": 3}}},
    )
    await engine.start()
    ns = "agent/demo"

    await engine.set_persona(ns, "You are a concise, friendly coding assistant.")
    await engine.write("User likes Python and type hints", namespace=ns)

    for i in range(5):  # overflow the 3-turn hot window -> oldest pages out
        await engine.write(f"turn {i}: discussed feature {i}", namespace=ns, memory_type="working")

    working = await engine.retrieve(ns, "working")
    episodic = await engine.retrieve(ns, "episodic")
    print(f"hot window: {len(working)} records; paged to episodic: {len(episodic)}")

    context = await engine.assemble("what does the user like?", namespace=ns, budget_tokens=500)
    print(
        f"\nassembled {len(context.records)} records "
        f"(cache boundary after #{context.boundary_index}, "
        f"~{context.tokens_used} tokens):"
    )
    for index, record in enumerate(context.records):
        marker = "  [--- cache boundary ---]" if index == context.boundary_index else ""
        print(f"{marker}\n  {index}. ({record.memory_type}) {record.content}")

    await engine.stop()


if __name__ == "__main__":
    asyncio.run(main())
