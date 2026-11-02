"""RM-62/RM-63 (ADR-094): the activation gate for one vocabulary family.

A proposed family moves into the active pack (``memspine.core``) only when its five
executable fixtures pass:

* **positive** -- representative claims of every family definition validate in the
  registry; a grounded candidate passes the canonical fact door
  (``compile_grounded_facts``); its fact compiles to graph edges whose topical domain is
  under the family's generic domain, so ``graph_filter`` by that domain admits them, and
  the edges name the owning vocabulary pack; plus the family's own contract check;
* **negative** -- ill-typed claims are rejected by the registry, an ill-typed candidate
  stays unresolved at the door (``semantic_validation_failed``), and a filter on every
  other domain (or excluding the family domain) admits none of the family's edges;
* **known-time** -- an as-of read before the fact was recorded admits none of its edges,
  one after admits them all, and a valid-time point before the fact's start fails;
* **replay** -- recompiling is a no-op, and a projector rebuild from the event log followed
  by a recompile restores identical edges and qualifiers;
* **erasure** -- a hard forget of the fact leaves no live edge, no filter admits one, and a
  later compile does not bring them back.

Each ``tests/unit/semantics/families/test_family_<x>.py`` subclasses
:class:`FamilyContract`. ``ACTIVE`` states where the family lives: True = in the default
active pack (the fixtures run against the default registry); False = still in
``memspine.proposed`` (the fixtures run with that pack selected, and the failing case is
recorded as ``xfail(strict=True)``). ``test_activation_state`` pins that placement.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar

import pytest

from memspine import Engine
from memspine.derive.non_generative_facts import (
    CompiledFactStage,
    FactStageResult,
    GroundedFactCandidate,
    compile_grounded_facts,
)
from memspine.derive.pipeline.stages import edge_compiler as edge_compiler_module
from memspine.derive.pipeline.stages.edge_compiler import (
    COMPILED,
    EdgeCompiler,
    compile_graph_edges,
    edge_qualifiers_for,
)
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.evidence import Evidence, Span, TurnRef
from memspine.schemas.durable.semantic import ClaimQualifier, RoleBinding
from memspine.semantics import (
    FAMILY_DOMAINS,
    QualifierValue,
    RoleBindingCheck,
    SemanticRegistry,
    load_builtin_registry,
    load_registry,
)
from memspine.semantics.domains import domain_covers
from memspine.services.graph.qualifiers import EdgeQualifiers, QualifierFilter, edge_qualifiers
from memspine.store.records import MemoryRecord, SourceInfo

NS = "a"
T0 = datetime(2023, 5, 7, 10, 0, tzinfo=UTC)
_DOOR_SCOPE = OwnerScope(tenant="tenant", owner="tenant/alice")


def full_registry() -> SemanticRegistry:
    return load_registry(["memspine.core", "memspine.proposed"])


# --------------------------------------------------------------------------- claims


@dataclass(frozen=True)
class Claim:
    """One claim shape checked against the registry (types, roles, qualifiers, origin)."""

    predicate: str
    subject_type: str = "person"
    value_kind: str = "entity"
    object_entity_type: str | None = None
    attribute_key: str | None = None
    roles: tuple[RoleBindingCheck, ...] = ()
    qualifiers: Mapping[str, QualifierValue] = field(default_factory=dict)
    source_origin: str | None = "raw_message"

    def validate(self, registry: SemanticRegistry) -> None:
        registry.validate_compatibility(
            self.predicate,
            subject_type=self.subject_type,
            value_kind=self.value_kind,
            object_entity_type=self.object_entity_type,
            attribute_key=self.attribute_key,
            role_names=[r.role for r in self.roles],
            qualifiers=self.qualifiers,
            source_origin=self.source_origin,
        )
        if self.roles:
            registry.validate_role_bindings(self.predicate, self.roles)


def role(
    name: str,
    kind: str = "entity",
    entity_type: str | None = None,
    *,
    evidence: str = "exact_span",
    unit: str | None = None,
    amount: str | None = None,
) -> RoleBindingCheck:
    return RoleBindingCheck(
        role=name,
        value_kind=kind,  # type: ignore[arg-type]
        entity_type=entity_type,
        evidence=evidence,  # type: ignore[arg-type]
        unit=unit,
        amount=Decimal(amount) if amount is not None else None,
    )


def text(value: str) -> QualifierValue:
    return QualifierValue(kind="text", text=value)


def number(value: str) -> QualifierValue:
    return QualifierValue(kind="number", amount=Decimal(value))


# --------------------------------------------------------------------------- the door


@dataclass(frozen=True)
class Door:
    """A source-grounded candidate for the canonical fact door. Every quoted value must
    occur exactly once in ``text``."""

    text: str
    predicate: str
    obj: str
    object_kind: str = "entity"
    object_entity_type: str | None = None
    subject_type: str = "person"
    #: (role name, value kind, quoted value, entity type)
    roles: tuple[tuple[str, str, str, str | None], ...] = ()
    #: (qualifier key, text value): registry-checked claim qualifiers (RM-157)
    qualifiers: tuple[tuple[str, str], ...] = ()

    def _record(self) -> MemoryRecord:
        return MemoryRecord(
            record_id="family-source-1",
            namespace="tenant/alice",
            memory_type="episodic",
            content=self.text,
            source=SourceInfo(
                role="user",
                channel="messages",
                message={"message_id": "family-message-1", "sender": {"id": "alice"}},
            ),
        )

    @staticmethod
    def _evidence(record: MemoryRecord, quote: str) -> Evidence:
        start = record.content.index(quote)
        return Evidence(
            role="support",
            turn=TurnRef(source_id=record.record_id, source_version=record.version, seg_index=0),
            span=Span(start=start, end=start + len(quote), quote=quote),
            source_family="alice",
        )

    def compile(self, registry: SemanticRegistry) -> CompiledFactStage:
        record = self._record()
        bindings = tuple(
            RoleBinding(
                binding_id=f"{name}-{i}",
                role_name=name,
                role_version=1,
                value_kind=kind,  # type: ignore[arg-type]
                value=value,
                entity_type=etype,  # type: ignore[arg-type]
                evidence=[self._evidence(record, value)],
            )
            for i, (name, kind, value, etype) in enumerate(self.roles)
        )
        candidate = GroundedFactCandidate(
            scope=_DOOR_SCOPE,
            subject="alice",
            subject_type=self.subject_type,  # type: ignore[arg-type]
            predicate=self.predicate,
            object=self.obj,
            object_kind=self.object_kind,  # type: ignore[arg-type]
            object_entity_type=self.object_entity_type,  # type: ignore[arg-type]
            modality="asserted",
            assertion_owner="alice",
            evidence=self._evidence(record, self.text),
            role_bindings=bindings,
            qualifiers={k: ClaimQualifier(kind="text", text=v) for k, v in self.qualifiers},
            producer_revision="family-fixture:1",
        )
        return compile_grounded_facts(
            FactStageResult(facts=(candidate,)), [record], _DOOR_SCOPE, registry=registry
        )


# --------------------------------------------------------------------------- the fact


@dataclass(frozen=True)
class Fact:
    """A semantic fact record of the family, as the extraction path writes it."""

    subject: str
    predicate: str
    obj: str
    tags: tuple[str, ...] = ()
    channel: str = "extract_graph"
    kind: str = "state"

    def all_tags(self) -> list[str]:
        return [f"kind:{self.kind}", f"rel:{self.predicate}", f"dst:{self.obj}", *self.tags]

    def record(self) -> MemoryRecord:
        return MemoryRecord(
            namespace=NS,
            memory_type="semantic",
            content=f"{self.subject} {self.predicate} {self.obj}",
            entity=self.subject,
            tags=self.all_tags(),
            source=SourceInfo(role="derived", channel=self.channel),
            valid_from=T0,
        )

    def entity_ids(self, record: MemoryRecord) -> dict[str, dict[str, str]]:
        names = (self.subject.lower(), self.obj.lower())
        return {record.record_id: {n: f"ent:{NS}:{n}" for n in names}}


def _engine() -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={
            "semantic": {"enabled": True},
            "episodic": {"enabled": True},
            "associative": {"enabled": True, "policies": {"entity_nodes": True}},
        },
        graph={"edge_compiler": True},
        read={"record_access": False},
    )


async def write_fact(eng: Engine, fact: Fact) -> str:
    turn = await eng.write(
        f"{fact.subject} {fact.predicate} {fact.obj}.", namespace=NS, memory_type="episodic"
    )
    rec = await eng.write(
        f"{fact.subject} {fact.predicate} {fact.obj}",
        namespace=NS,
        memory_type="semantic",
        entity=fact.subject,
        tags=fact.all_tags(),
        source=SourceInfo(role="derived", channel=fact.channel, parents=[turn.record_id]),
        valid_from=T0,
    )
    return rec.record_id


async def compiled_edges(eng: Engine, record_id: str | None = None) -> list[Any]:
    assert eng._graph is not None
    return [
        e
        for e in await eng._graph.edge_list(NS)
        if e.properties.get("compiled") == COMPILED
        and (record_id is None or e.properties.get("record_id") == record_id)
    ]


def _snapshot(edges: Iterable[Any]) -> list[tuple[Any, ...]]:
    rows = []
    for e in edges:
        q = edge_qualifiers(e.properties) or EdgeQualifiers()
        rows.append(
            (
                e.src,
                e.dst,
                e.rel_type,
                e.weight > 0,
                q.topical_domain,
                q.topical_domain_source,
                q.vocabulary_pack,
                tuple(sorted(q.sensitivity or ())),
                q.provenance.origin_type if q.provenance else None,
                q.lifecycle_status,
                q.lifecycle_outcome,
                q.claim_state,
            )
        )
    return sorted(rows, key=repr)


# --------------------------------------------------------------------------- contract


class FamilyContract:
    """Subclass per family; set the class attributes below."""

    FAMILY: ClassVar[str]
    DOMAIN: ClassVar[str]
    ACTIVE: ClassVar[bool]
    POSITIVE: ClassVar[Sequence[Claim]]
    NEGATIVE: ClassVar[Sequence[tuple[Claim, str]]]
    DOOR: ClassVar[Door]
    DOOR_NEGATIVE: ClassVar[Door]
    FACT: ClassVar[Fact]

    # ---- wiring

    @pytest.fixture
    def registry(self, monkeypatch: pytest.MonkeyPatch) -> SemanticRegistry:
        """The registry the family is checked against: the default active registry once
        activated; before that, the default plus the explicitly selected proposed pack (the
        edge compiler is pointed at the same selection)."""
        if self.ACTIVE:
            return load_builtin_registry()
        selected = full_registry()
        monkeypatch.setattr(edge_compiler_module, "load_builtin_registry", lambda: selected)
        return selected

    @pytest.fixture
    async def engine(self, registry: SemanticRegistry) -> AsyncIterator[Engine]:
        eng = _engine()
        await eng.start()
        try:
            yield eng
        finally:
            await eng.stop()

    def pure_qualifiers(self, registry: SemanticRegistry) -> list[EdgeQualifiers]:
        record = self.FACT.record()
        compiled = EdgeCompiler(registry=registry).compile(
            [record], NS, entity_ids=self.FACT.entity_ids(record)
        )
        meta = compiled.meta[record.record_id]
        return [edge_qualifiers_for(e, meta) for e in compiled.delta.edges]

    def extra_positive(self, registry: SemanticRegistry, qualifiers: list[EdgeQualifiers]) -> None:
        """The family's own contract (override)."""

    # ---- the gate

    def test_activation_state(self) -> None:
        default = load_builtin_registry()
        full = full_registry()
        members = [key for key, (fam, _) in full.families.items() if fam == self.FAMILY]
        assert members, f"family {self.FAMILY} has no members"
        assert self.DOMAIN in FAMILY_DOMAINS
        for kind, name in members:
            assert full.family_domain(kind, name) == self.DOMAIN
            expected = "memspine.core" if self.ACTIVE else "memspine.proposed"
            assert full.owner(kind, name) == expected, (kind, name)
            assert (default.family_of(kind, name) == self.FAMILY) is self.ACTIVE

    def test_positive(self, registry: SemanticRegistry) -> None:
        for claim in self.POSITIVE:
            claim.validate(registry)
        door = self.DOOR.compile(registry)
        assert not door.unresolved, door.unresolved
        assert len(door.envelopes) == 1
        typing = door.envelopes[0].registry
        assert typing is not None and typing.predicate == self.DOOR.predicate
        assert typing.registry_digest == registry.digest
        qualifiers = self.pure_qualifiers(registry)
        assert qualifiers
        owner = registry.owner("predicate", self.FACT.predicate)
        allow = QualifierFilter(topical_domains=frozenset({self.DOMAIN}))
        for q in qualifiers:
            assert q.topical_domain is not None
            assert domain_covers(self.DOMAIN, q.topical_domain)
            assert allow.matches(q)
            assert q.vocabulary_pack == owner
        self.extra_positive(registry, qualifiers)

    def test_negative(self, registry: SemanticRegistry) -> None:
        assert self.NEGATIVE
        for claim, match in self.NEGATIVE:
            with pytest.raises(ValueError, match=match):
                claim.validate(registry)
        bad = self.DOOR_NEGATIVE.compile(registry)
        assert not bad.envelopes
        assert [u.reason for u in bad.unresolved] == ["semantic_validation_failed"]
        others = frozenset(set(FAMILY_DOMAINS) - {self.DOMAIN})
        deny = QualifierFilter(exclude_topical_domains=frozenset({self.DOMAIN}))
        for q in self.pure_qualifiers(registry):
            assert not QualifierFilter(topical_domains=others).matches(q)
            assert not deny.matches(q)

    async def test_known_time(self, engine: Engine) -> None:
        before = datetime.now(UTC) - timedelta(seconds=1)
        record_id = await write_fact(engine, self.FACT)
        assert (await compile_graph_edges(engine._pipeline_ctx()))["status"] == "ok"
        after = datetime.now(UTC) + timedelta(days=1)
        edges = [e for e in await compiled_edges(engine, record_id) if e.weight > 0]
        assert edges
        for edge in edges:
            q = edge_qualifiers(edge.properties)
            assert q is not None and q.known_at is not None and before <= q.known_at <= after
            assert not QualifierFilter(known_as_of=before).matches(q)
            assert QualifierFilter(
                known_as_of=after, topical_domains=frozenset({self.DOMAIN})
            ).matches(q)
            assert not QualifierFilter(valid_at=T0 - timedelta(days=30)).matches(q)

    async def test_replay(self, engine: Engine) -> None:
        record_id = await write_fact(engine, self.FACT)
        first = await compile_graph_edges(engine._pipeline_ctx())
        assert first["status"] == "ok" and first["written"] > 0
        snap = _snapshot(await compiled_edges(engine, record_id))
        assert snap and all(row[3] for row in snap)
        again = await compile_graph_edges(engine._pipeline_ctx())
        assert again.get("written", 0) == 0
        assert _snapshot(await compiled_edges(engine, record_id)) == snap
        await engine.rebuild()
        await compile_graph_edges(engine._pipeline_ctx())
        assert _snapshot(await compiled_edges(engine, record_id)) == snap

    async def test_erasure(self, engine: Engine) -> None:
        record_id = await write_fact(engine, self.FACT)
        await compile_graph_edges(engine._pipeline_ctx())
        assert [e for e in await compiled_edges(engine, record_id) if e.weight > 0]
        await engine.forget(record_id, namespace=NS, hard=True)
        await compile_graph_edges(engine._pipeline_ctx())
        remaining = await compiled_edges(engine, record_id)
        assert not [e for e in remaining if e.weight > 0]
        allow = QualifierFilter(topical_domains=frozenset({self.DOMAIN}))
        for edge in remaining:  # tombstones only: erased edges never pass a filter
            q = edge_qualifiers(edge.properties)
            assert q is not None and not q.traversable
            assert not allow.matches(q) and not QualifierFilter().matches(q)
