"""Blast-radius audit (E1): ``audit taint`` as a log walk.

Because every mutation is an event (D0.1) and provenance is first-class
(D-42/D-43), tracing a poisoned memory is pure reading: find the origin WRITE,
then every derivation — merges that absorbed it, summaries that consolidated
it, supersession chains that grew from it. No inference, no heuristics; the
report is exactly what the log proves.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from memspine.core.events import EventKind, MemoryEvent

__all__ = ["IntegrityReport", "TaintReport", "trace_taint", "verify_events"]


class _EventSource(Protocol):
    async def read_events(self, after_seq: int = 0, limit: int = 1000) -> list[MemoryEvent]: ...


class TaintReport(BaseModel):
    """Everything the log proves about one record's origin and reach."""

    record_id: str
    origin_seq: int | None = None
    origin_actor: str | None = None
    origin_source: dict[str, object] | None = None
    #: Namespace of the first log event that mentions the seed record — the
    #: seed's home namespace, usable for ownership scoping even after the read
    #: model row is gone (merged away, hard-deleted). ``None`` = never seen.
    origin_namespace: str | None = None
    #: record_ids whose state the tainted record influenced, with the proof.
    descendants: dict[str, str] = Field(default_factory=dict)
    #: every event seq that mentions the record (the full evidence trail).
    event_seqs: list[int] = Field(default_factory=list)
    #: Cross-namespace walk only (``follow_parents``): the namespace each
    #: descendant lives in, and every reader namespace an EXPOSE event shows
    #: received tainted content (reader -> first exposure seq).
    descendant_namespaces: dict[str, str] = Field(default_factory=dict)
    exposed_namespaces: dict[str, int] = Field(default_factory=dict)

    @property
    def blast_radius(self) -> int:
        return len(self.descendants)


async def _all_events(storage: _EventSource) -> list[MemoryEvent]:
    events: list[MemoryEvent] = []
    after = 0
    while True:
        batch = await storage.read_events(after_seq=after)
        if not batch:
            return events
        events.extend(batch)
        assert batch[-1].seq is not None
        after = batch[-1].seq


def _snapshot_id(event: MemoryEvent) -> str | None:
    snapshot = event.payload.get("record")
    if isinstance(snapshot, dict):
        raw = snapshot.get("record_id")
        return str(raw) if raw is not None else None
    return None


async def trace_taint(
    storage: _EventSource, record_id: str, follow_parents: bool = False
) -> TaintReport:
    """Walk the log once; expand the tainted set transitively (a summary built
    from a tainted episode taints whatever later merges with the summary).

    ``source.parents`` (the ``derived_from`` lineage a writer declared, mined
    facts' turns, cue targets, reflection evidence) are always followed within
    a namespace (R2-10). ``follow_parents`` (MTI forensics) also follows them
    across grant boundaries and reports EXPOSE readers.

    A record DISPLACED by a tainted statement (``superseded_by_taint``: a clean
    fact archived by a poisoned UPDATE/INVALIDATE) is reported, but it does not
    propagate taint: its content is not the poison's, so what was derived from
    it is not tainted, and rollback restores it rather than archiving it.
    """
    events = await _all_events(storage)
    report = TaintReport(record_id=record_id)
    tainted: set[str] = {record_id}
    displaced: set[str] = set()
    write_namespace: dict[str, str] = {}

    changed = True
    while changed:  # transitive closure over derivations
        changed = False
        for event in events:
            assert event.seq is not None
            payload = event.payload
            if event.kind is EventKind.WRITE:
                snapshot_id = _snapshot_id(event)
                if snapshot_id == record_id and report.origin_seq is None:
                    report.origin_seq = event.seq
                    report.origin_actor = event.actor
                    snapshot = payload.get("record")
                    if isinstance(snapshot, dict):
                        source = snapshot.get("source")
                        if isinstance(source, dict):
                            report.origin_source = source
                if snapshot_id is not None:
                    write_namespace.setdefault(snapshot_id, event.namespace)
                # Derivation provenance rides the WRITE payload: consolidation
                # (P3.1) and reflection (M13.7/P5) both name their members.
                for derivation_key, label in (
                    ("consolidation", "consolidated"),
                    ("reflection", "reflected"),
                ):
                    derivation = payload.get(derivation_key)
                    if isinstance(derivation, dict) and snapshot_id is not None:
                        members = derivation.get("member_record_ids")
                        if (
                            isinstance(members, list)
                            and tainted & set(map(str, members))
                            and snapshot_id not in tainted
                        ):
                            tainted.add(snapshot_id)
                            report.descendants[snapshot_id] = f"{label}@{event.seq}"
                            changed = True
                if snapshot_id is not None and snapshot_id not in tainted:
                    snapshot = payload.get("record")
                    source = snapshot.get("source") if isinstance(snapshot, dict) else None
                    parents = source.get("parents") if isinstance(source, dict) else None
                    hit = tainted & set(map(str, parents)) if isinstance(parents, list) else set()
                    if hit and (
                        follow_parents
                        or any(write_namespace.get(p) == event.namespace for p in hit)
                    ):
                        tainted.add(snapshot_id)
                        report.descendants[snapshot_id] = f"derived@{event.seq}"
                        changed = True
            elif event.kind is EventKind.MERGE:
                kept = str(payload.get("kept_record_id", ""))
                dropped_snapshot = payload.get("dropped_record")
                dropped = (
                    str(dropped_snapshot.get("record_id", ""))
                    if isinstance(dropped_snapshot, dict)
                    else ""
                )
                if dropped in tainted and kept and kept not in tainted:
                    tainted.add(kept)
                    report.descendants[kept] = f"merged@{event.seq}"
                    changed = True
                # Taint flows both ways: merging INTO a tainted record taints
                # nothing new, but the merged row absorbed tainted content.
                if kept in tainted and dropped and dropped not in tainted:
                    tainted.add(dropped)
                    report.descendants[dropped] = f"absorbed_by@{event.seq}"
                    changed = True
            elif event.kind is EventKind.CONSOLIDATE:
                members = payload.get("member_record_ids")
                summary_id = str(payload.get("summary_record_id", ""))
                if (
                    isinstance(members, list)
                    and tainted & set(map(str, members))
                    and summary_id
                    and summary_id not in tainted
                ):
                    tainted.add(summary_id)
                    report.descendants[summary_id] = f"consolidated@{event.seq}"
                    changed = True
            elif event.kind is EventKind.LINK:
                # An association gives the tainted record graph reach into its
                # partner (PPR recall surfaces it): the partner is a linked
                # descendant. A weight-0 tombstone (ADR-015 prune/supersede)
                # removed that reach — it stays evidence via event_seqs below
                # but must not propagate taint.
                raw_weight = payload.get("weight", 1.0)
                weight = (
                    float(raw_weight)
                    if isinstance(raw_weight, int | float) and not isinstance(raw_weight, bool)
                    else 1.0
                )
                if weight > 0.0:
                    src = str(payload.get("src", ""))
                    dst = str(payload.get("dst", ""))
                    for tainted_end, other in ((src, dst), (dst, src)):
                        if tainted_end in tainted and other and other not in tainted:
                            tainted.add(other)
                            report.descendants[other] = f"linked@{event.seq}"
                            changed = True
            elif event.kind is EventKind.CONFLICT:
                # A supersession seeded by a tainted incoming record.
                incoming = payload.get("incoming_record")
                if isinstance(incoming, dict):
                    incoming_id = str(incoming.get("record_id", ""))
                    existing_id = str(payload.get("existing_record_id", ""))
                    if (
                        incoming_id in tainted
                        and existing_id
                        and payload.get("verdict") in ("update", "invalidate")
                        and existing_id not in tainted
                        and existing_id not in displaced
                    ):
                        displaced.add(existing_id)
                        report.descendants[existing_id] = f"superseded_by_taint@{event.seq}"

    seed = {record_id}
    for event in events:
        assert event.seq is not None
        # The seed's home namespace = the first (lowest-seq) event that names
        # it. Events are namespace-tagged and read in seq order, so this holds
        # even when the read-model row is gone (merged/hard-deleted).
        if report.origin_namespace is None and _event_references(event.payload, seed):
            report.origin_namespace = event.namespace
        if _event_references(event.payload, tainted | displaced):
            report.event_seqs.append(event.seq)
        if follow_parents and event.kind is EventKind.EXPOSE:
            exposed = event.payload.get("record_ids")
            reader = str(event.payload.get("reader_namespace", event.namespace))
            if isinstance(exposed, list) and tainted & set(map(str, exposed)):
                report.exposed_namespaces.setdefault(reader, event.seq)
    if follow_parents:
        report.descendant_namespaces = {
            rid: write_namespace[rid] for rid in report.descendants if rid in write_namespace
        }
    return report


def _event_references(node: Any, ids: set[str]) -> bool:
    """Structured (not substring) check: does this payload name any tainted
    record in a record-id-bearing field? Substring matching over ``str(payload)``
    would false-positive on ids quoted inside unrelated content."""
    if isinstance(node, dict):
        for key, value in node.items():
            if (
                key
                in (
                    "record_id",
                    "summary_record_id",
                    "existing_record_id",
                    "kept_record_id",
                    "src",  # LINK payloads (ADR-015): tombstones stay evidence
                    "dst",
                )
                and value in ids
            ):
                return True
            if key == "member_record_ids" and isinstance(value, list) and set(value) & ids:
                return True
            if _event_references(value, ids):
                return True
        return False
    if isinstance(node, list):
        return any(_event_references(item, ids) for item in node)
    return False


class IntegrityReport(BaseModel):
    """B2': what an offline pass over the log proves (``Engine.verify_integrity``)."""

    events: int = 0
    chain_head: str = ""
    keyed: bool = False
    head_matches: bool | None = None  # None when no expected head was supplied
    fingerprint_mismatches: list[int] = Field(default_factory=list)  # event seqs
    checked_writes: int = 0
    mti_violations: list[str] = Field(default_factory=list)  # record ids

    @property
    def ok(self) -> bool:
        return (
            not self.fingerprint_mismatches
            and not self.mti_violations
            and self.head_matches is not False
        )


def verify_events(
    events: list[MemoryEvent],
    view: Any,
    key: bytes | None = None,
    expected_head: str | None = None,
) -> IntegrityReport:
    """Chain + fingerprint + offline MTI recomputation over ``events`` (in seq order).

    - **Chain.** ``h_i = H(h_{i-1} || seq || kind || namespace || sha256(payload))``,
      with ``H`` = HMAC-SHA256 under ``key`` (tamper-evident without an anchor) or
      plain SHA-256 (tamper-evident against an externally stored head).
    - **Fingerprints.** Each stored xxh64 fingerprint must match its payload.
    - **MTI.** Every WRITE whose record declares ``source.parents`` must satisfy
      ``trust <= min(view(parent_trust, parent_ns, ns))`` over parents already
      written: the invariant recomputed from the log alone, no engine state.
      ``view(trust, grantor, grantee)`` is the integrity policy's view function.
    """
    import hashlib
    import hmac

    from memspine.core.events import canonical_payload, fingerprint_payload

    report = IntegrityReport(keyed=key is not None)
    head = b"memspine-log-v1"
    trust: dict[str, tuple[float, str]] = {}  # record_id -> (trust, namespace)
    for event in events:
        report.events += 1
        body = canonical_payload(event.payload)
        if event.fingerprint and fingerprint_payload(event.payload) != event.fingerprint:
            report.fingerprint_mismatches.append(int(event.seq or -1))
        message = b"|".join(
            [
                head,
                str(event.seq).encode(),
                event.kind.value.encode(),
                event.namespace.encode(),
                hashlib.sha256(body).digest(),
            ]
        )
        head = (
            hmac.new(key, message, hashlib.sha256).digest()
            if key is not None
            else hashlib.sha256(message).digest()
        )
        if event.kind is not EventKind.WRITE:
            continue
        snapshot = event.payload.get("record")
        if not isinstance(snapshot, dict):
            continue
        rid = str(snapshot.get("record_id", ""))
        ns = str(snapshot.get("namespace", event.namespace))
        value = float(snapshot.get("trust", 0.0))
        source = snapshot.get("source") or {}
        parents = source.get("parents") if isinstance(source, dict) else None
        if isinstance(parents, list) and parents:
            report.checked_writes += 1
            views = [view(trust[p][0], trust[p][1], ns) for p in map(str, parents) if p in trust]
            if views and value > min(views) + 1e-9:
                report.mti_violations.append(rid)
        trust[rid] = (value, ns)
    report.chain_head = head.hex()
    if expected_head is not None:
        report.head_matches = hmac.compare_digest(report.chain_head, expected_head)
    return report
