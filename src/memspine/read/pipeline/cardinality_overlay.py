"""Reader-specific scoped conflict flags on copies of canonical records (RM-45).

The persisted flag represents the exact declared audience at projection time.
Consent grants and the current viewer can narrow that audience later. Public
responses therefore derive their own flag from complete indexed groups after
the ordinary visibility and consent gates; no read writes a record or event.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from memspine.read import timeview as tv
from memspine.read.pipeline.timeview import _as_envelope
from memspine.schemas.durable.semantic import SemanticEnvelope
from memspine.store.claim_cardinality import group_key, group_of, record_group_of, value_token
from memspine.store.records import MemoryRecord
from memspine.store.visibility_policy import audience_key
from memspine.trust.visibility import visible_to


def _flagged(record: MemoryRecord, flag: bool) -> MemoryRecord:
    raw = record.fingerprint.envelope
    if raw is None or raw.get("cardinality_conflict") is flag:
        return record
    fp = record.fingerprint.model_copy(update={"envelope": {**raw, "cardinality_conflict": flag}})
    return record.model_copy(update={"fingerprint": fp})


async def _group_flag(
    eng: Any,
    record: MemoryRecord,
    viewer: str | None,
    cache: dict[tuple[str, str], tuple[bool, frozenset[str]]],
) -> bool:
    group = record_group_of(record)
    if group is None or not eng._consent_ok(record):
        return False
    if viewer is not None and not visible_to(record, viewer):
        return False
    key = group_key(group)
    cached = cache.get((record.namespace, key))
    if cached is not None:
        return cached[0] and record.record_id in cached[1]
    values: set[tuple[object, ...]] = set()
    visible_ids: set[str] = set()
    for peer in await eng._require_started().claim_group_snapshot(record.namespace, key):
        if record_group_of(peer) != group or not eng._consent_ok(peer):
            continue
        if viewer is not None and not visible_to(peer, viewer):
            continue
        raw = peer.fingerprint.envelope
        if raw is None:
            continue
        token = value_token(SemanticEnvelope.model_validate(raw))
        if token is not None:
            values.add(token)
            visible_ids.add(peer.record_id)
    conflict = len(values) > 1
    cache[(record.namespace, key)] = (conflict, frozenset(visible_ids))
    return conflict and record.record_id in visible_ids


async def historical_flags(
    eng: Any, namespace: str, cutoff: datetime, *, viewer: str | None, valid: datetime | None
) -> dict[str, bool]:
    """Derive known-time flags from the complete pre-top-k visible population."""
    listed = await eng._require_started().list_records(namespace)
    if not listed:
        return {}
    excluded = await eng._require_started().hidden_or_erased_record_ids(
        namespace, [row.record_id for row in listed]
    )
    listed = [row for row in listed if row.record_id not in excluded]
    # The public recorded_before bound is exclusive; the time view tests <=.
    known = cutoff - timedelta(microseconds=1)
    view = tv.TimeView.both(valid, known) if valid is not None else tv.TimeView.at_known(known)
    result = tv.apply(view, [_as_envelope(row) for row in listed])
    by_id = {row.record_id: row for row in listed}
    members: dict[str, str] = {}
    values: dict[str, set[tuple[object, ...]]] = {}
    for hit in result.hits:
        if hit.known is not True or hit.valid is False or not hit.live:
            continue
        row = by_id[hit.record_id]
        if row.quarantined or not eng._consent_ok(row):
            continue
        if viewer is not None and not visible_to(row, viewer):
            continue
        raw = row.fingerprint.envelope
        if raw is None:
            continue
        env = SemanticEnvelope.model_validate(raw)
        if env.status == "hidden":
            continue
        # The time view has already re-opened only inferred closures that were
        # not known at this cutoff. Keep explicit claim content and evidence.
        env = env.model_copy(update={"status": hit.assertion.status})
        group = group_of(env, namespace, audience=audience_key(row))
        token = value_token(env) if group is not None else None
        if group is None or token is None:
            continue
        key = group_key(group)
        members[row.record_id] = key
        values.setdefault(key, set()).add(token)
    return {rid: len(values[key]) > 1 for rid, key in members.items()}


async def overlay_result(
    eng: Any,
    result: Any,
    *,
    viewer: str | None,
    cache: dict[tuple[str, str], tuple[bool, frozenset[str]]],
    historical: dict[str, bool] | None = None,
) -> Any:
    """Replace canonical envelope flags in returned records, recursively on copies."""
    if isinstance(result, MemoryRecord):
        if result.fingerprint.envelope is None:
            return result
        flag = (
            historical.get(result.record_id, False)
            if historical is not None
            else await _group_flag(eng, result, viewer, cache)
        )
        return _flagged(result, flag)
    if isinstance(result, list):
        return [
            await overlay_result(eng, item, viewer=viewer, cache=cache, historical=historical)
            for item in result
        ]
    if isinstance(result, tuple):
        return tuple(
            [
                await overlay_result(eng, item, viewer=viewer, cache=cache, historical=historical)
                for item in result
            ]
        )
    if isinstance(result, dict):
        return {
            key: await overlay_result(eng, item, viewer=viewer, cache=cache, historical=historical)
            for key, item in result.items()
        }
    if is_dataclass(result) and not isinstance(result, type):
        updates = {
            field.name: await overlay_result(
                eng, getattr(result, field.name), viewer=viewer, cache=cache, historical=historical
            )
            for field in fields(result)
        }
        if any(updates[name] is not getattr(result, name) for name in updates):
            return replace(result, **updates)
    return result
