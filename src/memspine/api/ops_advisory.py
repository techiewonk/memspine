"""API: advisory prospective and procedural reads (O4-10, flag-off, pull-based, read-only)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from memspine.derive.pipeline import advisory
from memspine.exceptions import ConflictError, MemspineError
from memspine.memory.procedural import Suggestion
from memspine.memory.prospective import Reminder
from memspine.observability import tap
from memspine.store.scope import validate_namespace


async def reminders(
    eng: Any,
    namespace: str = "default",
    now: datetime | None = None,
    cues: Sequence[str] = (),
) -> list[Reminder]:
    """Advisory reminders from stated plans (``memories.prospective.policies.enabled``).

    Plans are the namespace's live records tagged as a plan (``mod:plan``); later statements close
    them and old ones expire. A reminder carries its reasons, due time, source ids and a trust no
    higher than the least trusted source; it never acts, writes or delivers. ``cues`` are the
    current event or place strings to match cue-triggered plans against. ``now`` defaults to
    the current UTC instant (pass it to own time).
    """
    storage = eng._require_started()
    if not advisory.prospective_on(eng._config()):
        raise MemspineError(
            "advisory reminders not enabled — set memories.prospective.enabled: true and "
            "memories.prospective.policies.enabled: true"
        )
    ns = validate_namespace(namespace)
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        raise ConflictError("now must be timezone-aware (naive datetimes are ambiguous)")
    report = advisory.prospective_report(await storage.list_records(ns), now, cues)
    if tap.enabled():
        tap.event(
            "job.prospective.read",
            intentions=len(report.intentions),
            due=len(report.reminders),
            cues=len(cues),
        )
    return report.reminders


async def suggest_procedures(
    eng: Any,
    context_tags: Sequence[str],
    namespace: str = "default",
    limit: int | None = None,
) -> list[Suggestion]:
    """Advisory procedure ranking (``memories.procedural.policies.enabled``): tag overlap x reliability x
    trust, a precondition not in ``context_tags`` halves the score. Counts come from explicit
    like / dislike feedback only. Never executes anything."""
    storage = eng._require_started()
    if not advisory.procedural_on(eng._config()):
        raise MemspineError(
            "advisory procedures not enabled — set memories.procedural.enabled: true and "
            "memories.procedural.policies.enabled: true"
        )
    ns = validate_namespace(namespace)
    report = await advisory.procedural_report(storage, ns, eng._config())
    return advisory.suggest_for(report.procedures, context_tags, limit)
