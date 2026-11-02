"""Shared harness of the F1..F12 scenario fixtures (RM-22, RM-54; tests/scenarios/README.md).

Every scenario boots the default rules-only profile (``template="base"``: the online rules
extractor over the durable outbox, no LLM role bound) with the deterministic ``hash``
embedder, an on-disk SQLite log and the associative graph with the edge compiler on, so a
scenario sees the claims, the compiled edges and their qualifiers. Model-free throughout:
the LLM router is replaced by an empty one, so any generative call would fail loudly.

The helpers read through the public Engine API (``ingest_messages``, ``flush_windows``,
``sleep``, ``search``, ``read``, ``forget``, ``rebuild``) and inspect the derived surfaces
(canonical claims, graph edges, the entity name index, the source annotations) only to
assert on them. ``tests`` is not a package, so scenario modules import this file by name.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.derive.non_generative_facts import (
    FactStageResult,
    GroundedFactCandidate,
    NonGenerativeFactExtractor,
    UnresolvedAnnotation,
    assertion_owner,
)
from memspine.ingest import windows as ingest_windows
from memspine.schemas.durable import AnnotationBatch, SemanticEnvelope
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.evidence import Evidence, Span, TurnRef
from memspine.schemas.durable.semantic import ClaimQualifier, RoleBinding
from memspine.services.graph.qualifiers import edge_qualifiers
from memspine.services.llm.base import LLMRouter
from memspine.store.records import MemoryRecord
from memspine.trust.pipeline.erasure_state import visible_id

NS = "a"


# ------------------------------------------------------------------ boot


async def boot(
    monkeypatch: pytest.MonkeyPatch,
    path: Path,
    *,
    extractor: NonGenerativeFactExtractor | None = None,
    max_turns: int = 1,
    resource: bool = False,
    read: Mapping[str, Any] | None = None,
) -> Engine:
    """The default rules-only profile on ``path``; ``extractor`` replaces the built-in rules
    behind the configured non-generative extractor hook (the family fixtures, DS-18)."""

    async def router(
        config: MemspineConfig, hooks: object = None, wiring: object = None
    ) -> LLMRouter:
        return LLMRouter({})  # no generative role: a model call cannot happen

    monkeypatch.setattr("memspine.runtime.container.build_llm_router", router)
    eng = Engine(
        template="base",
        dotenv_path=None,
        storage={
            "records": {"provider": "sqlite", "settings": {"path": str(path), "busy_timeout_s": 5}}
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={
            "associative": {"enabled": True, "policies": {"entity_nodes": True}},
            **({"resource": {"enabled": True}} if resource else {}),
        },
        graph={"edge_compiler": True},
        write={"window_async": False, "window_idle_s": 0.0, "window_max_turns": max_turns},
        **({"read": dict(read)} if read is not None else {}),
    )
    await eng.start()
    assert eng.config.write.online_extractor and eng.config.workers.outbox
    if extractor is not None:
        assert eng._windows is not None
        eng._windows._non_generative_extractor = extractor
    return eng


def msg(
    message_id: str,
    sender: str,
    content: str,
    created_at: str,
    *,
    role: str = "user",
    kind: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    sender_record: dict[str, str] = {"id": sender}
    if kind is not None:
        sender_record["kind"] = kind
    return {
        "message_id": message_id,
        "role": role,
        "sender": sender_record,
        "content": content,
        "created_at": created_at,
        **extra,
    }


async def send(
    eng: Engine, messages: Sequence[Mapping[str, Any]], *, ns: str = NS, session: str = "s1"
) -> list[MemoryRecord]:
    """Ingest ``messages`` and run their derivation (rules windows) to completion."""
    records = await eng.ingest_messages(
        list(messages), namespace=ns, session_id=session, conversation_id=session
    )
    stats = await eng.flush_windows(ns)
    assert stats["calls"] == 0  # rules only: zero generative calls
    return list(records)


async def settle(eng: Engine) -> None:
    """One sleep cycle: the edge compiler projects the settled claims."""
    stats = await eng.sleep()
    assert stats["compile_edges"]["status"] in ("ok", "partial")


async def tick() -> datetime:
    """A known-time cut between two ingests (the clock has visibly moved on both sides)."""
    await asyncio.sleep(0.02)
    cut = datetime.now(UTC)
    await asyncio.sleep(0.02)
    return cut


# ------------------------------------------------------------------ claims


@dataclass(frozen=True)
class Claim:
    """The comparable view of one visible canonical claim (ADR-088)."""

    record_id: str
    text: str
    subject: str
    predicate: str
    object: str | None
    modality: str
    status: str
    outcome: str
    principal: str | None
    producer: str
    roles: tuple[tuple[str, str, str, str | None, tuple[str, ...]], ...]
    evidence: tuple[tuple[str, int, int, str], ...]
    sources: tuple[str, ...]
    time_precision: str
    event_resolved: bool
    known_at: datetime | None
    valid_from: datetime | None

    def role(self, name: str) -> list[tuple[str, str, str | None, tuple[str, ...]]]:
        return [(v, k, t, q) for r, v, k, t, q in self.roles if r == name]


def envelope_of(record: MemoryRecord) -> SemanticEnvelope:
    return SemanticEnvelope.model_validate(record.fingerprint.envelope)


def _claim(record: MemoryRecord) -> Claim:
    env = envelope_of(record)
    assert env.registry is not None and env.producer is not None
    evidence = tuple(
        (e.turn.source_id, e.span.start, e.span.end, e.span.quote)
        for e in env.evidence
        if e.turn is not None and e.span is not None
    )
    roles = tuple(
        sorted(
            (
                b.role_name,
                b.value,
                b.value_kind,
                b.entity_type,
                tuple(e.span.quote for e in b.evidence if e.span is not None),
            )
            for b in env.role_bindings
        )
    )
    return Claim(
        record_id=record.record_id,
        text=record.content,
        subject=env.subject.surface if env.subject is not None else "",
        predicate=env.registry.predicate,
        object=env.object.surface if env.object is not None else None,
        modality=env.assessment.modality,
        status=env.status,
        outcome=env.outcome,
        principal=env.provenance.principal,
        producer=env.producer.kind,
        roles=roles,
        evidence=evidence,
        sources=tuple(record.source.parents),
        time_precision=env.assessment.time_precision,
        event_resolved=env.time.event is not None,
        known_at=env.known_at,
        valid_from=record.valid_from,
    )


async def claims(eng: Engine, ns: str = NS) -> list[Claim]:
    """Every visible (not erased, not hidden, not deleted) rules-produced claim of ``ns``."""
    storage = eng._require_started()
    out = [
        _claim(r)
        for r in await storage.list_records(ns, "semantic")
        if "non_generative" in r.tags
        and r.status.value != "deleted"
        and visible_id(eng, r.record_id)
    ]
    return sorted(out, key=lambda c: (c.valid_from or datetime.min.replace(tzinfo=UTC), c.text))


def by_predicate(found: Sequence[Claim], predicate: str) -> list[Claim]:
    return [c for c in found if c.predicate == predicate]


# ------------------------------------------------------------------ graph


@dataclass(frozen=True)
class Edge:
    edge_key: str
    rel: str
    src: str
    dst: str
    record_id: str
    claim_state: str | None
    modality: str | None
    polarity: str | None
    tentative: bool


async def edges(eng: Engine, ns: str = NS) -> list[Edge]:
    """The live compiled edges (weight above zero: a tombstone is not an edge)."""
    assert eng._graph is not None
    out = []
    for e in await eng._graph.edge_list(ns):
        props = e.properties
        if props.get("compiled") != "edge_compiler" or float(props.get("weight", 0.0)) <= 0:
            continue
        q = edge_qualifiers(props)
        out.append(
            Edge(
                edge_key=str(props.get("edge_key")),
                rel=e.rel_type,
                src=e.src,
                dst=e.dst,
                record_id=str(props.get("record_id")),
                claim_state=q.claim_state.value if q is not None and q.claim_state else None,
                modality=(
                    q.assertion_modality.value
                    if q is not None and q.assertion_modality is not None
                    else None
                ),
                polarity=q.polarity.value if q is not None and q.polarity is not None else None,
                tentative=bool(props.get("tentative")),
            )
        )
    return sorted(out, key=lambda x: x.edge_key)


def edges_of(found: Sequence[Edge], record_id: str) -> list[Edge]:
    return [e for e in found if e.record_id == record_id]


async def entity_ids(eng: Engine, name: str, ns: str = NS) -> list[str]:
    """RM-130: the opaque entity ids the name index of ``ns`` holds for ``name``."""
    assert eng._associative is not None
    return sorted(await (await eng._associative.entity_lookup(ns)).get(name.lower()))


# ------------------------------------------------------------------ annotations


def unresolved(eng: Engine, ns: str = NS) -> list[UnresolvedAnnotation]:
    return [
        UnresolvedAnnotation.model_validate(item)
        for item in ingest_windows.unresolved_annotations(eng, ns)
    ]


def annotation_sources(eng: Engine, ns: str = NS) -> set[str]:
    """The source record ids that still hold a mention annotation batch (BI-08)."""
    assert eng._windows is not None
    out: set[str] = set()
    for row in eng._windows.derivation_effect_status(ns):
        if row.get("layer") == "annotations" and row.get("state") == "complete":
            for raw in (row.get("artifact") or {}).get("annotation_batches", []):
                out.add(AnnotationBatch.model_validate(raw).source.record_id)
    return out


# ------------------------------------------------------------------ reads


async def semantic_hits(eng: Engine, query: str, ns: str = NS, **kw: Any) -> list[str]:
    """The claim texts a public search returns (semantic records only)."""
    hits = await eng.search(query, namespace=ns, memory_types=["semantic"], top_k=20, **kw)
    return [r.content for r, _ in hits if "non_generative" in r.tags]


async def read_claims(eng: Engine, query: str, ns: str = NS, **kw: Any) -> list[str]:
    """The claim texts a public read assembles (semantic records only)."""
    result = await eng.read(query, namespace=ns, memory_types=["semantic"], **kw)
    return [r.content for r in result.context.records if "non_generative" in r.tags]


# ------------------------------------------------------------------ replay / erasure


async def snapshot(eng: Engine, names: Sequence[str], ns: str = NS) -> dict[str, Any]:
    return {
        "claims": await claims(eng, ns),
        "entities": {name: await entity_ids(eng, name, ns) for name in names},
        "edges": await edges(eng, ns),
        "unresolved": sorted(
            (u.source.record_id, u.reason, u.attribute, u.value) for u in unresolved(eng, ns)
        ),
    }


async def assert_replay(eng: Engine, names: Sequence[str], ns: str = NS) -> dict[str, Any]:
    """Replay check: a rebuild of every projector from the log gives the identical claims,
    entity ids, compiled edges and unresolved annotations (ADR-089 amendment: replay never
    mints an entity id)."""
    before = await snapshot(eng, names, ns)
    counts = await eng.rebuild()
    assert counts  # every projector replayed
    after = await snapshot(eng, names, ns)
    assert after == before
    return after


async def assert_erased(
    eng: Engine,
    source: MemoryRecord,
    claim_ids: Sequence[str],
    query: str,
    ns: str = NS,
) -> None:
    """Erasure check: hard-forget the evidence ``source``; its claims, their compiled edges,
    the source's annotations and its search hits go, the erasure verifies clean, and a
    rebuild does not bring any of them back."""
    await eng.forget(source.record_id, namespace=ns, hard=True)
    ids = set(claim_ids)
    for _ in range(2):  # live, then after a full replay
        assert not ids & {c.record_id for c in await claims(eng, ns)}
        assert not [e for e in await edges(eng, ns) if e.record_id in ids]
        assert source.record_id not in {u.source.record_id for u in unresolved(eng, ns)}
        assert source.record_id not in annotation_sources(eng, ns)
        hits = await eng.search(query, namespace=ns, top_k=20)
        assert not ids & {r.record_id for r, _ in hits}
        assert source.record_id not in {r.record_id for r, _ in hits}
        await eng.rebuild()
    report = await eng.verify_forget(source.record_id, namespace=ns)
    assert report["clean"] is True and report["record_absent"] is True
    assert not report["descendants_leaked"]


# ------------------------------------------------------------------ synthetic candidates


def support(record: MemoryRecord, quote: str, *, role: str = "support") -> Evidence:
    """Evidence pinned to the unique exact span ``quote`` of ``record`` (fails if absent)."""
    start = record.content.index(quote)
    assert record.content.find(quote, start + 1) < 0, "fixture quote must be unique"
    owner = assertion_owner(record) or "unknown"
    return Evidence(
        role=role,  # type: ignore[arg-type]
        turn=TurnRef(source_id=record.record_id, source_version=record.version, seg_index=0),
        span=Span(start=start, end=start + len(quote), quote=quote),
        source_family=owner,
    )


def envelope_evidence(record: MemoryRecord) -> Evidence:
    """Evidence citing the whole source envelope (no exact span)."""
    return Evidence(
        role="support",
        turn=TurnRef(source_id=record.record_id, source_version=record.version, seg_index=0),
        source_family=assertion_owner(record) or "unknown",
    )


def binding(
    n: int,
    role: str,
    value: str,
    kind: str,
    evidence: Evidence,
    entity_type: str | None = None,
) -> RoleBinding:
    return RoleBinding(
        binding_id=f"rb-{n}",
        role_name=role,
        role_version=1,
        value_kind=kind,  # type: ignore[arg-type]
        value=value,
        entity_type=entity_type,  # type: ignore[arg-type]
        evidence=[evidence],
    )


class ScriptedExtractor:
    """A deterministic non-generative extractor: per source content, the candidates a
    fixture states (built from the stored record, so evidence pins its id and version)."""

    identity = "scenario-fixture:1"

    def __init__(self) -> None:
        self.script: dict[str, Any] = {}

    def on(self, content: str, build: Any) -> None:
        """``build(record, scope) -> list[GroundedFactCandidate]`` for the source ``content``."""
        self.script[content] = build

    async def extract(self, record: MemoryRecord, scope: OwnerScope) -> FactStageResult:
        build = self.script.get(record.content)
        if build is None:
            return FactStageResult()
        facts: list[GroundedFactCandidate] = list(build(record, scope))
        return FactStageResult(facts=tuple(facts))


def candidate(
    record: MemoryRecord,
    scope: OwnerScope,
    *,
    predicate: str,
    obj: str,
    evidence: Evidence,
    subject: str | None = None,
    subject_type: str = "person",
    object_kind: str = "entity",
    object_entity_type: str | None = None,
    roles: Sequence[RoleBinding] = (),
    owner: str | None = None,
    modality: str = "asserted",
    cardinality_scope: Mapping[str, str] | None = None,
    qualifiers: Mapping[str, str] | None = None,
) -> GroundedFactCandidate:
    sender = assertion_owner(record) or "unknown"
    return GroundedFactCandidate(
        scope=scope,
        subject=subject or sender,
        subject_type=subject_type,  # type: ignore[arg-type]
        predicate=predicate,
        object=obj,
        object_kind=object_kind,  # type: ignore[arg-type]
        object_entity_type=object_entity_type,  # type: ignore[arg-type]
        modality=modality,  # type: ignore[arg-type]
        assertion_owner=owner or sender,
        evidence=evidence,
        role_bindings=tuple(roles),
        cardinality_scope=dict(cardinality_scope or {}),
        qualifiers={
            key: ClaimQualifier(kind="text", text=value)
            for key, value in (qualifiers or {}).items()
        },
        producer_revision="scenario-fixture:1",
    )
