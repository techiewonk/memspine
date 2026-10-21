"""Pure example leaves. Replace with injected, idempotent application adapters."""

from __future__ import annotations

from typing import Any

from resonate.context import Context
from resonate.resonate import Resonate
from resonate.retry import Never


async def prepare(ctx: Context, items: list[str]) -> list[str]:
    return [item.strip() for item in items if item.strip()]


async def refine(ctx: Context, items: list[str]) -> list[str]:
    # An LLM/network call belongs in a leaf like this, with its own deadline
    # and stable external idempotency key. It does not belong in the coordinator.
    return sorted({item.casefold() for item in items})


async def review(ctx: Context, items: list[str]) -> bool:
    return items == sorted(set(items)) and all(item == item.strip().casefold() for item in items)


async def micro_pipeline(ctx: Context, items: list[str], max_rounds: int) -> dict[str, Any]:
    current = await ctx.run(prepare, items)
    for round_index in range(max_rounds):
        current = await ctx.run(refine, current)
        accepted = await ctx.run(review, current)
        if accepted:
            return {"items": current, "rounds": round_index + 1}
    raise ValueError("Micro-processing exhausted its iteration budget")


def new_local_runtime() -> Resonate:
    # env={} prevents RESONATE_URL/HOST in the worker environment selecting
    # a remote transport. The verified SDK selects its in-memory LocalConnection.
    runtime = Resonate(env={}, autostart=False, retry_policy=Never(), max_concurrent_tasks=4)
    runtime.register(micro_pipeline, name="micro-pipeline-v1")
    runtime.start()
    return runtime
