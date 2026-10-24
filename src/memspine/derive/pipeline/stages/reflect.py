"""Derive stage: reflect_profile (F-15)."""

from __future__ import annotations

from memspine.core.policies.consolidation import ConsolidationPolicy
from memspine.derive.pipeline.stages.context import PipelineContext, _policy_options
from memspine.derive.pipeline.stages.profile_card import profile_sleep
from memspine.derive.pipeline.stages.session_stage import _run_session_stage
from memspine.store.records import MemoryRecord


async def reflect_profile(ctx: PipelineContext) -> dict[str, object]:
    """H14: profile insights per consolidated session, once (done marker).

    O4-07 (``offline.profile_card``): the card upkeep (invalidation of forgotten support and the
    reflection plan) runs first and its counts ride along under ``profile_card``."""
    card = await profile_sleep(ctx) if ctx.config.offline.profile_card else None
    stats = await _reflect_insights(ctx)
    if card is not None:
        stats = {**stats, "profile_card": card}
    return stats


async def _reflect_insights(ctx: PipelineContext) -> dict[str, object]:
    policy = ConsolidationPolicy.bind(_policy_options(ctx, "episodic", "consolidation"))
    if not getattr(policy.options, "reflect_profile", False):
        return {"status": "skipped", "reason": "consolidation.reflect_profile is off"}
    if ctx.reflect is None or ctx.deposit_reflection is None:
        return {"status": "skipped", "reason": "no reflect LLM role / reflective memory"}
    reflector, deposit = ctx.reflect, ctx.deposit_reflection

    async def work(namespace: str, key: str, members: list[MemoryRecord]) -> tuple[int, list[str]]:
        proposed = await reflector([f"[{m.valid_from:%Y-%m-%d}] {m.content}" for m in members])
        written = 0
        errors: list[str] = []
        for text, evidence in proposed:
            ids = [members[i].record_id for i in evidence if 0 <= i < len(members)]
            if not (text.strip() and ids):
                continue
            try:
                stored = await deposit(namespace, text.strip(), ids, key)
            except Exception as exc:  # e.g. evidence forgotten mid-cycle (R2-2)
                errors.append(f"{namespace}:{key}: reflection deposit failed: {exc}")
                continue
            if stored is None:
                continue  # I72: evidence forgotten meanwhile; skipped, audited
            written += 1
        return written, errors

    return await _run_session_stage(ctx, "reflect_profile", "insights", work)
