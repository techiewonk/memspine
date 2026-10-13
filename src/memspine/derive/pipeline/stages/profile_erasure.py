"""Derive: the profile card under a turn forget (FU-CLOSE; O4-07 follow-up), opt-in.

``offline.profile_card_v2`` off (default): nothing here runs. On, when a supporting turn is
forgotten (soft or hard) :func:`scrub_profile_cards` runs at once, under the namespace write lock
the forget already holds, instead of waiting for the next sleep:

* a new ``profile_card_v2`` marker is appended without the entries supported only by the forgotten
  turns (entries with other support lose just those ids), so the current card never serves them;
* on a hard forget the text of every older card marker that carries a removed entry (its key,
  value and contest values) is redacted in the log with the storage pass hard forget already uses
  for WRITE payloads (``scrub_marker_payloads``); the marker, its seq and the card shape stay.

One engine hook, ``Engine._scrub_profile_cards``, calls this from ``trust.pipeline.erasure``.
Free text is never put in a tap event.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from memspine.config.constants import PROFILE_CARD_MARKER
from memspine.derive.pipeline.stages.index import SessionIndex
from memspine.derive.pipeline.stages.profile_card import card_from_dict, card_to_dict
from memspine.derive.profile import ChangedGroup, invalidate
from memspine.observability import tap
from memspine.store.events import EventKind, EventLogMode, MemoryEvent

#: the placeholder left where redacted entry text was.
_ERASED = ""


def _scrub_entries(
    payload: dict[str, Any], gone: frozenset[str], slots: set[tuple[str, str, str]]
) -> bool:
    """Blank the key, value and contest values of every card entry that was supported only by
    ``gone`` ids, or that equals an entry the forget removed. True when anything changed."""
    card = payload.get("card")
    if not isinstance(card, dict):
        return False
    changed = False
    for entry in card.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        support = {str(s[0]) for s in entry.get("support") or [] if s}
        slot = (str(entry.get("section")), str(entry.get("key")), str(entry.get("value")))
        if not ((bool(support) and support <= gone) or slot in slots):
            continue
        entry["key"] = entry["value"] = _ERASED
        entry["attrs"] = []
        for contest in entry.get("contests") or []:
            if isinstance(contest, dict):
                contest["value"] = _ERASED
        changed = True
    return changed


async def scrub_profile_cards(
    eng: Any, namespace: str, record_ids: Sequence[str], hard: bool
) -> dict[str, object]:
    """Drop forgotten support from the namespace's current card (lock held by the caller)."""
    if not eng._config().offline.profile_card_v2:
        return {"status": "off"}
    if eng._config().event_log.mode is EventLogMode.EPHEMERAL:
        return {"status": "skipped", "reason": "no durable log for the card"}
    storage = eng._require_started()
    index = SessionIndex()
    await index.refresh(storage)
    raw = index.profile_cards.get(namespace)
    if raw is None:
        return {"status": "ok", "entries": 0, "expired": 0, "reduced": 0, "redacted": 0}
    gone = frozenset(str(r) for r in record_ids)
    card = card_from_dict(raw, namespace, namespace)
    new_card, report = invalidate(card, [ChangedGroup(assertion_ids=gone)])
    expired = set(report.expired)
    slots = {
        (e.section, e.key, str(e.value)) for e in card.entries if (e.section, e.key) in expired
    }
    touched = card_to_dict(new_card) != card_to_dict(card)
    if touched:
        await eng._append_and_project(
            MemoryEvent(
                kind=EventKind.MARKER,
                namespace=namespace,
                actor="system",
                payload={
                    "marker": PROFILE_CARD_MARKER,
                    "card": card_to_dict(new_card),
                    "via": "forget",
                },
            )
        )
    redacted = 0
    if hard and (slots or touched):
        seqs = await storage.scrub_marker_payloads(
            PROFILE_CARD_MARKER, lambda p: _scrub_entries(p, gone, slots)
        )
        redacted = len(seqs)
    if tap.enabled():
        tap.event(
            "job.profile.forget",
            namespace=namespace,
            hard=hard,
            forgotten=len(gone),
            expired=len(report.expired),
            reduced=len(report.reduced),
            redacted_markers=redacted,
        )
    return {
        "status": "ok",
        "entries": len(new_card.entries),
        "expired": len(report.expired),
        "reduced": len(report.reduced),
        "redacted": redacted,
    }
