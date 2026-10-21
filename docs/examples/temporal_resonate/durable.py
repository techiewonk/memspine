"""Embedded Resonate with an application-owned, versioned result journal.

All demonstrated leaves are pure. External adapters must additionally implement
idempotency/reconciliation: a crash after an effect but before complete() can
repeat that effect. Fencing here protects journal commits, not external systems.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from resonate.context import Context
from resonate.resonate import Resonate
from resonate.retry import Never

from memspine.workers.step_journal import CompletedStep, JsonValue, StaleClaimError, StepJournal

from .micro import prepare, refine, review


def fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def new_durable_local_runtime(
    journal_path: str, operation_id: str, owner_id: str, request_fingerprint: str
) -> Resonate:
    # Step lease exceeds the Activity's 100s execution budget; leaf I/O remains
    # bounded to 30s. New attempts wait for an outstanding lease to expire.
    journal = StepJournal(journal_path, lease_seconds=120)

    async def checkpoint(
        step_key: str, inputs: JsonValue, execute: Callable[[str], Awaitable[JsonValue]]
    ) -> Any:
        binding = fingerprint({"request": request_fingerprint, "inputs": inputs})
        claim = await asyncio.to_thread(
            journal.claim_step, operation_id, step_key, binding, owner_id
        )
        if isinstance(claim, CompletedStep):
            return claim.output
        try:
            async with asyncio.timeout(30):
                effect_key = fingerprint({"operation": operation_id, "step": step_key})
                output = await execute(effect_key)
            await asyncio.to_thread(journal.complete, claim, output)
            return output
        except TimeoutError:
            # Completion of a remote effect is uncertain: retain its lease.
            raise
        except Exception:
            # Cancellation deliberately leaves the lease in place; in-flight
            # threads or external adapters may not yet have stopped.
            try:
                await asyncio.to_thread(journal.fail, claim)
            except StaleClaimError:
                pass
            raise

    async def saved_prepare(ctx: Context, items: list[str]) -> list[str]:
        return await checkpoint("prepare/v1", items, lambda effect_key: prepare(ctx, items))

    async def saved_refine(ctx: Context, items: list[str], round_index: int) -> list[str]:
        return await checkpoint(
            f"round/{round_index}/refine/v1", items, lambda effect_key: refine(ctx, items)
        )

    async def saved_review(ctx: Context, items: list[str], round_index: int) -> bool:
        return await checkpoint(
            f"round/{round_index}/review/v1", items, lambda effect_key: review(ctx, items)
        )

    async def coordinator(ctx: Context, items: list[str], max_rounds: int) -> dict[str, Any]:
        current = await ctx.run(saved_prepare, items)
        for round_index in range(max_rounds):
            current = await ctx.run(saved_refine, current, round_index)
            accepted = await ctx.run(saved_review, current, round_index)
            if accepted:
                return {"items": current, "rounds": round_index + 1}
        raise ValueError("Micro-processing exhausted its iteration budget")

    runtime = Resonate(env={}, autostart=False, retry_policy=Never(), max_concurrent_tasks=4)
    runtime.register(coordinator, name="journaled-micro-pipeline-v1")
    runtime.start()
    return runtime
