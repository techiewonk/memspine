"""The single derived-write door (F-16a; M-09, G-08, ER-11, ER-22, POL-2).

Every derived record (mined or window fact, calibration fact, cue, list card, episode fragment,
lesson) goes through :func:`write_derived`. With ``write.lineage_table`` off it is exactly
``eng._write_locked(...)``: nothing else runs and nothing is written to the table. With the flag on:

* the parents are the ones the caller names, else the record's ``source.parents`` (the source turns
  when known, G-08); a derived record with no parent at all is refused (``derive.lineage.refused``);
* the parents' trusts join the trust cap, so the stored trust is at most the least-trusted parent
  (POL-2, ``trust.horizon.deposit_trust``);
* the record carries the typed envelope fields ``layer`` and ``fingerprint.derivation``;
* one lineage edge per parent is written after the record, with its role (support / binding /
  context ...), optional evidence span and pinned parent version (``derive.lineage.write``).

F-02a: ``layer`` and the derivation fingerprint are the typed record-envelope fields (no tags).
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
from memspine.store.layers import LAYERS, derive_layer
from memspine.store.records import MemoryRecord
from memspine.trust.horizon import deposit_trust
from memspine.trust.pipeline import consent as trust_consent
from memspine.trust.pipeline import gates_v2
from memspine.trust.visibility import restrict_to_parents

__all__ = ["DerivedWithoutParents", "write_derived"]


class DerivedWithoutParents(MemspineError):
    """A derived record had no parent and no source turn to inherit (G-08)."""


async def write_derived(
    eng: Any,
    storage: Any,
    ns: str,
    record: MemoryRecord,
    route: str,
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

    ``route`` is the write-ROUTE name handed to ``eng._write_locked`` ("semantic" runs the semantic
    pipeline, "summary" the plain WRITE route). It is not the memory type: the layer comes from
    ``record.memory_type`` and ``record.source.channel`` (NEW-03). Callers pass it positionally.

    ``roles`` maps a parent id to its lineage role (default ``support``); ``spans`` maps a parent id
    to the evidence span in that parent. The caller holds the namespace write lock, as before.

    ``firewall.gates_v2`` (W2-04, gate 3) and ``consent.v2`` (T5-03, ``may_derive``): a record the
    gates refuse is still written, but held in quarantine (inert, never served, never a parent of
    further derivation); the reasons ride the write door as ``GATE_HOLD``.
    """
    record = await _inherit_visibility(eng, storage, ns, record, parents)
    reasons: list[str] = []
    if gates_v2.gates_on(eng):
        pids = list(parents if parents is not None else record.source.parents)
        reasons.extend(await gates_v2.derive_hold(eng, storage, record, pids))
    if eng._config().consent.v2:
        await trust_consent.ensure_registry(eng)
        denied = trust_consent.derive_denial(eng, record)
        if denied is not None:
            reasons.append(f"consent_{denied}")
    if not reasons:
        return await _write_derived(
            eng, storage, ns, record, route, actor,
            parents, roles, spans, layer, trust_cap, site,
        )  # fmt: skip
    if tap.enabled():
        tap.event("derive.gate.hold", site=site, record=record.record_id, reasons=reasons)
    token = gates_v2.GATE_HOLD.set((record.record_id, tuple(reasons)))
    try:
        return await _write_derived(
            eng, storage, ns, record, route, actor,
            parents, roles, spans, layer, trust_cap, site,
        )  # fmt: skip
    finally:
        gates_v2.GATE_HOLD.reset(token)


async def _inherit_visibility(
    eng: Any,
    storage: Any,
    ns: str,
    record: MemoryRecord,
    parents: Sequence[str] | None,
) -> MemoryRecord:
    """F1: under ``isolation.viewer_required`` the record is never more visible than its parents.

    The named ``parents`` (else ``source.parents``) lend their participant and visibility tags, so
    a derived path that names parents the record does not cite in ``source.parents`` is covered
    too (the write door's governance sees only ``source.parents``). Flag off: untouched.
    """
    if not getattr(eng._config().isolation, "viewer_required", False):
        return record
    pids = [p for p in dict.fromkeys(parents if parents is not None else record.source.parents)]
    found = [await storage.get_record(p) for p in pids if p != record.record_id]
    visible = [r.tags for r in found if r is not None and r.namespace == ns]
    tags = restrict_to_parents(record.tags, visible)
    return record if tags == record.tags else record.model_copy(update={"tags": tags})


async def _write_derived(
    eng: Any,
    storage: Any,
    ns: str,
    record: MemoryRecord,
    route: str,
    actor: str,
    parents: Sequence[str] | None,
    roles: Mapping[str, str] | None,
    spans: Mapping[str, Span] | None,
    layer: str | None,
    trust_cap: list[float] | None,
    site: str,
) -> MemoryRecord:
    # MF-04: the layer is stamped before the lineage-off early return, so lineage on/off agree.
    # (Flag off stays otherwise untouched: no derivation fingerprint, no edges.)
    if not eng._config().write.lineage_table:
        record = record.model_copy(update={"layer": _resolve_layer(record, layer)})
        return await eng._write_locked(storage, ns, record, route, actor, trust_cap)

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
    record = stamp_derived(record, route=route, site=site, layer=layer)
    layer_label = record.layer

    written = await eng._write_locked(storage, ns, record, route, actor, cap or None)

    # POL-2 safety net: the firewall path caps through ``cap``; never leave a record above it.
    cap_value = deposit_trust(1.0, [min(cap)]) if cap else 1.0
    if written.record_id == record.record_id and written.trust > cap_value:
        written = written.model_copy(update={"trust": cap_value})
        await storage.upsert_record(written)

    return await add_lineage(
        storage, ns, written, pids, role_of, spans, site, parent_records, cap_value, layer_label
    )


def _resolve_layer(record: MemoryRecord, layer: str | None) -> str:
    """The layer a derived record sits on: the explicit ``layer`` (must be one of ``LAYERS``,
    MF-03: no silent fallback), else the representation map over ``record.memory_type`` and
    ``record.source.channel`` (MF-01)."""
    if layer is None:
        return derive_layer(record.memory_type, record.source.channel)
    if layer not in LAYERS:
        raise ValueError(f"unknown layer {layer!r}; expected one of {LAYERS}")
    return layer


def stamp_derived(
    record: MemoryRecord,
    route: str | None = None,
    site: str = "derive",
    layer: str | None = None,
) -> MemoryRecord:
    """The envelope fields the door sets: ``layer`` and ``fingerprint.derivation``.

    ``route`` is the write-route name (informational; kept so positional callers
    ``stamp_derived(record, "summary", site)`` still work). The memory type is
    ``record.memory_type``.

    NEW-02 compat note: the derivation fingerprint no longer hashes the layer label (only site and
    channel), so relabelling a layer never changes the fingerprint of identical content. Records
    written after this change carry a different ``fingerprint.derivation`` than the same content
    written before it; stored values are not rewritten.
    """
    layer_label = _resolve_layer(record, layer)
    fp = fingerprint_payload({"site": site, "channel": record.source.channel})[:16]
    return record.model_copy(
        update={
            "layer": layer_label,
            "fingerprint": record.fingerprint.model_copy(update={"derivation": fp}),
        }
    )


async def link_derived(
    storage: Any, ns: str, written: MemoryRecord, parents: Sequence[str], site: str
) -> MemoryRecord:
    """FU-SAFE: lineage edges for a derived record that was appended as a raw WRITE event (the
    legacy summary paths keep their event payload, e.g. ``consolidation``, for the graph projector).
    The record must already be stamped with :func:`stamp_derived`; nothing is written if no parent
    is named. Only called under ``write.lineage_table``."""
    pids = [p for p in dict.fromkeys(parents) if p != written.record_id]
    if not pids:
        return written
    found = [r for r in [await storage.get_record(p) for p in pids] if r is not None]
    return await add_lineage(
        storage, ns, written, pids, {p: "support" for p in pids}, None, site, found,
        deposit_trust(1.0, [min(r.trust for r in found)]) if found else 1.0, written.layer,
    )  # fmt: skip


async def add_lineage(
    storage: Any,
    ns: str,
    written: MemoryRecord,
    pids: Sequence[str],
    role_of: Mapping[str, str],
    spans: Mapping[str, Span] | None,
    site: str,
    parent_records: Sequence[MemoryRecord],
    cap_value: float,
    layer_label: str | None,
) -> MemoryRecord:
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
