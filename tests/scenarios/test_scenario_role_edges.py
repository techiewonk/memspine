"""RM-157 (ADR-094 amendment 2026-11-02): claim role bindings compile into qualified graph edges.

Families 1-3 (commitments, decisions, documents), E (feelings, beliefs) and H (communication
acts) through the real path: a scripted non-generative extractor states exact-span candidates,
the canonical fact door validates them, the deposit door stores the claim, the entity index
links the role fillers, and the edge compiler emits ``role:<role>`` edges from the claim's
assertion node to each filler's entity node. Checked here:

* the role edges per family, their endpoints and that they carry the claim's qualifiers
  (modality, status, outcome, valid time, domain, sensitivity, statement kind, claim
  qualifiers) with ``event_role`` = the role name;
* fillers that are not entities, or that the entity policy rejects, compile no edge and are
  reported with their reason;
* ``graph_filter`` on a role edge (graph store walk and the search graph leg);
* replay equality of the role edges and their properties;
* erasure removes the role edges and no filter reaches them afterwards.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from scenario_harness import (
    NS,
    ScriptedExtractor,
    assert_erased,
    binding,
    by_predicate,
    candidate,
    claims,
    entity_ids,
    envelope_evidence,
    msg,
    send,
    settle,
    support,
)
from scenario_harness import boot as _boot

from memspine.derive.pipeline.stages.edge_compiler import compile_graph_edges
from memspine.observability import tap
from memspine.services.graph.qualifiers import EdgeQualifiers, QualifierFilter, edge_qualifiers

Build = Callable[[Any, Any], list[Any]]

COMMIT = "I will send the quarterly report to Kim."
REQUEST = "Ola, please review the design doc."
DECIDED = "I decided on Postgres over MySQL."
CITES = "The design doc cites RFC 9110 on page 4."
FEELS = "I feel anxious about Bram."
TALK = "I asked Tomasz about the trip."
UNBOUND = "I will mail the invoice to you."


def _commit(r: Any, s: Any) -> list[Any]:
    return [
        candidate(
            r,
            s,
            predicate="committed_to",
            obj="quarterly report",
            object_entity_type="document",
            evidence=support(r, COMMIT),
            qualifiers={"priority": "high"},
            roles=[
                binding(1, "obligor", "Ola", "entity", envelope_evidence(r), "person"),
                binding(
                    2,
                    "action",
                    "send the quarterly report",
                    "text",
                    support(r, "send the quarterly report"),
                ),
                binding(3, "beneficiary", "Kim", "entity", support(r, "Kim"), "person"),
            ],
        )
    ]


def _request(r: Any, s: Any) -> list[Any]:
    return [
        candidate(
            r,
            s,
            predicate="requested_of",
            obj="design doc",
            object_entity_type="document",
            evidence=support(r, REQUEST),
            roles=[
                binding(1, "requester", "Kim", "entity", envelope_evidence(r), "person"),
                binding(2, "addressee", "Ola", "entity", support(r, "Ola"), "person"),
                binding(
                    3,
                    "action",
                    "review the design doc",
                    "text",
                    support(r, "review the design doc"),
                ),
            ],
        )
    ]


def _decided(r: Any, s: Any) -> list[Any]:
    return [
        candidate(
            r,
            s,
            predicate="decided",
            obj="Postgres",
            object_entity_type="software",
            evidence=support(r, DECIDED),
            roles=[
                binding(1, "decider", "Ola", "entity", envelope_evidence(r), "person"),
                binding(2, "preferred", "Postgres", "entity", support(r, "Postgres"), "software"),
                binding(3, "alternative", "MySQL", "entity", support(r, "MySQL"), "software"),
            ],
        )
    ]


def _cites(r: Any, s: Any) -> list[Any]:
    return [
        candidate(
            r,
            s,
            predicate="cites",
            obj="RFC 9110",
            object_entity_type="document",
            subject="design doc",
            subject_type="document",
            evidence=support(r, CITES),
            qualifiers={"locator": "page 4"},
        )
    ]


def _feels(r: Any, s: Any) -> list[Any]:
    return [
        candidate(
            r,
            s,
            predicate="feels",
            obj="anxious",
            object_kind="text",
            evidence=support(r, FEELS),
            roles=[binding(1, "topic", "Bram", "entity", support(r, "Bram"), "person")],
        )
    ]


def _talk(r: Any, s: Any) -> list[Any]:
    return [
        candidate(
            r,
            s,
            predicate="communicated_with",
            obj="Tomasz",
            object_entity_type="person",
            evidence=support(r, TALK),
            qualifiers={"speech_act": "ask"},
        )
    ]


def _unbound(r: Any, s: Any) -> list[Any]:
    return [
        candidate(
            r,
            s,
            predicate="committed_to",
            obj="invoice",
            object_entity_type="document",
            evidence=support(r, UNBOUND),
            roles=[
                binding(1, "obligor", "Ola", "entity", envelope_evidence(r), "person"),
                binding(2, "action", "mail the invoice", "text", support(r, "mail the invoice")),
                # a pronoun: the entity policy rejects it, so no entity is invented for it
                binding(3, "beneficiary", "you", "entity", support(r, "you"), "person"),
            ],
        )
    ]


CASES: list[tuple[str, str, Build]] = [
    ("Ola", COMMIT, _commit),
    ("Kim", REQUEST, _request),
    ("Ola", DECIDED, _decided),
    ("Ola", CITES, _cites),
    ("Noor", FEELS, _feels),
    ("Noor", TALK, _talk),
    ("Ola", UNBOUND, _unbound),
]


class _Spy:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

        def event(stage: str, /, **fields: Any) -> None:
            self.events.append((stage, fields))

        monkeypatch.setattr(tap, "event", event)
        monkeypatch.setattr(tap, "enabled", lambda: True)

    def of(self, stage: str) -> list[dict[str, Any]]:
        return [f for s, f in self.events if s == stage]


async def _run(monkeypatch: pytest.MonkeyPatch, path: Path) -> tuple[Any, dict[str, Any], _Spy]:
    extractor = ScriptedExtractor()
    for _, text, build in CASES:
        extractor.on(text, build)
    spy = _Spy(monkeypatch)
    eng = await _boot(
        monkeypatch,
        path,
        extractor=extractor,
        read={"graph_leg": True, "graph_leg_seed_hits": 0, "record_access": False},
    )
    sources = await send(
        eng,
        [
            msg(f"m-{i}", sender, text, f"2026-10-0{i + 1}T09:00Z")
            for i, (sender, text, _) in enumerate(CASES)
        ],
    )
    await settle(eng)
    return eng, {s.content: s for s in sources}, spy


async def _compiled(eng: Any, record_id: str | None = None) -> list[Any]:
    """The live compiled edges (of one claim)."""
    assert eng._graph is not None
    return sorted(
        (
            e
            for e in await eng._graph.edge_list(NS)
            if e.properties.get("compiled") == "edge_compiler"
            and e.weight > 0
            and (record_id is None or e.properties.get("record_id") == record_id)
        ),
        key=lambda e: (e.rel_type, e.src, e.dst),
    )


_FIXED = {"role:subject", "role:object", "role:value", "role:time"}


def _bindings(edges: list[Any]) -> dict[str, list[str]]:
    """``role -> filler nodes`` of the role-binding edges (the fixed roles left out)."""
    out: dict[str, list[str]] = {}
    for e in edges:
        if e.rel_type.startswith("role:") and e.rel_type not in _FIXED:
            out.setdefault(e.rel_type.removeprefix("role:"), []).append(e.dst)
    return out


def _quals(edge: Any) -> EdgeQualifiers:
    q = edge_qualifiers(edge.properties)
    assert q is not None
    return q


def _one(edges: list[Any], rel: str) -> Any:
    [edge] = [e for e in edges if e.rel_type == rel]
    return edge


async def test_role_bindings_compile_to_qualified_edges(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng, src, spy = await _run(monkeypatch, tmp_path / "roles.db")
    try:
        found = await claims(eng)
        [commit, unbound] = by_predicate(found, "committed_to")
        assert commit.object == "quarterly report"
        [request] = by_predicate(found, "requested_of")
        [decided] = by_predicate(found, "decided")
        [cites] = by_predicate(found, "cites")
        [feels] = by_predicate(found, "feels")
        [talk] = by_predicate(found, "communicated_with")
        [ola] = await entity_ids(eng, "Ola")
        [kim] = await entity_ids(eng, "Kim")
        [postgres] = await entity_ids(eng, "Postgres")
        [mysql] = await entity_ids(eng, "MySQL")
        [bram] = await entity_ids(eng, "Bram")

        # ---- family 1: commitments and requests
        mine = await _compiled(eng, commit.record_id)
        assert _bindings(mine) == {"beneficiary": [kim], "obligor": [ola]}  # action is text
        subject = _one(mine, "role:subject")
        beneficiary = _one(mine, "role:beneficiary")
        assert beneficiary.src == subject.src  # from the claim's assertion (event) node
        q_subject, q_role = _quals(subject), _quals(beneficiary)
        assert q_role.event_role == "beneficiary"
        # every claim qualifier carries over; only the edge's role differs
        assert q_role.model_copy(update={"event_role": "subject"}) == q_subject
        assert (
            q_role.assertion_modality is not None and q_role.assertion_modality.value == "asserted"
        )
        assert q_role.claim_state is not None and q_role.claim_state.value == "current"
        assert q_role.lifecycle_outcome is None
        assert q_role.valid_from is not None and q_role.known_at is not None
        assert q_role.topical_domain is not None and q_role.topical_domain.startswith("work")
        assert q_role.sensitivity_checked is not None
        assert q_role.statement_kind == "assertion"
        assert q_role.claim_qualifiers == {"priority": "high"}
        assert q_role.claim_id == commit.record_id
        commit_claim = await eng.get_envelope(commit.record_id, namespace=NS)
        assert commit_claim is not None and commit_claim.statement_kind == "assertion"
        assert {k: v.text for k, v in commit_claim.qualifiers.items()} == {"priority": "high"}
        assert _bindings(await _compiled(eng, request.record_id)) == {
            "addressee": [ola],
            "requester": [kim],
        }

        # ---- family 2: decisions
        assert _bindings(await _compiled(eng, decided.record_id)) == {
            "alternative": [mysql],
            "decider": [ola],
            "preferred": [postgres],
        }

        # ---- family 3: documents have no role slots; their qualifiers pass through
        cited = await _compiled(eng, cites.record_id)
        assert cited and not _bindings(cited)
        assert all(_quals(e).claim_qualifiers == {"locator": "page 4"} for e in cited)

        # ---- family E: an attributed claim, its topic role bound
        felt = await _compiled(eng, feels.record_id)
        assert _bindings(felt) == {"topic": [bram]}
        felt_claim = await eng.get_envelope(feels.record_id, namespace=NS)
        assert felt_claim is not None and felt_claim.statement_kind == "attributed"
        assert {_quals(e).statement_kind for e in felt} == {"attributed"}
        assert QualifierFilter(statement_kinds=frozenset({"attributed"})).matches(
            _quals(_one(felt, "role:topic"))
        )

        # ---- family H: a speech act qualifier, no role slots
        talked = await _compiled(eng, talk.record_id)
        assert talked and not _bindings(talked)
        assert all(_quals(e).claim_qualifiers == {"speech_act": "ask"} for e in talked)

        # ---- an unbound filler is skipped with a reason, never invented
        assert _bindings(await _compiled(eng, unbound.record_id)) == {"obligor": [ola]}
        assert await entity_ids(eng, "you") == []
        reasons: dict[str, int] = {}
        for event in spy.of("graph.compile.role_skipped"):
            for reason, n in event["reasons"].items():
                reasons[reason] = max(reasons.get(reason, 0), n)
        assert reasons == {"not_an_entity": 3, "rejected_by_entity_policy": 1}

        # ---- graph_filter on a role edge: the graph store walk and the search graph leg
        assert eng._graph is not None
        only = QualifierFilter(event_roles=frozenset({"beneficiary"}))
        reached = await eng._graph.neighbors(kim, namespace=NS, qualifier_filter=only)
        assert [n.node_id for n in reached] == [beneficiary.src]
        assert not await eng._graph.neighbors(
            kim, namespace=NS, qualifier_filter=QualifierFilter(event_roles=frozenset({"decider"}))
        )
        flt = QualifierFilter(
            known_as_of=datetime.now(UTC) + timedelta(days=1),
            event_roles=frozenset({"beneficiary"}),
        )
        spy.events.clear()
        await eng.search("Kim", namespace=NS, top_k=5, graph_filter=flt)
        [filtered] = spy.of("read.graph_filter")
        assert filtered["admitted"] == 1  # the beneficiary edge into Kim, nothing else

        # ---- replay: a rebuild and a recompile restore identical role edges
        def snap(edges: list[Any]) -> list[tuple[Any, ...]]:
            return [(e.src, e.dst, e.rel_type, e.weight, e.properties) for e in edges]

        before = snap(await _compiled(eng))
        assert sum(1 for e in await _compiled(eng) if e.rel_type not in _FIXED) > 0
        await eng.rebuild()
        assert snap(await _compiled(eng)) == before
        again = await compile_graph_edges(eng._pipeline_ctx())
        assert again["status"] == "ok" and again.get("written", 0) == 0
        assert snap(await _compiled(eng)) == before

        # ---- erasure removes the role edges; no filter reaches them afterwards
        await assert_erased(eng, src[COMMIT], [commit.record_id], "quarterly report")
        assert not await _compiled(eng, commit.record_id)
        assert not await eng._graph.neighbors(kim, namespace=NS, qualifier_filter=only)
        tombstones = [
            e
            for e in await eng._graph.edge_list(NS)
            if e.properties.get("record_id") == commit.record_id
        ]
        assert all(e.weight <= 0 for e in tombstones)
        # the other claims keep theirs
        assert _bindings(await _compiled(eng, request.record_id)) == {
            "addressee": [ola],
            "requester": [kim],
        }
    finally:
        await eng.stop()
