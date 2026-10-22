"""Derive stage: compile_edges (F-02c, ``graph.edge_compiler_v2``): assertions -> typed graph edges.

The wiring of the edge compiler (LIB-35, ``memspine.derive.edges``) and the link projection. Off by
default; the stage then reports ``skipped`` and the sleep cycle does not schedule it.

* **EdgeCompiler** (pure): a live or superseded semantic fact becomes an assertion envelope (F-02a
  ``store.envelope.to_envelope``, which already carries the F-02b tri-temporal fields: ``source_at``,
  ``event_precision``, ``event_basis``), enriched in code from the record tags an ``extract_graph`` edge
  fact carries (``rel:``, ``dst:``, ``kind:``): subject and object mentions are resolved to the existing
  entity-node ids (``ent:<namespace>:<canonical>``, the entity policy applied), and the scope is the typed
  tenant + owner when ``isolation.scope_keys_v2`` is on (M-05/M-16: entity scope, the session is never part
  of a key). The library then emits an assertion node, role edges and one binary projection edge per
  fact (n-ary roles, A-B-A occurrence keys). Tentative rule (S5-3): hypothetical / planned / reported /
  question content, assistant-proposed content that no one confirmed, a partly bound edge (the object
  was rejected as an entity) and an unbound edge are tagged ``tentative``; default reads exclude them.
* **LinkProjection**: writes the delta as LINK events (replayable, ``payload["props"]`` carries the
  typed edge properties, ``src_node`` / ``dst_node`` the labels of the nodes this layer owns), only for
  edges whose stored form differs (idempotent re-projection appends nothing). A record that is gone,
  hidden, quarantined or archived without a successor loses its edges (tombstone LINK events, then the
  assertion node and the value / time nodes nobody else uses are deleted); a superseded record keeps
  closed edges (``valid_to``). :meth:`LinkProjection.retract_records` is the erasure hook (lineage: edges
  carry the source ``record_id``).
* **Evidence erasure** (RM-89): erasing a record that is *evidence* of another claim (one of its
  ``evidence_ids``, not its source record) removes the id from the edge's evidence, marks the edge
  contested and tombstones it once no evidence remains. The erased ids ride in the edge properties
  (``evidence_erased``), so a later re-projection never restores them and replay reproduces the state.
* **Qualifiers** (RM-92): every projected edge carries typed :class:`EdgeQualifiers`; a value the
  compiler does not know stays unknown. :func:`requalify_namespace` (CLI ``memspine graph
  requalify``) re-projects one namespace on demand (RM-100); the sleep cycle does the same for
  every namespace, rewriting only edges whose stored form differs.

Edge properties hold ids, times, flags and trust only; never record text.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, get_args

from memspine.config.constants import EDGE_LINEAGE_MAX_DEPTH
from memspine.derive.edges import GraphDelta, GraphEdge, GraphNode, compile_edges
from memspine.derive.fact_policy import FactPurpose
from memspine.derive.pipeline.stages.context import (
    PipelineContext,
    live_record,
    live_records,
    sweep_namespaces,
)
from memspine.derive.pipeline.stages.envelope_adapter import is_fact
from memspine.derive.sensitivity import (
    NOT_CHECKED,
    ClaimText,
    SensitivityLabeller,
    SensitivityResult,
)
from memspine.memories.associative.entities import (
    EntityPolicy,
    canonical_entity,
    entity_node_id,
    is_entity_node,
)
from memspine.memories.associative.links import RESERVED_RELS
from memspine.observability import tap
from memspine.observability.logging import get_logger
from memspine.schemas.durable.assertion import AssertionEnvelope, Mention
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.identity import AssertionKey, occurrence_id
from memspine.scope import Scope
from memspine.semantics.domains import (
    AnyTopicTagger,
    ResolvedDomain,
    atopic_domains,
    resolve_domain,
)
from memspine.semantics.lifecycle import LIFECYCLE_OUTCOMES, LIFECYCLE_STATUSES
from memspine.semantics.origins import SourceOrigin
from memspine.semantics.registry import (
    SemanticRegistry,
    load_builtin_packs,
    load_builtin_registry,
)
from memspine.services.graph.base import GraphEdge as StoredEdge
from memspine.services.graph.base import GraphStore
from memspine.services.graph.qualifiers import (
    QUALIFIERS_PROP,
    AssertionModality,
    BindingStatus,
    ClaimState,
    EdgeQualifiers,
    ErasureState,
    Polarity,
    Provenance,
    TimePrecision,
    edge_qualifiers,
)
from memspine.store.envelope import to_envelope
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import MemoryRecord

_log = get_logger(__name__)

__all__ = [
    "COMPILED",
    "CompiledEdges",
    "EdgeCompiler",
    "LinkProjection",
    "ProjectionStats",
    "attach_lineage",
    "compile_graph_edges",
    "edge_qualifiers_for",
    "requalify_namespace",
    "retract_after_forget",
    "source_origin_for",
]

#: The marker every projected edge and node carries.
COMPILED = "edge_compiler_v2"
#: Role-edge relation prefix in the graph store (the projection edge keeps the relation label).
ROLE_PREFIX = "role:"
_TENTATIVE_MODALITIES = ("hypothetical", "planned", "reported", "question")
_TENTATIVE_TAGS = frozenset({"tentative", *_TENTATIVE_MODALITIES})

AppendEvent = Callable[[MemoryEvent], Awaitable[None]]


def _tag(record: MemoryRecord, prefix: str) -> str | None:
    return next((t[len(prefix) :] for t in record.tags if t.startswith(prefix)), None)


#: RM-85: write channel -> source origin, whatever the provenance class.
_CHANNEL_ORIGIN: dict[str, SourceOrigin] = {
    "reflection": "reflection",
    "observation": "agent_observation",
    "agent_tool": "tool_output",
}
#: RM-85: (provenance class, write channel) -> source origin.
_CLASS_CHANNEL_ORIGIN: dict[tuple[str, str], SourceOrigin] = {
    ("user_stated", "message"): "raw_message",
    ("assistant_proposed", "message"): "raw_message",
    ("user_confirmed", "message"): "raw_message",
    ("document", "ingest"): "document",
}
#: RM-85: provenance class -> source origin, whatever the channel.
_CLASS_ORIGIN: dict[str, SourceOrigin] = {"document": "document"}


def source_origin_for(provenance_class: str, channel: str) -> SourceOrigin | None:
    """RM-85 code map: a record's provenance class and source channel -> its RM-48 origin.

    Only pairs that name the kind of source record are mapped; everything else (derived
    extraction channels, internal writes, REST) stays unknown. A record origin field
    (BI-02) replaces this map once records carry one."""
    return (
        _CHANNEL_ORIGIN.get(channel)
        or _CLASS_CHANNEL_ORIGIN.get((provenance_class, channel))
        or _CLASS_ORIGIN.get(provenance_class)
    )


def _lifecycle(record: MemoryRecord) -> tuple[str | None, str | None]:
    """RM-57 status / outcome the record carries (``lifecycle:`` / ``outcome:`` tags).

    An unknown value, or an outcome that does not fit the status axis, leaves both unknown."""
    status, outcome = _tag(record, "lifecycle:"), _tag(record, "outcome:")
    if status is None or status not in LIFECYCLE_STATUSES:
        return None, None
    if outcome is not None and (outcome not in LIFECYCLE_OUTCOMES or status != "closed"):
        return None, None
    return status, outcome


def _vocabulary(predicate: str) -> tuple[str | None, str | None]:
    """The default-active pack that defines ``predicate``, and its version (else unknown)."""
    pack_id = load_builtin_registry().owner("predicate", predicate)
    if pack_id is None:
        return None, None
    versions = {p.pack_id: p.pack_version for p in load_builtin_packs()}
    version = versions.get(pack_id)
    return pack_id, str(version) if version is not None else None


# --------------------------------------------------------------------------- compiler


@dataclass(frozen=True)
class CompiledEdges:
    """The library delta plus the per-record facts the library does not carry."""

    namespace: str
    delta: GraphDelta
    #: record id -> (trust, sorted evidence ids, evidence role counts, event precision, source_at iso,
    #: and the RM-92 qualifier inputs: claim id, binding, origin, lifecycle, vocabulary, lineage)
    meta: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: Records that were not compiled, with the reason.
    skipped: Mapping[str, str] = field(default_factory=dict)


class EdgeCompiler:
    """Pure: records -> envelopes -> :class:`CompiledEdges`. No I/O, no clock, no model."""

    def __init__(
        self,
        *,
        scoped: bool = False,
        policy: EntityPolicy | None = None,
        registry: SemanticRegistry | None = None,
        labeller: SensitivityLabeller | None = None,
        topic_tagger: AnyTopicTagger | None = None,
    ) -> None:
        self.scoped = scoped
        self.policy = policy or EntityPolicy.from_policy(True)
        assert self.policy is not None
        #: RM-91: the vocabulary whose predicate / property / pack domains apply.
        self.registry = registry if registry is not None else load_builtin_registry()
        #: RM-99: the sensitivity labelling layer (deterministic unless a model is bound).
        self.labeller = labeller or SensitivityLabeller()
        #: RM-106: the opt-in topic tagger (already gated by ``models.bindings.gliner_topic``);
        #: run on the async path before :meth:`compile` (see ``_compile_namespace``).
        self.topic_tagger = topic_tagger

    def domain_of(self, record: MemoryRecord, topic: str | None = None) -> ResolvedDomain | None:
        """RM-91: ``record``'s domain and its source; ``topic`` is a tagger result (lowest)."""
        rel = _tag(record, "rel:") or to_envelope(record).assertion_key.rel
        return resolve_domain(
            tags=record.tags,
            predicate=rel,
            attribute_key=record.attribute,
            registry=self.registry,
            topic=topic,
        )

    @staticmethod
    def claim_of(record: MemoryRecord) -> ClaimText:
        """What the sensitivity labeller sees of ``record``: predicate, key and value texts."""
        dst = _tag(record, "dst:")
        return ClaimText(
            predicate=_tag(record, "rel:"),
            attribute_key=record.attribute or _tag(record, "attr:"),
            values=tuple(dict.fromkeys(v for v in (record.content, dst) if v and v.strip())),
            tags=tuple(record.tags),
        )

    def eligible(self, record: MemoryRecord) -> bool:
        """A semantic fact with a subject entity, live or superseded with a successor."""
        return is_fact(record, include_history=True, purpose=FactPurpose.EDGES)

    def envelope(self, record: MemoryRecord) -> AssertionEnvelope:
        """The enriched assertion envelope of ``record`` (the F-02a envelope plus code-resolved roles)."""
        env = to_envelope(record)
        ns = record.namespace
        scope = env.scope
        if self.scoped:  # M-05 / M-16: tenant + owner, never the session
            scope = Scope.from_namespace(ns).to_owner_scope()
        assert isinstance(scope, OwnerScope)
        rel = _tag(record, "rel:") or env.assertion_key.rel
        kind = _tag(record, "kind:") or env.assertion_key.kind
        dst_raw = _tag(record, "dst:")
        subj = canonical_entity(record.entity or "")
        subj_ok = bool(subj) and self.policy.accepts(subj)  # type: ignore[union-attr]
        subj_node = entity_node_id(ns, subj) if subj_ok else None
        obj_node: str | None = None
        obj_rejected = False
        if dst_raw:
            dst = canonical_entity(dst_raw)
            if dst and dst != subj:
                if self.policy.accepts(dst):  # type: ignore[union-attr]
                    obj_node = entity_node_id(ns, dst)
                else:
                    obj_rejected = True
        key = AssertionKey(
            src=env.assertion_key.src,
            rel=rel,
            dst=canonical_entity(dst_raw) if dst_raw else "",
            kind=kind,
        )
        cited = list(record.source.parents) or [record.record_id]
        conversation = record.source.message_id or record.record_id
        modality = env.assessment.modality
        tag_set = set(record.tags)
        if "retract" in tag_set:
            modality = "negated"
        elif tag_set & _TENTATIVE_TAGS:
            hit = next((m for m in _TENTATIVE_MODALITIES if m in tag_set), "hypothetical")
            modality = hit  # type: ignore[assignment]
        elif record.provenance_class == "assistant_proposed":
            modality = "reported"  # proposed by the assistant, never confirmed by the user
        binding = "bound"
        if subj_node is None or obj_rejected:
            binding = "partly" if subj_node is not None or obj_node is not None else "unbound"
        assessment = env.assessment.model_copy(update={"modality": modality, "binding": binding})
        subject = (
            Mention(
                surface=record.entity or subj,
                mention_id=f"m_{record.record_id}",
                entity_id=subj_node,
            )
            if record.entity
            else None
        )
        obj = (
            Mention(surface=dst_raw, mention_id=f"o_{record.record_id}", entity_id=obj_node)
            if dst_raw and dst_raw.strip() and canonical_entity(dst_raw) != subj
            else None
        )
        return env.model_copy(
            update={
                "scope": scope,
                "assertion_key": key,
                "occurrence_id": occurrence_id(conversation, cited, key),
                "subject": subject,
                "object": obj,
                "assessment": assessment,
                "kind": kind,
            }
        )

    def compile(
        self,
        records: Iterable[MemoryRecord],
        namespace: str,
        *,
        sensitivity: Mapping[str, SensitivityResult] | None = None,
        topics: Mapping[str, str] | None = None,
    ) -> CompiledEdges:
        """``sensitivity``: precomputed labels per record id (the async model path); a record
        without one is labelled here by the deterministic detectors. ``topics`` (RM-106):
        precomputed topic-tagger domains per record id (the ``gliner_topic`` source)."""
        envs: list[AssertionEnvelope] = []
        meta: dict[str, dict[str, Any]] = {}
        skipped: dict[str, str] = {}
        for record in sorted(records, key=lambda r: r.record_id):
            if not self.eligible(record):
                skipped[record.record_id] = "not_an_assertion"
                continue
            env = self.envelope(record)
            envs.append(env)
            roles = Counter(e.role for e in env.evidence)
            ids = {e.turn.source_id for e in env.evidence if e.turn is not None}
            ids.update(record.source.parents)
            domain = resolve_domain(
                tags=record.tags,
                predicate=env.assertion_key.rel,
                attribute_key=record.attribute,
                registry=self.registry,
                topic=(topics or {}).get(record.record_id),
            )
            meta[record.record_id] = {
                "trust": env.assessment.source_trust,
                "evidence_ids": sorted(ids),
                "evidence_roles": dict(sorted(roles.items())),
                "event_precision": env.time.event_precision,
                "event_basis": (
                    env.time.event.start.basis
                    if env.time.event is not None and env.time.event.start is not None
                    else None
                ),
                "source_at": env.time.source_time.isoformat() if env.time.source_time else None,
                # RM-92: inputs of the edge qualifiers (never record text).
                "namespace": record.namespace,
                "modality": env.assessment.modality,
                "known_at": record.recorded_at,
                "source_message_id": record.source.message_id,
                # RM-91: the domain and which of the four sources set it
                "topical_domain": domain.domain if domain else None,
                "topical_domain_source": domain.source if domain else None,
                # RM-99: labels, check state and labeller (never the matched text).
                "sensitivity": (sensitivity or {}).get(record.record_id)
                or self.labeller.label(self.claim_of(record)),
                "claim_id": record.record_id,
                "binding": env.assessment.binding,
                "origin": source_origin_for(record.provenance_class, record.source.channel),
                "lifecycle": _lifecycle(record),
                "vocabulary": _vocabulary(env.assertion_key.rel),
            }
        delta = compile_edges(envs)
        return CompiledEdges(namespace, delta, meta, skipped)


def attach_lineage(compiled: CompiledEdges, roots: Mapping[str, Sequence[str]]) -> CompiledEdges:
    """RM-89: ``compiled`` with each record's resolved evidence lineage roots (evidence id ->
    sorted root ids). An evidence id missing from ``roots`` is unresolved: the record's
    independent-support count then stays unknown."""
    meta: dict[str, Mapping[str, Any]] = {}
    for record_id, m in compiled.meta.items():
        known = {str(e): sorted(roots[e]) for e in m.get("evidence_ids", ()) if roots.get(e)}
        meta[record_id] = {**m, "evidence_roots": known} if known else m
    return replace(compiled, meta=meta)


# --------------------------------------------------------------------------- projection


@dataclass
class ProjectionStats:
    nodes: int = 0
    written: int = 0
    unchanged: int = 0
    tentative: int = 0
    closed: int = 0
    retracted_edges: int = 0
    retracted_nodes: int = 0
    unbound: int = 0
    #: RM-89: edges that lost erased evidence and turned contested.
    contested: int = 0

    def as_dict(self) -> dict[str, int]:
        return {k: v for k, v in self.__dict__.items() if v}


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _rel_type(edge: GraphEdge) -> str:
    if edge.kind == "role":
        return f"{ROLE_PREFIX}{edge.label}"
    return f"fact:{edge.label}" if edge.label in RESERVED_RELS else edge.label


def _edge_props(edge: GraphEdge, meta: Mapping[str, Any]) -> dict[str, object]:
    props: dict[str, object] = {
        "compiled": COMPILED,
        "edge_key": edge.key,
        "edge_kind": edge.kind,
        "assertion_id": edge.assertion_id,
        "record_id": edge.record_id,
        "occurrence_id": edge.occurrence_id,
        "tenant": edge.tenant,
        "owner": edge.owner,
        "tentative": edge.tentative,
        "negated": edge.negated,
        "contested": edge.contested or bool(meta.get("evidence_erased")),
        "closed": edge.closed,
        "valid_from": _iso(edge.valid_from),
        "valid_to": _iso(edge.valid_to),
        "status": edge.status,
        "trust": meta.get("trust"),
        "evidence_ids": list(meta.get("evidence_ids", ())),
        "source_at": meta.get("source_at"),
        "event_precision": meta.get("event_precision"),
        "event_basis": meta.get("event_basis"),
        QUALIFIERS_PROP: edge_qualifiers_for(edge, meta).to_properties(),
    }
    roots = meta.get("evidence_roots")
    if roots:
        props["evidence_roots"] = {str(k): list(v) for k, v in sorted(roots.items())}
    erased = meta.get("evidence_erased")
    if erased:
        props["evidence_erased"] = sorted(erased)
    return props


def _support(
    evidence_ids: Sequence[str], roots: Mapping[str, Sequence[str]] | None
) -> tuple[int | None, str | None]:
    """RM-89: (distinct lineage roots of the evidence, the single root if only one).

    Unknown (None) when there is no evidence or any evidence id has no resolved root."""
    if not evidence_ids or not roots or any(not roots.get(e) for e in evidence_ids):
        return None, None
    distinct = {r for e in evidence_ids for r in roots[e]}
    return len(distinct), (next(iter(distinct)) if len(distinct) == 1 else None)


def _ids(raw: object) -> list[str]:
    """A stored id list (anything else reads as empty)."""
    return [str(i) for i in raw] if isinstance(raw, list | tuple) else []


def _without_erased(meta: Mapping[str, Any], erased: Iterable[str]) -> dict[str, Any]:
    """RM-89: ``meta`` with the erased evidence ids removed and remembered."""
    gone = set(erased)
    remaining = [e for e in meta.get("evidence_ids", ()) if e not in gone]
    roots = {k: v for k, v in (meta.get("evidence_roots") or {}).items() if k in remaining}
    out = {**meta, "evidence_ids": remaining, "evidence_erased": sorted(gone)}
    if roots:
        out["evidence_roots"] = roots
    else:
        out.pop("evidence_roots", None)
    return out


#: Envelope modality -> qualifier modality. ``negated`` is an asserted negative (polarity).
_MODALITY = {
    "asserted": AssertionModality.ASSERTED,
    "negated": AssertionModality.ASSERTED,
    "hypothetical": AssertionModality.HYPOTHETICAL,
    "planned": AssertionModality.INTENDED,
    "reported": AssertionModality.REPORTED,
    "question": AssertionModality.QUESTIONED,
}


_PRECISIONS = frozenset(p.value for p in TimePrecision)


def _aware(value: Any) -> datetime | None:
    """An aware datetime, else unknown (a naive time is never guessed into a zone)."""
    return value if isinstance(value, datetime) and value.utcoffset() is not None else None


def edge_qualifiers_for(edge: GraphEdge, meta: Mapping[str, Any]) -> EdgeQualifiers:
    """RM-92: the typed qualifiers of one compiled edge, from the edge and its record only.

    Nothing is inferred: a value the compiler does not know stays unknown (and then fails
    any filter on it). The edge is live when projected, so its erasure state is active;
    erasure of the source record tombstones the edge (weight 0). Erasure of some of its
    evidence (``evidence_erased``) makes the claim contested (RM-89)."""
    valid_from, valid_to = _aware(edge.valid_from), _aware(edge.valid_to)
    if valid_from is not None and valid_to is not None and valid_to <= valid_from:
        valid_to = None  # incoherent bounds: keep the start, the end stays unknown
    precision = meta.get("event_precision")
    claim_state = (
        ClaimState.CONTESTED
        if edge.contested or meta.get("evidence_erased")
        else ClaimState.SUPERSEDED
        if edge.closed
        else ClaimState.CURRENT
        if edge.status == "active"
        else None
    )
    source_id = meta.get("source_message_id")
    labelled = meta.get("sensitivity")
    sens = labelled if isinstance(labelled, SensitivityResult) else NOT_CHECKED
    evidence = tuple(str(e) for e in meta.get("evidence_ids", ()))
    support, root = _support(evidence, meta.get("evidence_roots"))
    origin = meta.get("origin")
    provenance = (
        Provenance(
            source_id=str(source_id) if source_id else None,
            origin_type=origin,
            lineage_root=root,
        )
        if source_id or origin or root
        else None
    )
    trust = meta.get("trust")
    binding = meta.get("binding")
    status, outcome = meta.get("lifecycle") or (None, None)
    pack, version = meta.get("vocabulary") or (None, None)
    return EdgeQualifiers(
        sensitivity=sens.labels if sens.checked is not None else None,
        sensitivity_checked=sens.checked,
        sensitivity_labeller=sens.labeller_id,
        sensitivity_labeller_version=sens.labeller_version,
        sensitivity_confidence=dict(sens.confidence) if sens.confidence else None,
        valid_from=valid_from,
        valid_to=valid_to,
        valid_open_ended=(
            True if valid_from is not None and valid_to is None and not edge.closed else None
        ),
        valid_precision=(
            TimePrecision(precision)
            if isinstance(precision, str) and precision in _PRECISIONS
            else None
        ),
        known_at=_aware(meta.get("known_at")),
        assertion_modality=_MODALITY.get(str(meta.get("modality"))),
        polarity=Polarity.NEGATIVE if edge.negated else Polarity.POSITIVE,
        lifecycle_status=status,
        lifecycle_outcome=outcome,
        claim_state=claim_state,
        claim_id=meta.get("claim_id") or edge.record_id or None,
        evidence_ids=evidence or None,
        provenance=provenance,
        trust=(
            max(0.0, min(1.0, float(trust)))
            if isinstance(trust, int | float) and not isinstance(trust, bool)
            else None
        ),
        binding_status=binding if binding in _BINDINGS else None,
        scope=meta.get("namespace"),
        erasure_state=ErasureState.ACTIVE,
        event_role=edge.label if edge.kind == "role" else None,
        occurrence_id=edge.occurrence_id or None,
        topical_domain=meta.get("topical_domain"),
        topical_domain_source=meta.get("topical_domain_source"),
        vocabulary_pack=pack,
        vocabulary_version=version,
        independent_support_count=support,
    )


_BINDINGS = frozenset(get_args(BindingStatus))


def _weight(meta: Mapping[str, Any]) -> float:
    trust = meta.get("trust")
    return max(0.0, min(1.0, float(trust))) if isinstance(trust, int | float) else 1.0


def _node_payload(node: GraphNode, namespace: str, record_id: str | None) -> dict[str, object]:
    return {
        "labels": [node.kind],
        "properties": {
            "namespace": namespace,
            "compiled": COMPILED,
            "assertion_id": node.assertion_id,
            "record_id": record_id,
            "label": node.label if node.kind == "assertion" else None,
        },
    }


class LinkProjection:
    """Projects a compiled delta onto the graph as LINK events, and takes it back out."""

    def __init__(self, graph: GraphStore, append: AppendEvent) -> None:
        self._graph = graph
        self._append = append

    async def _current(self, namespace: str) -> list[StoredEdge]:
        return [
            e
            for e in await self._graph.edge_list(namespace)
            if e.properties.get("compiled") == COMPILED
        ]

    async def projected_record_ids(self, namespace: str) -> set[str]:
        """Records whose compiled edges are live in the graph (the lineage index)."""
        return {
            str(e.properties["record_id"])
            for e in await self._current(namespace)
            if e.weight > 0 and e.properties.get("record_id")
        }

    async def plan(
        self, compiled: CompiledEdges
    ) -> tuple[list[tuple[GraphEdge, dict[str, object]]], int]:
        """The LINK payloads :meth:`project` would append (edge, payload), and the unchanged count.

        A stored edge's ``evidence_erased`` ids stay erased (RM-89): they are removed from the
        recompiled evidence, the claim stays contested, and an edge left with no evidence stays a
        tombstone."""
        ns = compiled.namespace
        stored = {(e.src, e.dst, e.rel_type): e for e in await self._current(ns)}
        nodes = {n.id: n for n in compiled.delta.nodes}
        out: list[tuple[GraphEdge, dict[str, object]]] = []
        unchanged = 0
        for edge in compiled.delta.edges:
            meta: Mapping[str, Any] = compiled.meta.get(edge.record_id, {})
            rel = _rel_type(edge)
            have = stored.get((edge.src, edge.dst, rel))
            erased = _ids(have.properties.get("evidence_erased")) if have is not None else []
            if erased:
                meta = _without_erased(meta, erased)
            props = _edge_props(edge, meta)
            weight = _weight(meta) if not erased or meta.get("evidence_ids") else 0.0
            expected = {**props, "weight": weight}
            if have is not None and all(have.properties.get(k) == v for k, v in expected.items()):
                unchanged += 1
                continue
            payload: dict[str, object] = {
                "src": edge.src,
                "dst": edge.dst,
                "rel": rel,
                "weight": weight,
                "reason": COMPILED,
                "props": props,
            }
            for side, nid in (("src_node", edge.src), ("dst_node", edge.dst)):
                node = nodes.get(nid)
                if node is not None and node.kind != "entity":
                    payload[side] = _node_payload(node, ns, edge.record_id)
            out.append((edge, payload))
        return out, unchanged

    async def project(self, compiled: CompiledEdges) -> ProjectionStats:
        """Append a LINK event for every edge whose stored form differs; idempotent."""
        ns = compiled.namespace
        stats = ProjectionStats(unbound=len(compiled.delta.unbound))
        planned, stats.unchanged = await self.plan(compiled)
        for edge, payload in planned:
            await self._append(
                MemoryEvent(kind=EventKind.LINK, namespace=ns, actor="system", payload=payload)
            )
            stats.written += 1
            stats.tentative += int(edge.tentative)
            stats.closed += int(edge.closed)
        stats.nodes = len(compiled.delta.nodes)
        return stats

    async def _erase_evidence(
        self, namespace: str, wanted: set[str], skip: set[tuple[str, str, str]]
    ) -> ProjectionStats:
        """RM-89: live compiled edges citing an erased id as evidence lose it and turn contested;
        an edge left with no evidence is tombstoned (its properties keep the erased ids, so a
        re-projection never restores them)."""
        stats = ProjectionStats()
        for e in await self._current(namespace):
            if e.weight <= 0 or (e.src, e.dst, e.rel_type) in skip:
                continue
            evidence = _ids(e.properties.get("evidence_ids"))
            lost = wanted.intersection(evidence)
            if not lost:
                continue
            prior = set(_ids(e.properties.get("evidence_erased")))
            meta = _without_erased(
                {
                    "evidence_ids": evidence,
                    "evidence_roots": e.properties.get("evidence_roots") or {},
                },
                prior | lost,
            )
            remaining: list[str] = meta["evidence_ids"]
            props = {k: v for k, v in e.properties.items() if k != "weight"}
            props["evidence_ids"] = remaining
            props["evidence_erased"] = meta["evidence_erased"]
            props["contested"] = True
            props.pop("evidence_roots", None)
            if meta.get("evidence_roots"):
                props["evidence_roots"] = meta["evidence_roots"]
            q = edge_qualifiers(e.properties) or EdgeQualifiers()
            support, root = _support(remaining, meta.get("evidence_roots"))
            provenance = q.provenance
            if provenance is not None or root is not None:
                provenance = (provenance or Provenance()).model_copy(update={"lineage_root": root})
                if not provenance.model_dump(exclude_none=True):
                    provenance = None
            props[QUALIFIERS_PROP] = q.model_copy(
                update={
                    "claim_state": ClaimState.CONTESTED,
                    "evidence_ids": tuple(remaining) or None,
                    "independent_support_count": support,
                    "provenance": provenance,
                }
            ).to_properties()
            weight = e.weight if remaining else 0.0
            await self._append(
                MemoryEvent(
                    kind=EventKind.LINK,
                    namespace=namespace,
                    actor="system",
                    payload={
                        "src": e.src,
                        "dst": e.dst,
                        "rel": e.rel_type,
                        "weight": weight,
                        "reason": f"{COMPILED}:evidence_erased",
                        "props": props,
                    },
                )
            )
            stats.contested += 1
            stats.retracted_edges += int(not remaining)
        return stats

    async def retract_records(
        self, namespace: str, record_ids: Iterable[str], *, reason: str = "retract"
    ) -> ProjectionStats:
        """Remove every compiled edge sourced from ``record_ids`` (erasure / retire, by lineage).

        Edges are tombstoned with a LINK event (weight 0, properties replaced, so the log replays to the
        same graph) and the assertion node plus any value / time node nobody else uses is deleted.
        On erasure (``reason="erasure"``) the other edges citing an erased id as evidence lose it
        (RM-89, :meth:`_erase_evidence`)."""
        wanted = {str(r) for r in record_ids}
        stats = ProjectionStats()
        if not wanted:
            return stats
        hit = [
            e
            for e in await self._current(namespace)
            if e.weight > 0 and str(e.properties.get("record_id")) in wanted
        ]
        if reason == "erasure":
            ev = await self._erase_evidence(
                namespace, wanted, {(e.src, e.dst, e.rel_type) for e in hit}
            )
            stats.contested += ev.contested
            stats.retracted_edges += ev.retracted_edges
        assertions: set[str] = set()
        others: set[str] = set()
        for e in hit:
            if e.properties.get("edge_kind") == "role":
                assertions.add(e.src)
                if not is_entity_node(e.dst):
                    others.add(e.dst)
            await self._append(
                MemoryEvent(
                    kind=EventKind.LINK,
                    namespace=namespace,
                    actor="system",
                    payload={
                        "src": e.src,
                        "dst": e.dst,
                        "rel": e.rel_type,
                        "weight": 0.0,
                        "reason": f"{COMPILED}:{reason}",
                    },
                )
            )
            stats.retracted_edges += 1
        for node_id in sorted(assertions):
            await self._graph.delete_node(node_id)
            stats.retracted_nodes += 1
        for node_id in sorted(others):  # value / time nodes are shared: only drop the unused ones
            if not [x for x in await self._graph.edges_of(node_id) if x.weight > 0]:
                await self._graph.delete_node(node_id)
                stats.retracted_nodes += 1
        return stats

    async def visible_edges(
        self, namespace: str, *, include_tentative: bool = False, include_closed: bool = True
    ) -> list[StoredEdge]:
        """Default graph read of the compiled layer: tentative edges are excluded unless asked (S5-3)."""
        return [
            e
            for e in await self._current(namespace)
            if e.weight > 0
            and (include_tentative or not e.properties.get("tentative"))
            and (include_closed or not e.properties.get("closed"))
        ]


# --------------------------------------------------------------------------- stage


async def _lineage_roots(ctx: PipelineContext, ids: Iterable[str]) -> dict[str, list[str]]:
    """RM-89: evidence id -> its sorted lineage roots (records without parents), following
    ``source.parents`` through live records. An id that is not a live record, a chain deeper
    than :data:`EDGE_LINEAGE_MAX_DEPTH`, or a cycle leaves that id unresolved (absent)."""
    cache: dict[str, frozenset[str] | None] = {}

    async def roots_of(record_id: str, depth: int) -> frozenset[str] | None:
        if record_id in cache:
            return cache[record_id]
        cache[record_id] = None  # cycle guard: a record met again on its own chain is unresolved
        record = await live_record(ctx, record_id)
        found: frozenset[str] | None
        if record is None or depth > EDGE_LINEAGE_MAX_DEPTH:
            found = None
        elif not record.source.parents:
            found = frozenset({record_id})
        else:
            acc: set[str] = set()
            found = frozenset()
            for parent in dict.fromkeys(record.source.parents):
                sub = await roots_of(parent, depth + 1)
                if sub is None:
                    found = None
                    break
                acc |= sub
            if found is not None:
                found = frozenset(acc)
        cache[record_id] = found
        return found

    out: dict[str, list[str]] = {}
    for evidence_id in dict.fromkeys(ids):
        got = await roots_of(evidence_id, 0)
        if got:
            out[evidence_id] = sorted(got)
    return out


async def _compile_namespace(
    ctx: PipelineContext, namespace: str, compiler: EdgeCompiler
) -> tuple[list[MemoryRecord], CompiledEdges]:
    """The namespace's live semantic records and their compiled edges, lineage attached."""
    records = await live_records(ctx, namespace, "semantic")
    labels: dict[str, SensitivityResult] | None = None
    if compiler.labeller.model is not None:  # RM-99 opt-in model detector (async)
        labels = {
            r.record_id: await compiler.labeller.alabel(compiler.claim_of(r))
            for r in records
            if compiler.eligible(r)
        }
    topics: dict[str, str] | None = None
    if compiler.topic_tagger is not None:  # RM-106 opt-in topic tagger (async)
        topics = {}
        for r in records:
            # only where no higher-precedence source (or earlier topic tag) gives a domain
            if not compiler.eligible(r) or compiler.domain_of(r) is not None:
                continue
            found = await atopic_domains(r.content, compiler.topic_tagger)
            if found:
                topics[r.record_id] = found[0]
    compiled = compiler.compile(records, namespace, sensitivity=labels, topics=topics)
    evidence = (str(e) for m in compiled.meta.values() for e in m.get("evidence_ids", ()))
    return records, attach_lineage(compiled, await _lineage_roots(ctx, evidence))


async def _compile_one(
    ctx: PipelineContext, namespace: str, compiler: EdgeCompiler
) -> dict[str, int]:
    assert ctx.graph is not None and ctx.append_event is not None
    projection = LinkProjection(ctx.graph, ctx.append_event)
    records, compiled = await _compile_namespace(ctx, namespace, compiler)
    tap.event("graph.compile.start", namespace=namespace, records=len(records))
    delta = compiled.delta
    if tap.enabled():
        tap.event(
            "graph.compile.delta",
            namespace=namespace,
            assertions=sum(1 for n in delta.nodes if n.kind == "assertion"),
            edges=len(delta.edges),
            tentative=sum(1 for e in delta.edges if e.tentative),
            closed=sum(1 for e in delta.edges if e.closed),
            unbound=len(delta.unbound),
            skipped=len(delta.skipped),
            scoped=compiler.scoped,
        )
    stats = await projection.project(compiled)
    tap.event(
        "graph.compile.project",
        namespace=namespace,
        written=stats.written,
        unchanged=stats.unchanged,
        tentative=stats.tentative,
    )
    # Retire: projected records that are no longer assertions (erased, hidden, archived with no successor).
    live = {r.record_id for r in records if compiler.eligible(r)}
    gone = sorted(await projection.projected_record_ids(namespace) - live)
    if gone:
        gone_stats = await projection.retract_records(namespace, gone, reason="retire")
        stats.retracted_edges += gone_stats.retracted_edges
        stats.retracted_nodes += gone_stats.retracted_nodes
        tap.event(
            "graph.compile.retract",
            namespace=namespace,
            records=len(gone),
            edges=gone_stats.retracted_edges,
            nodes=gone_stats.retracted_nodes,
            reason="retire",
        )
    return stats.as_dict()


async def compile_graph_edges(ctx: PipelineContext) -> dict[str, object]:
    """F-02c: compile the namespace's assertions into graph edges (``graph.edge_compiler_v2``)."""
    if not ctx.config.graph.edge_compiler_v2:
        return {"status": "skipped", "reason": "graph.edge_compiler_v2 off"}
    if ctx.graph is None:
        return {"status": "skipped", "reason": "no graph store"}
    if ctx.append_event is None:
        return {"status": "skipped", "reason": "read-only context (no write door)"}
    compiler = EdgeCompiler(
        scoped=bool(ctx.config.isolation.scope_keys_v2),
        labeller=SensitivityLabeller(model=getattr(ctx, "sensitivity_model", None)),
        topic_tagger=getattr(ctx, "topic_tagger", None),
    )
    totals: Counter[str] = Counter()
    errors: list[str] = []
    for namespace in await sweep_namespaces(ctx):
        try:
            async with ctx.lock(namespace):
                totals.update(await _compile_one(ctx, namespace, compiler))
        except Exception as exc:  # one bad namespace must not kill the sleep cycle
            errors.append(f"{namespace}: {exc}")
            _log.warning("compile_edges.namespace_failed", namespace=namespace, error=str(exc))
    stats: dict[str, object] = {"status": "ok", **dict(totals)}
    if errors:
        stats.update(status="partial", errors=errors)
    return stats


async def requalify_namespace(
    ctx: PipelineContext, namespace: str, *, dry_run: bool = False
) -> dict[str, object]:
    """RM-100: re-project one namespace's compiled edges now, so every edge carries current
    qualifiers. Only edges whose stored form differs are rewritten (idempotent); ``dry_run``
    counts without writing. Records that are no longer assertions are left to the sleep cycle.

    Report: ``edges`` (live compiled edges before), ``missing_qualifiers`` (of those, without
    valid qualifiers), ``to_update`` (edges whose stored form differs), ``unchanged`` and, when
    not a dry run, ``updated``."""
    if not ctx.config.graph.edge_compiler_v2:
        raise ValueError("graph.edge_compiler_v2 is off: there is no compiled edge layer")
    if ctx.graph is None:
        raise ValueError("no graph store configured")
    if not dry_run and ctx.append_event is None:
        raise ValueError("read-only context (no write door)")

    async def _refuse(_event: MemoryEvent) -> None:
        raise RuntimeError("dry run: no event is appended")

    # The same labelling inputs as the sleep-cycle compile, so a requalify agrees with it.
    compiler = EdgeCompiler(
        scoped=bool(ctx.config.isolation.scope_keys_v2),
        labeller=SensitivityLabeller(model=getattr(ctx, "sensitivity_model", None)),
        topic_tagger=getattr(ctx, "topic_tagger", None),
    )
    projection = LinkProjection(ctx.graph, ctx.append_event or _refuse)
    async with ctx.lock(namespace):
        live = [e for e in await projection._current(namespace) if e.weight > 0]
        _, compiled = await _compile_namespace(ctx, namespace, compiler)
        planned, unchanged = await projection.plan(compiled)
        report: dict[str, object] = {
            "namespace": namespace,
            "dry_run": dry_run,
            "edges": len(live),
            "missing_qualifiers": sum(1 for e in live if edge_qualifiers(e.properties) is None),
            "to_update": len(planned),
            "unchanged": unchanged,
        }
        if not dry_run:
            stats = await projection.project(compiled)
            report["updated"] = stats.written
    tap.event("graph.requalify", **report)
    return report


async def retract_after_forget(eng: Any, namespace: str, record_ids: Sequence[str]) -> None:
    """Erasure hook (flag on): the forgotten records' compiled edges go with them (by lineage)."""
    graph = getattr(eng, "_graph", None)
    if graph is None or not eng._config().graph.edge_compiler_v2:
        return
    stats = await LinkProjection(graph, eng._append_and_project).retract_records(
        namespace, record_ids, reason="erasure"
    )
    tap.event(
        "graph.compile.retract",
        namespace=namespace,
        records=len(record_ids),
        edges=stats.retracted_edges,
        nodes=stats.retracted_nodes,
        contested=stats.contested,
        reason="erasure",
    )
