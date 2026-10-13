"""Derive stage: reflect_profile (F-15)."""

from __future__ import annotations

from memspine.core.policies.consolidation import ConsolidationPolicy
from memspine.derive.pipeline.stages.context import PipelineContext, _policy_options
from memspine.derive.pipeline.stages.session_stage import _legacy_tag_check, _run_session_stage
from memspine.store.records import MemoryRecord


async def reflect_profile(ctx: PipelineContext) -> dict[str, object]:
    """H14: profile insights per consolidated session, once (done marker)."""
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

    legacy = _legacy_tag_check(
        ctx, "reflective", lambda r, key: r.source.message_id == f"reflected:{key}"
    )
    return await _run_session_stage(ctx, "reflect_profile", "insights", legacy, work)
