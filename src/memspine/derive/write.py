"""The single derived-write door (F-16a; M-09, G-08, ER-11, ER-22, POL-2).

Every derived record (mined or window fact, calibration fact, cue, list card, episode fragment,
lesson) goes through :func:`write_derived`. With ``write.lineage_table`` off it is exactly
``eng._write_locked(...)``: nothing else runs and nothing is written to the table. With the flag on:

* the parents are the ones the caller names, else the record's ``source.parents`` (the source turns
  when known, G-08); a derived record with no parent at all is refused (``derive.lineage.refused``);
* the parents' trusts join the trust cap, so the stored trust is at most the least-trusted parent
  (POL-2, ``trust.horizon.deposit_trust``);
* the record is tagged ``layer:<label>`` and ``deriv:<fingerprint>``;
* one lineage edge per parent is written after the record, with its role (support / binding /
  context ...), optional evidence span and pinned parent version (``derive.lineage.write``).

TODO(F-02a): ``layer`` and the derivation fingerprint are tags until the record-envelope columns
land; switch them to the typed fields then.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from memspine.exceptions import MemspineError
from memspine.observability import tap
from memspine.schemas.durable.evidence import Span
from memspine.schemas.durable.lineage import LINEAGE_ROLES, LineageEdge
from memspine.store import lineage as lineage_store
from memspine.store.events import fingerprint_payload
from memspine.store.records import MemoryRecord
from memspine.trust.horizon import deposit_trust

__all__ = ["DerivedWithoutParents", "write_derived"]


class DerivedWithoutParents(MemspineError):
    """A derived record had no parent and no source turn to inherit (G-08)."""


async def write_derived(
    eng: Any,
    storage: Any,
    ns: str,
    record: MemoryRecord,
    memory_type: str,
    actor: str,
    *,
    parents: Sequence[str] | None = None,
    roles: Mapping[str, str] | None = None,
    spans: Mapping[str, Span] | None = None,
    layer: str | None = None,
    trust_cap: list[float] | None = None,
    site: str = "derive",
) -> MemoryRecord:
    """Write one derived record. See the module docstring for flag-on behaviour.

    ``roles`` maps a parent id to its lineage role (default ``support``); ``spans`` maps a parent id
    to the evidence span in that parent. The caller holds the namespace write lock, as before.
    """
    if not eng._config().write.lineage_table:
        return await eng._write_locked(storage, ns, record, memory_type, actor, trust_cap)

    pids = [p for p in dict.fromkeys(parents if parents is not None else record.source.parents)]
    pids = [p for p in pids if p != record.record_id]
    if not pids:
        if tap.enabled():
            tap.event(
                "derive.lineage.refused", site=site, record=record.record_id, reason="no_parents"
            )
        raise DerivedWithoutParents(
            f"derived record {record.record_id!r} ({site}) has no parent; lineage is enforced"
        )
    role_of = {p: (roles or {}).get(p, "support") for p in pids}
    bad = sorted({r for r in role_of.values() if r not in LINEAGE_ROLES})
    if bad:
        raise ValueError(f"unknown lineage role(s) {bad}")

    parent_records = [r for r in [await storage.get_record(p) for p in pids] if r is not None]
    cap = list(trust_cap or [])
    cap.extend(r.trust for r in parent_records)
    layer_label = layer or memory_type
    fp = fingerprint_payload(
        {"site": site, "channel": record.source.channel, "layer": layer_label}
    )[:16]
    tags = list(record.tags)
    for tag in (f"layer:{layer_label}", f"deriv:{fp}"):
        if tag not in tags:
            tags.append(tag)
    record = record.model_copy(update={"tags": tags})

    written = await eng._write_locked(storage, ns, record, memory_type, actor, cap or None)

    # POL-2 safety net: the firewall path caps through ``cap``; never leave a record above it.
    cap_value = deposit_trust(1.0, [min(cap)]) if cap else 1.0
    if written.record_id == record.record_id and written.trust > cap_value:
        written = written.model_copy(update={"trust": cap_value})
        await storage.upsert_record(written)

    by_id = {r.record_id: r for r in parent_records}
    edges = [
        LineageEdge(
            child=written.record_id,
            parent=p,
            role=role_of[p],  # type: ignore[arg-type]
            parent_version=by_id[p].version if p in by_id else None,
            span=(spans or {}).get(p),
        )
        for p in pids
        if p != written.record_id
    ]
    added = await lineage_store.add_edges(storage._client.engine, edges, ns)
    if tap.enabled():
        tap.event(
            "derive.lineage.write",
            site=site,
            record=written.record_id,
            parents=pids,
            roles=sorted(set(role_of.values())),
            edges=added,
            trust=written.trust,
            trust_cap=cap_value,
            layer=layer_label,
        )
    return written
