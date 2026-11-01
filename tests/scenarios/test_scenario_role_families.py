"""RM-47 / RM-50: per-role-family synthetic fixtures for the agent-memory families 1-3
(docs/BI01_CONTRACT_REWRITE_PLAN.md "Owner review decisions 2026-10-22" and "Registry
contracts implemented"):

- family 1, commitments and requests: ``committed_to`` (obligor, action, beneficiary),
  ``requested_of`` (requester, addressee, action), ``due_by``; qualifier ``priority``;
- family 2, decisions and work: ``decided`` (decider, preferred, alternative), ``works_on``,
  ``blocked_by``; qualifiers ``progress``, ``outcome``;
- family 3, documents: ``cites``, ``about``, ``derived_from``, ``revision_of``; qualifier
  ``locator``, descriptive keys ``publisher``, ``licence``.

No rule extracts these predicates yet, so a scripted non-generative extractor (behind the
DS-18 extractor hook) states exact-span candidates; everything else is the real path: the
canonical compiler and registry gate, the deposit door, the edge compiler, rebuild and
erasure. Each positive case has negative variants that must stay unresolved."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from scenario_harness import (
    ScriptedExtractor,
    assert_erased,
    assert_replay,
    binding,
    by_predicate,
    candidate,
    claims,
    edges,
    edges_of,
    entity_ids,
    envelope_evidence,
    msg,
    send,
    settle,
    support,
    unresolved,
)
from scenario_harness import boot as _boot

from memspine.semantics import QualifierValue, load_builtin_registry

Build = Callable[[Any, Any], list[Any]]


async def _run(
    monkeypatch: pytest.MonkeyPatch, path: Path, cases: list[tuple[str, str, Build]]
) -> tuple[Any, ScriptedExtractor, dict[str, Any]]:
    extractor = ScriptedExtractor()
    for _, text, build in cases:
        extractor.on(text, build)
    eng = await _boot(monkeypatch, path, extractor=extractor)
    sources = await send(
        eng,
        [
            msg(f"m-{i}", sender, text, f"2026-10-0{i + 1}T09:00Z")
            for i, (sender, text, _) in enumerate(cases)
        ],
    )
    await settle(eng)
    return eng, extractor, {s.content: s for s in sources}


def _reasons(eng: Any) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for note in unresolved(eng):
        assert note.asserted is False
        out.setdefault(note.attribute or "", []).append(note.reason)
    return out


# ------------------------------------------------------------------ family 1

COMMIT = "I will send the quarterly report to Kim."
REQUEST = "Ola, please review the design doc."


def _commit(r: Any, s: Any) -> list[Any]:
    ev = support(r, COMMIT)
    return [
        candidate(
            r,
            s,
            predicate="committed_to",
            obj="quarterly report",
            object_entity_type="document",
            evidence=ev,
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


F1_NEG = {
    "missing obligor": (
        "I will draft the budget memo.",
        lambda r, s: [
            candidate(
                r,
                s,
                predicate="committed_to",
                obj="budget memo",
                object_entity_type="document",
                evidence=support(r, "I will draft the budget memo."),
                roles=[
                    binding(
                        1,
                        "action",
                        "draft the budget memo",
                        "text",
                        support(r, "draft the budget memo"),
                    )
                ],
            )
        ],
    ),
    "two obligors": (
        "Ola and Kim will write the launch plan.",
        lambda r, s: [
            candidate(
                r,
                s,
                predicate="committed_to",
                obj="launch plan",
                object_entity_type="document",
                evidence=support(r, "Ola and Kim will write the launch plan."),
                roles=[
                    binding(1, "obligor", "Ola", "entity", support(r, "Ola"), "person"),
                    binding(2, "obligor", "Kim", "entity", support(r, "Kim"), "person"),
                    binding(
                        3,
                        "action",
                        "write the launch plan",
                        "text",
                        support(r, "write the launch plan"),
                    ),
                ],
            )
        ],
    ),
    "action without exact span": (
        "I will fix the release notes.",
        lambda r, s: [
            candidate(
                r,
                s,
                predicate="committed_to",
                obj="release notes",
                object_entity_type="document",
                evidence=support(r, "I will fix the release notes."),
                roles=[
                    binding(1, "obligor", "Ola", "entity", envelope_evidence(r), "person"),
                    binding(2, "action", "fix the release notes", "text", envelope_evidence(r)),
                ],
            )
        ],
    ),
    "beneficiary of the wrong type": (
        "I will mail the invoice to Paris.",
        lambda r, s: [
            candidate(
                r,
                s,
                predicate="committed_to",
                obj="invoice",
                object_entity_type="document",
                evidence=support(r, "I will mail the invoice to Paris."),
                roles=[
                    binding(1, "obligor", "Ola", "entity", envelope_evidence(r), "person"),
                    binding(
                        2, "action", "mail the invoice", "text", support(r, "mail the invoice")
                    ),
                    binding(3, "beneficiary", "Paris", "entity", support(r, "Paris"), "place"),
                ],
            )
        ],
    ),
    "request without addressee": (
        "Please review the test plan.",
        lambda r, s: [
            candidate(
                r,
                s,
                predicate="requested_of",
                obj="test plan",
                object_entity_type="document",
                evidence=support(r, "Please review the test plan."),
                roles=[
                    binding(1, "requester", "Ola", "entity", envelope_evidence(r), "person"),
                    binding(
                        2,
                        "action",
                        "review the test plan",
                        "text",
                        support(r, "review the test plan"),
                    ),
                ],
            )
        ],
    ),
}


async def test_family1_commitments_and_requests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cases: list[tuple[str, str, Build]] = [("Ola", COMMIT, _commit), ("Kim", REQUEST, _request)]
    cases += [("Ola", text, build) for text, build in F1_NEG.values()]
    eng, _, src = await _run(monkeypatch, tmp_path / "fam1.db", cases)
    try:
        found = await claims(eng)
        [commit] = by_predicate(found, "committed_to")
        [request] = by_predicate(found, "requested_of")
        assert (commit.subject, commit.object, commit.principal) == (
            "Ola",
            "quarterly report",
            "Ola",
        )
        assert (commit.modality, commit.status, commit.outcome) == ("asserted", "active", "none")
        assert commit.role("obligor") == [("Ola", "entity", "person", ())]
        assert commit.role("action") == [
            ("send the quarterly report", "text", None, ("send the quarterly report",))
        ]
        assert commit.role("beneficiary") == [("Kim", "entity", "person", ("Kim",))]
        assert commit.evidence == ((src[COMMIT].record_id, 0, len(COMMIT), COMMIT),)
        assert (request.subject, request.object) == ("Kim", "design doc")
        assert [r[0] for r in request.roles] == ["action", "addressee", "requester"]
        # every negative variant is a typed, unasserted, source-bound annotation
        assert _reasons(eng) == {
            "committed_to": ["semantic_validation_failed"] * 4,
            "requested_of": ["semantic_validation_failed"],
        }
        # graph: the claim's subject and its document object bind to entities
        [doc] = await entity_ids(eng, "quarterly report")
        mine = edges_of(await edges(eng), commit.record_id)
        assert {(e.rel, e.dst) for e in mine if e.rel == "role:object"} == {("role:object", doc)}
        assert {e.claim_state for e in mine} == {"current"}

        await assert_replay(eng, ["Ola", "Kim", "quarterly report", "design doc"])
        await assert_erased(eng, src[COMMIT], [commit.record_id], "quarterly report")
        assert [c.predicate for c in await claims(eng)] == ["requested_of"]
    finally:
        await eng.stop()


async def test_family1_due_by_deadline_needs_its_precision_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``due_by`` is single-valued per precision (cardinality scope ``precision``): with the
    scope stated the aware deadline compiles; without it the candidate stays unresolved."""
    text = "The quarterly report is due by 2026-11-05T17:00+01:00."
    vague = "The budget memo is due by 2026-11-07T12:00+01:00."

    def due(subject: str, quote: str, scope: dict[str, str] | None) -> Build:
        return lambda r, s: [
            candidate(
                r,
                s,
                predicate="due_by",
                obj=quote,
                subject=subject,
                subject_type="document",
                object_kind="temporal",
                evidence=support(r, r.content),
                cardinality_scope=scope,
            )
        ]

    cases: list[tuple[str, str, Build]] = [
        ("Ola", text, due("quarterly report", "2026-11-05T17:00+01:00", {"precision": "minute"})),
        ("Ola", vague, due("budget memo", "2026-11-07T12:00+01:00", None)),
    ]
    eng, _, src = await _run(monkeypatch, tmp_path / "due.db", cases)
    try:
        [deadline] = by_predicate(await claims(eng), "due_by")
        assert (deadline.subject, deadline.object) == ("quarterly report", "2026-11-05T17:00+01:00")
        assert deadline.sources == (src[text].record_id,)
        assert _reasons(eng) == {"due_by": ["semantic_validation_failed"]}
        # a temporal value is not an entity: no object entity node
        assert await entity_ids(eng, "2026-11-05T17:00+01:00") == []
        await assert_replay(eng, ["quarterly report"])
        await assert_erased(eng, src[text], [deadline.record_id], "quarterly report due")
    finally:
        await eng.stop()


# ------------------------------------------------------------------ family 2

DECIDED = "I decided on Postgres over MySQL."
WORKS = "I work on Project Atlas."
BLOCKED = "Project Atlas is blocked by the security review."

F2_POS: list[tuple[str, str, Build]] = [
    (
        "Ola",
        DECIDED,
        lambda r, s: [
            candidate(
                r,
                s,
                predicate="decided",
                obj="Postgres",
                object_entity_type="software",
                evidence=support(r, DECIDED),
                roles=[
                    binding(1, "decider", "Ola", "entity", envelope_evidence(r), "person"),
                    binding(
                        2, "preferred", "Postgres", "entity", support(r, "Postgres"), "software"
                    ),
                    binding(3, "alternative", "MySQL", "entity", support(r, "MySQL"), "software"),
                ],
            )
        ],
    ),
    (
        "Ola",
        WORKS,
        lambda r, s: [
            candidate(
                r,
                s,
                predicate="works_on",
                obj="Project Atlas",
                object_entity_type="project",
                evidence=support(r, WORKS),
            )
        ],
    ),
    (
        "Ola",
        BLOCKED,
        lambda r, s: [
            candidate(
                r,
                s,
                predicate="blocked_by",
                obj="security review",
                object_entity_type="work",
                subject="Project Atlas",
                subject_type="project",
                evidence=support(r, BLOCKED),
            )
        ],
    ),
]
F2_NEG: list[tuple[str, str, Build]] = [
    (
        "Ola",
        "I decided against Redis.",
        lambda r, s: [  # no preferred binding
            candidate(
                r,
                s,
                predicate="decided",
                obj="Redis",
                object_entity_type="software",
                evidence=support(r, "I decided against Redis."),
                roles=[binding(1, "decider", "Ola", "entity", envelope_evidence(r), "person")],
            )
        ],
    ),
    (
        "Ola",
        "I work on making things better.",
        lambda r, s: [  # works_on takes an entity object only
            candidate(
                r,
                s,
                predicate="works_on",
                obj="making things better",
                object_kind="text",
                evidence=support(r, "I work on making things better."),
            )
        ],
    ),
    (
        "Ola",
        "Kim is blocked by the outage.",
        lambda r, s: [  # a person is not a blockable work item
            candidate(
                r,
                s,
                predicate="blocked_by",
                obj="outage",
                object_entity_type="event",
                subject="Kim",
                subject_type="person",
                evidence=support(r, "Kim is blocked by the outage."),
            )
        ],
    ),
]


async def test_family2_decisions_and_work(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    eng, _, src = await _run(monkeypatch, tmp_path / "fam2.db", F2_POS + F2_NEG)
    try:
        found = await claims(eng)
        [decided] = by_predicate(found, "decided")
        [works] = by_predicate(found, "works_on")
        [blocked] = by_predicate(found, "blocked_by")
        assert decided.role("preferred") == [("Postgres", "entity", "software", ("Postgres",))]
        assert decided.role("alternative") == [("MySQL", "entity", "software", ("MySQL",))]
        assert (works.subject, works.object) == ("Ola", "Project Atlas")
        assert (blocked.subject, blocked.object, blocked.principal) == (
            "Project Atlas",
            "security review",
            "Ola",
        )
        assert _reasons(eng) == {
            "decided": ["semantic_validation_failed"],
            "works_on": ["semantic_validation_failed"],
            "blocked_by": ["semantic_validation_failed"],
        }
        # the project entity is shared by works_on (object) and blocked_by (subject)
        [atlas] = await entity_ids(eng, "Project Atlas")
        graph = await edges(eng)
        assert atlas in {e.dst for e in edges_of(graph, works.record_id)}
        assert atlas in {e.dst for e in edges_of(graph, blocked.record_id)}

        await assert_replay(eng, ["Ola", "Postgres", "Project Atlas", "security review"])
        await assert_erased(eng, src[BLOCKED], [blocked.record_id], "Project Atlas blocked")
        assert sorted(c.predicate for c in await claims(eng)) == ["decided", "works_on"]
        assert await entity_ids(eng, "Project Atlas") == [atlas]
    finally:
        await eng.stop()


# ------------------------------------------------------------------ family 3

CITES = "The design doc cites RFC 9110."
ABOUT = "The design doc is about caching."
DERIVED = "The v2 spec is derived from the v1 spec."
REVISION = "Release 2.1 is a revision of release 2.0."

F3_POS: list[tuple[str, str, Build]] = [
    (
        "Ola",
        CITES,
        lambda r, s: [
            candidate(
                r,
                s,
                predicate="cites",
                obj="RFC 9110",
                object_entity_type="document",
                subject="design doc",
                subject_type="document",
                evidence=support(r, CITES),
            )
        ],
    ),
    (
        "Ola",
        ABOUT,
        lambda r, s: [
            candidate(
                r,
                s,
                predicate="about",
                obj="caching",
                object_kind="text",
                subject="design doc",
                subject_type="document",
                evidence=support(r, ABOUT),
            )
        ],
    ),
    (
        "Ola",
        DERIVED,
        lambda r, s: [
            candidate(
                r,
                s,
                predicate="derived_from",
                obj="v1 spec",
                object_entity_type="document",
                subject="v2 spec",
                subject_type="document",
                evidence=support(r, DERIVED),
            )
        ],
    ),
    (
        "Ola",
        REVISION,
        lambda r, s: [
            candidate(
                r,
                s,
                predicate="revision_of",
                obj="release 2.0",
                object_entity_type="software",
                subject="Release 2.1",
                subject_type="software",
                evidence=support(r, REVISION),
            )
        ],
    ),
]
F3_NEG: list[tuple[str, str, Build]] = [
    (
        "Ola",
        "Kim cites RFC 7231.",
        lambda r, s: [  # a person is not a citing work
            candidate(
                r,
                s,
                predicate="cites",
                obj="RFC 7231",
                object_entity_type="document",
                subject="Kim",
                subject_type="person",
                evidence=support(r, "Kim cites RFC 7231."),
            )
        ],
    ),
    (
        "Ola",
        "The roadmap is a revision of Project Atlas.",
        lambda r, s: [  # a project is not a revisable work
            candidate(
                r,
                s,
                predicate="revision_of",
                obj="Project Atlas",
                object_entity_type="project",
                subject="roadmap",
                subject_type="document",
                evidence=support(r, "The roadmap is a revision of Project Atlas."),
            )
        ],
    ),
    (
        "Ola",
        "The summary is derived from what we said.",
        lambda r, s: [  # derived_from takes an entity object only
            candidate(
                r,
                s,
                predicate="derived_from",
                obj="what we said",
                object_kind="text",
                subject="summary",
                subject_type="document",
                evidence=support(r, "The summary is derived from what we said."),
            )
        ],
    ),
    (
        "Ola",
        "The memo is about Kim, by Kim.",
        lambda r, s: [  # ``about`` declares no roles
            candidate(
                r,
                s,
                predicate="about",
                obj="Kim",
                object_entity_type="person",
                subject="memo",
                subject_type="document",
                evidence=support(r, "The memo is about Kim, by Kim."),
                roles=[binding(1, "actor", "Ola", "entity", envelope_evidence(r), "person")],
            )
        ],
    ),
]


async def test_family3_documents(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    eng, _, src = await _run(monkeypatch, tmp_path / "fam3.db", F3_POS + F3_NEG)
    try:
        found = await claims(eng)
        assert sorted((c.predicate, c.subject, c.object) for c in found) == [
            ("about", "design doc", "caching"),
            ("cites", "design doc", "RFC 9110"),
            ("derived_from", "v2 spec", "v1 spec"),
            ("revision_of", "Release 2.1", "release 2.0"),
        ]
        assert {c.principal for c in found} == {"Ola"}  # the sender reported it
        assert _reasons(eng) == {
            "cites": ["semantic_validation_failed"],
            "revision_of": ["semantic_validation_failed"],
            "derived_from": ["semantic_validation_failed"],
            "about": ["semantic_validation_failed"],
        }
        [doc] = await entity_ids(eng, "design doc")
        [rfc] = await entity_ids(eng, "RFC 9110")
        [cites] = by_predicate(found, "cites")
        assert (doc, rfc) in {(e.src, e.dst) for e in await edges(eng) if e.rel == "cites"}
        # a text object ("caching") is a value, not an entity
        assert await entity_ids(eng, "caching") == []

        await assert_replay(eng, ["design doc", "RFC 9110", "v1 spec", "release 2.0"])
        await assert_erased(eng, src[CITES], [cites.record_id], "design doc cites")
        assert by_predicate(await claims(eng), "cites") == []
        assert len(by_predicate(await claims(eng), "about")) == 1
    finally:
        await eng.stop()


# ------------------------------------------------------------------ family qualifiers


@pytest.mark.parametrize(
    ("predicate", "key", "ok"),
    [
        ("committed_to", "priority", True),
        ("requested_of", "priority", True),
        ("works_on", "progress", True),
        ("works_on", "outcome", True),
        ("decided", "outcome", True),
        ("cites", "locator", True),
        ("derived_from", "locator", True),
        ("cites", "priority", False),
        ("decided", "progress", False),
        ("revision_of", "locator", False),
    ],
)
def test_family_qualifier_keys(predicate: str, key: str, ok: bool) -> None:
    registry = load_builtin_registry()
    spec = registry.predicates[predicate]
    subject = spec.subject_types[0]
    kind = spec.value_kinds[0]
    entity_type = spec.object_entity_types[0] if kind == "entity" else None
    roles = list(spec.required_roles)
    call = dict(
        subject_type=subject,
        value_kind=kind,
        object_entity_type=entity_type,
        role_names=roles,
        qualifiers={key: QualifierValue(kind="text", text="high")},
    )
    if ok:
        assert registry.validate_compatibility(predicate, **call).name == predicate  # type: ignore[arg-type]
    else:
        with pytest.raises(ValueError, match="not registered"):
            registry.validate_compatibility(predicate, **call)  # type: ignore[arg-type]


@pytest.mark.parametrize("key", ["publisher", "licence"])
def test_family3_descriptive_keys(key: str) -> None:
    registry = load_builtin_registry()
    assert (
        registry.validate_compatibility(
            "has_attribute", subject_type="software", value_kind="text", attribute_key=key
        ).name
        == "has_attribute"
    )
    with pytest.raises(ValueError):  # descriptive document keys: never on a person
        registry.validate_compatibility(
            "has_attribute", subject_type="person", value_kind="text", attribute_key=key
        )
