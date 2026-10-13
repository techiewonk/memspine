"""Derive stage: anticipate (anticipated-question cues) (F-15)."""

from __future__ import annotations

from memspine.core.policies.consolidation import ConsolidationPolicy
from memspine.derive.pipeline.stages.context import PipelineContext, _policy_options
from memspine.derive.pipeline.stages.session_stage import _legacy_tag_check, _run_session_stage
from memspine.store.records import MemoryRecord


async def anticipate(ctx: PipelineContext) -> dict[str, object]:
    """H8: store likely future questions as cues on the turns that answer them.

    One call per consolidated session, idempotent per session (done marker). Cues
    go through ``Engine.add_cues``: firewall-screened, trust capped at the target
    turn's, and retrieval keys only (never assembled as content).
    """
    policy = ConsolidationPolicy.bind(_policy_options(ctx, "episodic", "consolidation"))
    if not getattr(policy.options, "anticipate", False):
        return {"status": "skipped", "reason": "consolidation.anticipate is off"}
    if ctx.anticipate is None or ctx.deposit_cues is None:
        return {"status": "skipped", "reason": "no anticipate/extract LLM role bound"}
    anticipator, deposit = ctx.anticipate, ctx.deposit_cues

    async def work(namespace: str, key: str, members: list[MemoryRecord]) -> tuple[int, list[str]]:
        transcript = "\n".join(
            f"[{n}] [{m.valid_from:%Y-%m-%d}] {m.content}" for n, m in enumerate(members, 1)
        )
        proposed = await anticipator(transcript)
        by_line: dict[int, list[str]] = {}
        for item in proposed:
            if 1 <= item.line <= len(members) and item.cue.strip():
                by_line.setdefault(item.line, []).append(item.cue.strip())
        written = 0
        errors: list[str] = []
        for line, texts in by_line.items():
            try:
                cues = await deposit(namespace, members[line - 1].record_id, texts, key)
            except Exception as exc:
                errors.append(f"{namespace}:{key}: cue deposit failed: {exc}")
                continue
            written += len(cues) if isinstance(cues, list) else len(texts)  # I72: skips write 0
        return written, errors

    legacy = _legacy_tag_check(ctx, "semantic", lambda r, key: f"anticipated:{key}" in r.tags)
    return await _run_session_stage(ctx, "anticipate", "cues", legacy, work)
