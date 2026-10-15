# ruff: noqa: E501
"""The Memory Firewall at its gates, wired from ``trust.gates`` (W2-04, LIB-38; ``firewall.gates_v2``).

Adapters only: they turn engine records into the library's inputs and its verdicts back into what the
engine already acts on. With the flag off nothing here runs. The decision rule, trust matrix, quarantine
floor and the cap ``tau <= min(parent views)`` are the shipped N1 semantics; the library adds the
de/nl/es instruction signals, the MINJA recall-phrase bridge, the derived-record gate and the S4-4 family
rule for release.

* gate 1 :func:`assess_insert` replaces ``Firewall.assess`` in ``_assess_write``;
* gate 2 :func:`filter_input` runs on the turns an extraction stage reads;
* gate 3 :func:`derive_hold` runs in the derived-write door; a hold quarantines the derived record
  (stored inert, never served) through the ``GATE_HOLD`` context variable the write door reads;
* gate 4 :func:`read_item` runs where evidence is admitted (own and foreign reads);
* release :func:`release_ok` adds the family rule to quarantine corroboration.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextvars import ContextVar
from typing import Any

from memspine.config import constants
from memspine.core import trace_sink
from memspine.observability import tap
from memspine.schemas.durable.lineage import LineageEdge
from memspine.store.records import MemoryRecord, RecordStatus
from memspine.trust.consent import Viewer, ViewerKind
from memspine.trust.families import (
    SourceDescriptor,
    StoredRecord,
    family_of,
)
from memspine.trust.firewall import Firewall, FirewallSignals, FirewallVerdict
from memspine.trust.gates import (
    DerivedMeta,
    GatePolicy,
    GateVerdict,
    ParentRef,
    ReadItem,
    Signal,
    SignalConfig,
    TurnMeta,
    build_signals,
    gate_derive,
    gate_derive_input,
    gate_insert,
    gate_read,
    release_quarantine,
)
from memspine.trust.pipeline.state import _fact_value

#: Reasons that hold the derived record being written (set around the derived-write door).
GATE_HOLD: ContextVar[tuple[str, tuple[str, ...]] | None] = ContextVar(
    "memspine_gate_hold", default=None
)

_EXTERNAL_DOC_CHANNELS = frozenset({"retrieved", "external", "email", "mcp", "ingest", "rest"})


def config_gates_on(config: Any) -> bool:
    return bool(config.firewall.gates_v2) and bool(config.firewall.enabled)


def gates_on(eng: Any) -> bool:
    return config_gates_on(eng._config())


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


def _codes(reasons: Sequence[str]) -> list[str]:
    return [r.split("(", 1)[0] for r in reasons]


def _emit(gate: int | str, verdict: GateVerdict, record_id: str = "") -> None:
    if tap.enabled():
        tap.event(
            "trust.gate",
            gate=gate,
            id=record_id or None,
            action=verdict.action,
            trust=verdict.trust,
            reasons=_codes(verdict.reasons),
            signals=list(verdict.signals_run),
            label=verdict.label,
        )


# --------------------------------------------------------------------------- policy and signals


def gate_policy(firewall: Firewall, *, derive: bool = False) -> GatePolicy:
    """The library thresholds, taken from the bound trust policy (so config overrides carry over)."""
    options = firewall.policy.options
    kw: dict[str, Any] = {
        "role_trust": dict(options.role_trust),  # type: ignore[attr-defined]
        "default_trust": options.default_trust,  # type: ignore[attr-defined]
        "retrieved_cap": options.retrieved_content_cap,  # type: ignore[attr-defined]
        "quarantine_below": options.quarantine_below,  # type: ignore[attr-defined]
    }
    if not derive:
        # Gate 1 keeps today's rule: only tool and assistant authorship is an untrusted origin
        # besides the external channels. Gate 3 uses the library default (derived text too).
        kw["untrusted_roles"] = frozenset({"tool", "assistant"})
    return GatePolicy(**kw)


def _signal_config(signals: FirewallSignals, *, derive: bool = False) -> SignalConfig:
    return SignalConfig(
        instruction=signals.instruction,
        anomaly=signals.anomaly and not derive,
        minja_bridge=signals.minja_bridge and not derive,
        instruction_extended=signals.instruction_extended,
        semantic_risk=signals.semantic_risk,
        query_anomaly=signals.query_anomaly and not derive,
        minja_bridge_exempt_roles=tuple(signals.minja_bridge_exempt_roles),
        anomaly_exempt_roles=tuple(signals.anomaly_exempt_roles),
        minja_bridge_prefix_chars=signals.minja_bridge_prefix_chars
        or constants.MINJA_BRIDGE_PREFIX_CHARS,
    )


def signal_stack(signals: FirewallSignals, *, derive: bool = False) -> tuple[Signal, ...]:
    """The library stack for the configured switches. The anomaly thresholds are the engine's own."""
    from memspine.trust.gates import AnomalySignal

    cfg = _signal_config(signals, derive=derive)
    stack = list(build_signals(cfg))
    return tuple(
        AnomalySignal(
            min_neighbours=constants.ANOMALY_MIN_NEIGHBOURS,
            outlier_similarity=constants.ANOMALY_CENTROID_MIN_SIMILARITY,
            exempt_roles=cfg.anomaly_exempt_roles,
        )
        if isinstance(s, AnomalySignal)
        else s
        for s in stack
    )


# --------------------------------------------------------------------------- gate 1


def assess_insert(
    firewall: Firewall,
    record: MemoryRecord,
    neighbour_similarities: list[float] | None = None,
    recent_contents: list[str] | None = None,
    query_anomaly: float | None = None,
) -> FirewallVerdict:
    """Gate 1 for one write: the same inputs as ``Firewall.assess``, the library's verdict."""
    policy = firewall.policy
    trust = policy.tier_capped(policy.trust_at_write(record.source), record)
    meta = TurnMeta(
        record_id=record.record_id,
        role=record.source.role,
        channel=record.source.channel,
        trust=_clamp(trust),
        recent_contents=tuple(recent_contents or ()),
        neighbour_similarities=tuple(neighbour_similarities or ()),
        query_anomaly_z=query_anomaly if firewall.signals.query_anomaly else None,
    )
    verdict = gate_insert(
        meta,
        record.content,
        signal_stack(firewall.signals),
        policy=gate_policy(firewall),
    )
    reasons = [r for r in verdict.reasons if r != "quarantined"]
    quarantine = verdict.quarantine
    stamped = record.model_copy(update={"trust": trust})
    if policy.needs_evidence(stamped):  # N26: held, not rejected
        quarantine = True
        reasons.append(f"pending_evidence(tier={policy.source_tier(stamped)})")
    if quarantine:
        reasons.append("quarantined")
    _emit(1, verdict, record.record_id)
    if trace_sink.write_active():
        trace_sink.write_note(
            "firewall",
            record_id=record.record_id,
            gate=1,
            role=record.source.role,
            channel=record.source.channel,
            trust=trust,
            signals=list(verdict.signals_run),
            instruction_flag=verdict.instruction_flag,
            anomalous=verdict.anomalous,
            quarantine=quarantine,
            reasons=list(reasons),
        )
    return FirewallVerdict(
        trust=trust,
        instruction_flag=verdict.instruction_flag,
        anomalous=verdict.anomalous,
        quarantine=quarantine,
        reasons=reasons,
    )


# --------------------------------------------------------------------------- gate 2


def filter_input(members: Sequence[MemoryRecord]) -> list[MemoryRecord]:
    """Gate 2: the turns an extraction stage may read. Quarantined, dead or unreadable turns are
    excluded (the stage renders what is kept as data)."""
    refs = [
        ParentRef(
            record_id=m.record_id,
            trust=_clamp(m.trust),
            quarantined=m.quarantined,
            visible=m.status is not RecordStatus.DELETED,
            live=m.status is RecordStatus.ACTIVATED,
        )
        for m in members
    ]
    kept, dropped = gate_derive_input(refs)
    if tap.enabled():
        tap.event(
            "trust.gate",
            gate=2,
            action="accept" if not dropped else "filter",
            kept=len(kept),
            dropped=len(dropped),
            reasons=["input_excluded"] if dropped else [],
        )
    keep_ids = {k.record_id for k in kept}
    return [m for m in members if m.record_id in keep_ids]


# --------------------------------------------------------------------------- gate 3


async def derive_hold(
    eng: Any, storage: Any, record: MemoryRecord, parent_ids: Sequence[str]
) -> tuple[str, ...]:
    """Gate 3 for one derived record: the reasons it must be held in quarantine (empty: admitted).

    The trust cap itself stays with the write door (``trust_cap``) and the POL-2 safety net."""
    policy = eng._integrity()
    refs: list[ParentRef] = []
    for pid in dict.fromkeys(p for p in parent_ids if p != record.record_id):
        parent = await storage.get_record(pid)
        if parent is None:
            refs.append(ParentRef(record_id=pid, trust=0.0, visible=False, live=False))
            continue
        view = (
            policy.view_trust(parent.trust, parent.namespace, record.namespace)
            if policy.enabled
            else parent.trust
        )
        refs.append(
            ParentRef(
                record_id=pid,
                trust=_clamp(parent.trust),
                quarantined=parent.quarantined,
                view_trust=_clamp(view),
                visible=parent.status is not RecordStatus.DELETED,
                live=parent.status is RecordStatus.ACTIVATED,
            )
        )
    firewall: Firewall = eng._firewall
    verdict = gate_derive(
        DerivedMeta(record_id=record.record_id, base_trust=_clamp(record.trust)),
        refs,
        text=record.content,
        signals=signal_stack(firewall.signals, derive=True),
        policy=gate_policy(firewall, derive=True),
    )
    _emit(3, verdict, record.record_id)
    if verdict.action == "accept":
        return ()
    return tuple(_codes([r for r in verdict.reasons if r not in ("quarantined", "rejected")]))


# --------------------------------------------------------------------------- gate 4


def read_item(
    record: MemoryRecord,
    viewer_ns: str,
    *,
    grants: Sequence[Any] | None = None,
    min_view_trust: float = 0.0,
) -> GateVerdict:
    """Gate 4: may ``record`` be rendered to the reader in ``viewer_ns``? Own records need no grant;
    a foreign one needs a visible grant chain (``grants``) from its namespace to the viewer."""
    item = ReadItem(
        record_id=record.record_id,
        owner=record.namespace,
        scope="*",
        trust=_clamp(record.trust),
        quarantined=record.quarantined,
        instruction_flag=record.instruction_flag,
    )
    viewer = Viewer(tenant=viewer_ns, principal=viewer_ns, kind=ViewerKind.OWNER)
    verdict = gate_read(item, viewer, grants, min_view_trust=min_view_trust)
    _emit(4, verdict, record.record_id)
    return verdict


# --------------------------------------------------------------------------- release (S4-4)


def _descriptor(record: MemoryRecord) -> SourceDescriptor:
    src = record.source
    role, channel = src.role, src.channel
    if role == "tool":
        return SourceDescriptor(kind="tool", tool_name=channel)
    if channel == "web":
        return SourceDescriptor(kind="web", url=src.doc_path)
    if channel in _EXTERNAL_DOC_CHANNELS:
        return SourceDescriptor(kind="document", document_id=src.doc_path or src.message_id)
    if role == "user":
        return SourceDescriptor(kind="person", principal=src.principal)
    if role == "operator":
        return SourceDescriptor(kind="operator", principal=src.principal)
    if role == "system":
        return SourceDescriptor(kind="system", principal=src.principal)
    if role == "assistant":
        return SourceDescriptor(kind="assistant", principal=src.principal)
    return SourceDescriptor(kind="derived")


def _stored(record: MemoryRecord) -> StoredRecord:
    keyed = record.entity is not None and record.attribute is not None
    return StoredRecord(
        record_id=record.record_id,
        source=_descriptor(record),
        trust=_clamp(record.trust),
        quarantined=record.quarantined,
        memory_type=record.memory_type,
        entity=record.entity,
        attribute=record.attribute,
        value=_fact_value(record.content) if keyed else None,
        content_fingerprint=record.content_fingerprint,
    )


async def _lineage(
    storage: Any, record: MemoryRecord, depth: int = 8
) -> dict[str, list[LineageEdge]]:
    """Edges of ``record``'s ``source.parents`` ancestry, each carrying its parent's family."""
    edges: dict[str, list[LineageEdge]] = {}
    frontier = [record]
    seen = {record.record_id}
    for _ in range(depth):
        nxt: list[MemoryRecord] = []
        for child in frontier:
            for pid in child.source.parents:
                if pid == child.record_id:
                    continue
                parent = await storage.get_record(pid)
                if parent is None:
                    continue
                fam = None if parent.source.parents else str(family_of(_descriptor(parent)))
                edges.setdefault(child.record_id, []).append(
                    LineageEdge(
                        child=child.record_id, parent=pid, role="support", source_family=fam
                    )
                )
                if pid not in seen:
                    seen.add(pid)
                    nxt.append(parent)
        if not nxt:
            break
        frontier = nxt
    return edges


async def release_ok(eng: Any, held: MemoryRecord, incoming: MemoryRecord) -> bool:
    """S4-4: may ``incoming`` count as an independent second source for the quarantined ``held``?

    Only a person, operator or system family that is not one of the held record's families can
    (LIB-08 ``corroboration``); a summary or echo takes its parents' families, so it cannot vouch."""
    storage = eng._require_started()
    edges = await _lineage(storage, held)
    for child, es in (await _lineage(storage, incoming)).items():
        edges.setdefault(child, []).extend(es)
    verdict = release_quarantine(
        _stored(held),
        [_stored(incoming)],
        lineage_lookup=lambda rid: edges.get(rid, ()),
        default_trust=constants.TRUST_DEFAULT,
    )
    if tap.enabled():
        tap.event(
            "trust.gate",
            gate="release",
            id=held.record_id,
            action="accept" if verdict.release else "reject",
            reasons=[reason for _, reason in verdict.checked],
        )
    return verdict.release
