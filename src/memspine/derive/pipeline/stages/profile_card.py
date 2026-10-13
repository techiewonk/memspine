"""Derive: the profile card (O4-07; LIB-39 ``derive/profile.py``), opt-in.

``offline.profile_card_v2`` off (default): nothing here runs. On:

* :func:`apply_session_ops` takes the profile ops call 2 proposed at session close
  (``ProposedProfileOp``), maps them to the library's ``ProfileOp`` and applies them through the
  guarded ``apply_ops`` (shape, evidence, stance, firm-evidence, staleness, trust, kappa_P bound,
  EMA drift). The cited turns are the evidence: each becomes an ``AssertionEnvelope`` through
  :func:`turn_assertion` (the engine stores no assertion envelopes yet, so a live turn record is
  the evidence unit).
* The card lives in the event log: one ``profile_card_v2`` MARKER per change, the latest wins
  (folded by ``SessionIndex``). Read-modify-write runs under the namespace lock.
* :func:`profile_sleep` (called from the ``reflect_profile`` sleep stage) invalidates entries whose
  supporting turns were forgotten, quarantined or archived, then reports the reflection plan.

Free text (entry values) is only in the marker payload, never in a tap event.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from memspine.config.constants import PROFILE_CARD_MARKER
from memspine.derive.pipeline.stages.context import PipelineContext
from memspine.derive.profile import (
    ChangedGroup,
    ContestNote,
    DriftState,
    ProfileCard,
    ProfileConfig,
    ProfileEntry,
    ProfileOp,
    SupportRef,
    apply_ops,
    empty_card,
    invalidate,
    reflection_plan,
)
from memspine.derive.session_close import ProposedProfileOp
from memspine.observability import tap
from memspine.schemas.durable.assertion import AssertionEnvelope
from memspine.schemas.durable.assessment import Assessment
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.evidence import Evidence, TurnRef
from memspine.schemas.durable.identity import AssertionKey
from memspine.schemas.durable.temporal import TimeAxes
from memspine.store.events import EventKind, EventLogMode, MemoryEvent
from memspine.store.records import MemoryRecord, RecordStatus

#: call-2 ``entry_type`` -> card section (the library has five; ``routines`` has no call-2 type).
SECTION_OF = {
    "identity": "identity",
    "attribute": "preferences",
    "relationship": "relationships",
    "instruction": "constraints",
}
_KIND_OF = {"add": "add", "update": "update", "delete": "remove"}
#: perspective ``mod:`` tags (I39) -> assertion modality; ``fact`` / ``opinion`` / none: asserted.
_MODALITY_OF = {
    "plan": "planned",
    "wish": "hypothetical",
    "hypothetical": "hypothetical",
    "question": "question",
    "reported": "reported",
    "negated": "negated",
}


def _now() -> datetime:
    """The clock of the stage (the library takes ``now`` as an argument; tests patch this)."""
    return datetime.now(UTC)


def profile_config(ctx: PipelineContext) -> ProfileConfig:
    cfg_o = ctx.config.offline
    return ProfileConfig(
        kappa_p=cfg_o.profile_kappa_p,
        max_chars=cfg_o.profile_max_chars,
        alpha=cfg_o.profile_alpha,
        allow_inferred=cfg_o.profile_allow_inferred,
    )


# --------------------------------------------------------------------------- evidence adapter


def _aware(value: datetime | None) -> datetime | None:
    return value if value is not None and value.tzinfo is not None else None


def turn_assertion(record: MemoryRecord, tenant: str, owner: str) -> AssertionEnvelope:
    """Adapter: one live turn record as the evidence assertion the profile guard reads."""
    mods = [t[4:] for t in record.tags if t.startswith("mod:")]
    modality = next((_MODALITY_OF[m] for m in mods if m in _MODALITY_OF), "asserted")
    role = record.source.role
    if "assistant_claim" in record.tags or role == "assistant":
        src_class = "assistant_proposed"
    elif role == "user":
        src_class = "user_stated"
    elif role == "tool":
        src_class = "tool"
    else:
        src_class = "external"
    live = record.status is RecordStatus.ACTIVATED and not record.quarantined
    family = role or "unknown"
    return AssertionEnvelope(
        write_id=record.record_id,
        assertion_key=AssertionKey(src=family, rel="turn", kind="turn"),
        occurrence_id=record.record_id,
        record_id=record.record_id,
        scope=OwnerScope(tenant=tenant, owner=owner),
        text=record.content or "(empty)",
        kind="turn",
        time=TimeAxes(source_time=_aware(record.valid_from)),
        evidence=[
            Evidence(
                role="support",
                turn=TurnRef(
                    source_id=record.record_id, source_version=record.version, seg_index=0
                ),
                source_family=family,
            )
        ],
        assessment=Assessment(
            source_trust=record.trust,
            binding="bound",
            modality=modality,  # type: ignore[arg-type]
        ),
        src_class=src_class,  # type: ignore[arg-type]
        status="active" if live else "hidden",
    )


async def _evidence_index(
    ctx: PipelineContext, ids: Sequence[str], tenant: str, owner: str
) -> dict[str, AssertionEnvelope]:
    out: dict[str, AssertionEnvelope] = {}
    for rid in dict.fromkeys(ids):
        record = await ctx.storage.get_record(rid)
        if record is not None and record.namespace == tenant:
            out[rid] = turn_assertion(record, tenant, owner)
    return out


# --------------------------------------------------------------------------- card <-> marker


def _iso(value: datetime) -> str:
    return value.isoformat()


def card_to_dict(card: ProfileCard) -> dict[str, Any]:
    return {
        "tenant": card.tenant,
        "owner": card.owner,
        "entries": [
            {
                "section": e.section,
                "key": e.key,
                "value": e.value,
                "support": [[s.assertion_id, s.trust] for s in e.support],
                "restated": e.restated,
                "first_confirmed": _iso(e.first_confirmed),
                "last_confirmed": _iso(e.last_confirmed),
                "status": e.status,
                "stance": e.stance,
                "attrs": list(e.attrs),
                "contests": [
                    {
                        "value": c.value,
                        "assertion_ids": list(c.assertion_ids),
                        "trust": c.trust,
                        "at": _iso(c.at),
                    }
                    for c in e.contests
                ],
                "drift": (
                    {"value": e.drift.value, "n": e.drift.n, "last_at": _iso(e.drift.last_at)}
                    if e.drift
                    else None
                ),
                "uses": e.uses,
                "needs_recheck": e.needs_recheck,
            }
            for e in card.entries
        ],
    }


def card_from_dict(raw: Mapping[str, Any], tenant: str, owner: str) -> ProfileCard:
    entries: list[ProfileEntry] = []
    for e in raw.get("entries", []) or []:
        drift = e.get("drift")
        entries.append(
            ProfileEntry(
                section=e["section"],
                key=e["key"],
                value=e["value"],
                support=tuple(SupportRef(str(a), float(t)) for a, t in e.get("support", [])),
                restated=int(e.get("restated", 0)),
                first_confirmed=datetime.fromisoformat(e["first_confirmed"]),
                last_confirmed=datetime.fromisoformat(e["last_confirmed"]),
                status=e.get("status", "active"),
                stance=e.get("stance", "stated"),
                attrs=tuple(e.get("attrs", [])),
                contests=tuple(
                    ContestNote(
                        value=c.get("value"),
                        assertion_ids=tuple(c.get("assertion_ids", [])),
                        trust=float(c.get("trust", 0.0)),
                        at=datetime.fromisoformat(c["at"]),
                    )
                    for c in e.get("contests", [])
                ),
                drift=(
                    DriftState(
                        value=float(drift["value"]),
                        n=int(drift["n"]),
                        last_at=datetime.fromisoformat(drift["last_at"]),
                    )
                    if drift
                    else None
                ),
                uses=int(e.get("uses", 0)),
                needs_recheck=bool(e.get("needs_recheck", False)),
            )
        )
    return ProfileCard(tenant, owner, tuple(entries))


async def load_card(ctx: PipelineContext, namespace: str) -> ProfileCard:
    """The namespace's current card (an empty one before the first change)."""
    await ctx.session_index.refresh(ctx.storage)
    raw = ctx.session_index.profile_cards.get(namespace)
    if raw is None:
        return empty_card(namespace, namespace)
    return card_from_dict(raw, namespace, namespace)


async def _save_card(ctx: PipelineContext, namespace: str, card: ProfileCard, via: str) -> None:
    assert ctx.append_event is not None
    payload = {"marker": PROFILE_CARD_MARKER, "card": card_to_dict(card), "via": via}
    await ctx.append_event(
        MemoryEvent(kind=EventKind.MARKER, namespace=namespace, actor="system", payload=payload)
    )
    ctx.session_index.profile_cards[namespace] = payload["card"]  # type: ignore[assignment]


def _writable(ctx: PipelineContext) -> bool:
    return ctx.append_event is not None and ctx.config.event_log.mode is not EventLogMode.EPHEMERAL


# --------------------------------------------------------------------------- apply (call 2)


def to_library_op(proposed: ProposedProfileOp, observed: datetime | None) -> ProfileOp | str:
    """Map a call-2 proposal to the library op, or a rejection reason (never raises)."""
    section = SECTION_OF.get(proposed.entry_type)
    kind = _KIND_OF.get(proposed.op)
    if section is None or kind is None:
        return "bad_shape"
    when = _aware(proposed.observed_at) or _aware(observed)
    if when is None:
        return "bad_time"
    return ProfileOp(
        kind=kind,  # type: ignore[arg-type]
        section=section,
        key=proposed.key,
        observed_at=when,
        value=None if kind == "remove" else proposed.value,
        cites=tuple(proposed.cites),
        stance=proposed.stance,  # type: ignore[arg-type]
    )


async def apply_session_ops(
    ctx: PipelineContext,
    namespace: str,
    session_id: str,
    proposed: Sequence[ProposedProfileOp],
) -> dict[str, object]:
    """Run call-2 profile ops through the guard and persist the new card. Returns counts."""
    if not proposed:
        return {"status": "ok", "proposed": 0, "applied": 0}
    if not _writable(ctx):
        return {"status": "skipped", "reason": "no durable write door for the card"}
    cfg = profile_config(ctx)
    cited = [c for p in proposed for c in p.cites]
    index = await _evidence_index(ctx, cited, namespace, namespace)
    seen_at: dict[str, datetime] = {}
    for rid, env in index.items():
        if env.time.source_time is not None:
            seen_at[rid] = env.time.source_time
    pre_rejected: Counter[str] = Counter()
    ops: list[ProfileOp] = []
    for p in proposed:
        times = [seen_at[c] for c in p.cites if c in seen_at]
        mapped = to_library_op(p, max(times) if times else None)
        if isinstance(mapped, str):
            pre_rejected[mapped] += 1
        else:
            ops.append(mapped)
    async with ctx.lock(namespace):
        card = await load_card(ctx, namespace)
        new_card, report = apply_ops(card, ops, index, config=cfg, now=_now())
        changed = card_to_dict(new_card) != card_to_dict(card)
        if changed:
            await _save_card(ctx, namespace, new_card, via=f"close:{session_id}")
    reasons = Counter(report.reasons())
    reasons.update(pre_rejected)
    rejected = report.rejected + sum(pre_rejected.values())
    stats: dict[str, object] = {
        "status": "ok",
        "proposed": len(proposed),
        "applied": report.applied,
        "rejected": rejected,
        "contested": report.contested,
        "noop": report.count("noop"),
        "evicted": len(report.evicted),
        "entries": len(new_card.entries),
        "reasons": dict(sorted(reasons.items())),
    }
    if tap.enabled():
        tap.event(
            "job.profile.ops",
            site="close",
            session=session_id,
            proposed=len(proposed),
            accepted=report.applied,
            rejected=rejected,
            contested=report.contested,
            evicted=len(report.evicted),
            entries=len(new_card.entries),
            kappa_p=cfg.kappa_p,
            reasons=dict(sorted(reasons.items())),
        )
    return stats


# --------------------------------------------------------------------------- sleep (reflection)


async def profile_sleep(ctx: PipelineContext) -> dict[str, object]:
    """Sleep-time upkeep of the card: ER-28 invalidation of forgotten support, then the plan."""
    if not _writable(ctx):
        return {"status": "skipped", "reason": "no durable write door for the card"}
    await ctx.session_index.refresh(ctx.storage)
    cfg = profile_config(ctx)
    expired = reduced = flagged = planned = 0
    reasons: Counter[str] = Counter()
    for namespace in sorted(ctx.session_index.profile_cards):
        async with ctx.lock(namespace):
            card = await load_card(ctx, namespace)
            if not card.entries:
                continue
            support = [s for e in card.entries for s in e.support_ids]
            index = await _evidence_index(ctx, support, namespace, namespace)
            gone = frozenset(s for s in support if s not in index or index[s].status != "active")
            if gone:
                card, report = invalidate(card, [ChangedGroup(assertion_ids=gone)])
                expired += len(report.expired)
                reduced += len(report.reduced)
                flagged += len(report.flagged)
                await _save_card(ctx, namespace, card, via="sleep")
            plan = reflection_plan(card, None, now=_now(), config=cfg, evidence_index=index)
            planned += len(plan.items)
            for item in plan.items:
                reasons.update(item.reasons)
    if tap.enabled():
        tap.event(
            "job.profile.invalidate",
            expired=expired,
            reduced=reduced,
            flagged=flagged,
            reflection_items=planned,
            reasons=dict(sorted(reasons.items())),
        )
    return {
        "status": "ok",
        "expired": expired,
        "reduced": reduced,
        "flagged": flagged,
        "reflection_items": planned,
        "reflection_reasons": dict(sorted(reasons.items())),
    }
