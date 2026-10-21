"""Generate proposed review schemas/examples only; not imported by the engine."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parent
D = {}


def ref(name):
    return {"$ref": f"#/$defs/{name}"}


def enum(*values):
    return {"enum": list(values)}


def nullable(value):
    return {"anyOf": [value, {"type": "null"}]}


def array(value, minimum=0):
    return {"type": "array", "items": value, "minItems": minimum}


def obj(fields, optional=()):
    return {
        "type": "object",
        "properties": fields,
        "required": [k for k in fields if k not in optional],
        "additionalProperties": False,
    }


S = {"type": "string", "minLength": 1}
I = {"type": "integer", "minimum": 0}
V = {"type": "integer", "minimum": 1}
B = {"type": "boolean"}
TS = {"type": "string", "format": "date-time"}
HASH = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
D["Scope"] = obj(dict(tenant=S, owner=S, owner_kind=enum("user", "group"), namespace=S))
D["Ref"] = obj(dict(id=S, version=V))
D["Producer"] = obj(
    dict(
        kind=enum("rule", "lexical", "embedding", "ner", "reranker", "catalogue", "generative_llm"),
        name=S,
        version=S,
        settings_hash=HASH,
        model_id=nullable(S),
    )
)
D["SourceClock"] = obj(
    dict(
        created_at=nullable(TS),
        original_text=nullable(S),
        timezone=nullable(S),
        timestamp_origin=enum("provided", "generated", "unknown"),
        timezone_origin=enum("provided", "explicit_context", "unknown"),
    )
)
D["SourceSnapshot"] = obj(
    dict(
        source=ref("Ref"),
        scope=ref("Scope"),
        content_hash=HASH,
        message_id=nullable(S),
        session_id=nullable(S),
        conversation_id=nullable(S),
        sender_id=nullable(S),
        clock=ref("SourceClock"),
        erasure_generation=I,
        available_at=TS,
        available_seq=I,
    )
)
D["Span"] = obj(dict(start=I, end=I, quote=S))
D["Evidence"] = obj(
    dict(
        evidence_id=S,
        role=enum("support", "binding", "context"),
        origin=enum(
            "raw_message",
            "document",
            "attachment_text",
            "image_caption",
            "catalogue",
            "collection_metadata",
        ),
        source=ref("SourceSnapshot"),
        seg_index=I,
        span=nullable(ref("Span")),
        source_family=S,
    )
)
D["EntityRef"] = obj(dict(entity_id=nullable(S), mention_id=nullable(S)))
D["Entity"] = obj(
    dict(
        entity_id=S,
        revision=V,
        scope=ref("Scope"),
        type=enum(
            "person",
            "agent",
            "group",
            "organisation",
            "place",
            "object",
            "work",
            "project",
            "species",
            "other",
        ),
        world=enum("real", "fictional", "symbolic", "depicted", "unknown"),
        label=S,
        identity_basis=enum("stable_id", "explicit_identity"),
        identity_evidence_ids=array(S),
        status=enum("active", "archived"),
        known_since=TS,
        known_seq=I,
    )
)
D["Mention"] = obj(
    dict(
        mention_id=S,
        source=ref("SourceSnapshot"),
        seg_index=I,
        span=ref("Span"),
        entity_type_hint=nullable(S),
        world_hint=enum("real", "fictional", "symbolic", "depicted", "unknown"),
        resolution=enum("resolved", "ambiguous", "unresolved"),
        entity_id=nullable(S),
        candidate_entity_ids=array(S),
        reasons=array(S),
        asserted={"const": False},
    )
)
D["IdentityLink"] = obj(
    dict(
        link_id=S,
        scope=ref("Scope"),
        relation=enum("alias_of", "same_as", "distinct_from"),
        left=ref("EntityRef"),
        right=ref("EntityRef"),
        evidence_ids=array(S, 1),
        status=enum("proposed", "accepted", "contested", "invalidated"),
        producer=ref("Producer"),
        known_since=TS,
    )
)
D["AwareTimeValue"] = obj(dict(at=TS, end_exclusive=nullable(TS)))
D["Anchor"] = obj(
    dict(
        kind=enum("source_clock", "event_boundary"),
        source=nullable(ref("SourceSnapshot")),
        event=nullable(ref("Ref")),
        boundary=nullable(enum("start", "end")),
        evidence_ids=array(S, 1),
    )
)
D["TemporalRelation"] = obj(
    dict(
        kind=enum("before", "after", "during", "since", "until"),
        target=ref("Ref"),
        offset=nullable(ref("Duration")),
        evidence_ids=array(S, 1),
    )
)
D["Duration"] = obj(
    dict(
        minimum={"type": "number", "minimum": 0},
        maximum=nullable({"type": "number", "minimum": 0}),
        unit=enum("second", "minute", "hour", "day", "week", "month", "year"),
        convention=enum("elapsed", "inclusive_calendar", "calendar_difference", "unspecified"),
        approximate=B,
    )
)
D["TemporalRef"] = obj(
    dict(
        temporal_id=S,
        text=nullable(S),
        resolution=enum("resolved", "ambiguous", "unresolved"),
        value=nullable(ref("AwareTimeValue")),
        precision=enum(
            "instant",
            "minute",
            "hour",
            "day",
            "week",
            "month",
            "season",
            "year",
            "decade",
            "unknown",
        ),
        text_precision=enum(
            "instant",
            "minute",
            "hour",
            "day",
            "week",
            "month",
            "season",
            "year",
            "decade",
            "unknown",
        ),
        approximate=B,
        basis=enum("explicit", "source_time", "relation", "none"),
        timezone=nullable(S),
        anchor=nullable(ref("Anchor")),
        relation=nullable(ref("TemporalRelation")),
        evidence_ids=array(S),
    )
)
D["Interval"] = obj(
    dict(
        start=nullable(ref("TemporalRef")),
        end=nullable(ref("TemporalRef")),
        end_semantics=enum("exclusive", "inclusive", "unknown"),
        start_status=enum("known", "unknown", "unbounded"),
        end_status=enum("known", "unknown", "unbounded"),
    )
)
D["TimeAxes"] = obj(
    dict(
        valid=nullable(ref("Interval")),
        occurred=nullable(ref("TemporalRef")),
        source_clock=ref("SourceClock"),
        recorded_at=TS,
        known_since=TS,
        known_seq=I,
    )
)
D["Value"] = {
    "oneOf": [
        obj(dict(kind={"const": "entity"}, ref=ref("EntityRef"))),
        obj(dict(kind={"const": "text"}, text=S, language=nullable(S))),
        obj(
            dict(
                kind={"const": "number"},
                amount={"type": "number"},
                unit=nullable(S),
                currency=nullable(S),
            )
        ),
        obj(dict(kind={"const": "boolean"}, value=B)),
        obj(dict(kind={"const": "temporal"}, ref=ref("TemporalRef"))),
    ]
}
D["Assessment"] = obj(
    dict(
        source_trust={"type": "number", "minimum": 0, "maximum": 1},
        modality=enum("asserted", "negated", "hypothetical", "planned", "reported", "question"),
        negated=B,
        binding=enum("bound", "partly", "unbound"),
        support_families=array(S),
        restated=I,
        grounding=enum("grounded", "uncertain", "rejected"),
    )
)
D["ClaimLink"] = obj(
    dict(
        relation=enum("corrects", "supersedes", "contradicts", "equivalent_to", "derived_from"),
        target=ref("Ref"),
    )
)
D["Claim"] = obj(
    dict(
        claim_id=S,
        revision=V,
        scope=ref("Scope"),
        subject=ref("EntityRef"),
        predicate=S,
        attribute_key=nullable(S),
        object=ref("Value"),
        event_id=nullable(S),
        asserted_by=S,
        reported_by=nullable(S),
        source_class=enum(
            "user_stated",
            "assistant_proposed",
            "user_confirmed",
            "tool",
            "document",
            "external",
            "derived",
        ),
        text=S,
        time=ref("TimeAxes"),
        assessment=ref("Assessment"),
        status=enum("active", "tentative", "contested", "superseded", "hidden"),
        evidence=array(ref("Evidence"), 1),
        links=array(ref("ClaimLink")),
        producer=ref("Producer"),
        effect_id=S,
    )
)
D["Participant"] = obj(dict(role=S, entity=ref("EntityRef"), evidence_ids=array(S, 1)))
D["EventOccurrence"] = obj(
    dict(
        event_id=S,
        revision=V,
        scope=ref("Scope"),
        event_type=S,
        participants=array(ref("Participant"), 1),
        stage=enum(
            "proposed", "planned", "started", "ongoing", "completed", "cancelled", "unknown"
        ),
        participation_mode=enum("physical", "remote", "mixed", "unknown"),
        time=ref("TimeAxes"),
        claim_refs=array(ref("Ref"), 1),
        same_event_links=array(ref("Ref")),
        identity_status=enum("resolved", "provisional", "contested"),
        producer=ref("Producer"),
    )
)
D["PredicateSpec"] = obj(
    dict(
        name=S,
        version=V,
        subject_types=array(S, 1),
        value_kinds=array(S, 1),
        semantics=enum("state", "event", "relation", "attribute"),
        cardinality=enum("one", "many", "unknown"),
        closed_world={"const": False},
        transition_policy=enum("explicit_only", "append_only"),
        inference_rule_ids=array(S),
        allowed_source_origins=array(S, 1),
    )
)
D["CandidateSearch"] = obj(
    dict(
        scope=ref("Scope"),
        as_of_known=nullable(TS),
        as_of_seq=nullable(I),
        methods=array(
            enum(
                "exact", "alias", "reply", "bm25", "embedding", "reranker", "temporal", "catalogue"
            ),
            1,
        ),
        limit=V,
        covered_all_relevant_scope=B,
        continuation=nullable(S),
    )
)
D["Candidate"] = obj(
    dict(
        candidate_id=S,
        target=ref("Ref"),
        target_kind=enum("entity", "claim", "event", "source", "catalogue"),
        retrieval_method=S,
        relevance_score=nullable({"type": "number"}),
        evidence_refs=array(S),
        reasons=array(S),
    )
)
D["Unresolved"] = obj(
    dict(
        annotation_id=S,
        revision=V,
        scope=ref("Scope"),
        source=ref("SourceSnapshot"),
        mention_ids=array(S),
        reason=enum(
            "ambiguous_entity",
            "ambiguous_place",
            "missing_anchor",
            "missing_support",
            "unsupported_form",
            "unsupported_language",
            "conflict",
            "budget",
            "incomplete_search",
            "missing_media",
        ),
        candidates=array(ref("Candidate")),
        search=ref("CandidateSearch"),
        retry_eligible=B,
        retry_on=array(
            enum(
                "new_evidence", "source_revision", "rule_revision", "catalogue_revision", "capacity"
            )
        ),
        producer=ref("Producer"),
        known_since=TS,
    )
)
D["Dependency"] = obj(
    dict(
        dependency_id=S,
        kind=enum(
            "support",
            "entity_binding",
            "time_anchor",
            "polarity",
            "modality",
            "list_member",
            "join_branch",
            "duration_endpoint",
        ),
        evidence_ids=array(S, 1),
        claim_refs=array(ref("Ref")),
        mandatory=B,
        branch_id=nullable(S),
    )
)
D["Coverage"] = obj(
    dict(
        kind=enum("complete_scope", "observed_subset", "unknown"),
        requested_scope=S,
        enumerated_scope=nullable(S),
        scope_snapshot_hash=nullable(HASH),
        continuation=nullable(S),
        reasons=array(S),
        proof_evidence_ids=array(S),
    )
)
D["ExcerptCopy"] = obj(
    dict(
        copy_id=S,
        evidence_id=S,
        source=ref("Ref"),
        excerpt_hash=HASH,
        erasure_generation=I,
        child_copy_ids=array(S),
    )
)
D["Bundle"] = obj(
    dict(
        bundle_id=S,
        scope=ref("Scope"),
        claims=array(ref("Ref"), 1),
        evidence=array(ref("Evidence"), 1),
        copy_manifest=array(ref("ExcerptCopy"), 1),
        dependencies=array(ref("Dependency"), 1),
        temporal_refs=array(ref("TemporalRef")),
        coverage=ref("Coverage"),
        cost=ref("Cost"),
        producer=ref("Producer"),
    )
)
D["Cost"] = obj(
    dict(
        value=I,
        unit=enum("tokens", "characters"),
        method=enum("exact_tokenizer", "estimated", "characters"),
        tokenizer_id=nullable(S),
    )
)
D["Selection"] = obj(
    dict(
        kept_bundle_ids=array(S),
        dropped=array(ref("Drop")),
        coverage=ref("Coverage"),
        used=ref("Cost"),
        budget=ref("Cost"),
        outcome=enum("complete", "partial", "unresolved"),
        policy_version=S,
        warnings=array(S),
    )
)
D["Drop"] = obj(
    dict(
        bundle_id=S,
        reason=enum(
            "over_budget", "duplicate", "scope", "stale", "low_relevance", "missing_dependency"
        ),
    )
)
D["Computation"] = obj(
    dict(
        computation_id=S,
        operator=enum("count", "union", "intersection", "duration"),
        input_claim_refs=array(ref("Ref"), 1),
        event_ids=array(S),
        coverage=ref("Coverage"),
        result=nullable(ref("Value")),
        result_kind=enum("exact", "lower_bound", "interval", "unresolved"),
        duration=nullable(ref("Duration")),
        producer=ref("Producer"),
    )
)
D["Invalidation"] = obj(
    dict(
        target=ref("Ref"),
        reason=enum("erasure", "source_revision", "access_revoked", "rule_revision"),
        source_refs=array(ref("Ref"), 1),
    )
)
D["Effect"] = obj(
    dict(
        effect_id=S,
        kind=enum(
            "entity_recorded",
            "mention_recorded",
            "identity_link_recorded",
            "claim_recorded",
            "claim_revised",
            "event_recorded",
            "unresolved_recorded",
            "annotation_invalidated",
        ),
        target=ref("Ref"),
        expected_target_revision=nullable(V),
        source_snapshot_refs=array(ref("SourceSnapshot"), 1),
        lease_epoch=V,
        expected_erasure_generation=I,
        producer=ref("Producer"),
        idempotency_key=S,
        committed_at=TS,
        committed_seq=I,
        payload_hash=HASH,
        payload={
            "oneOf": [
                ref(k)
                for k in (
                    "Entity",
                    "Mention",
                    "IdentityLink",
                    "Claim",
                    "EventOccurrence",
                    "Unresolved",
                    "Invalidation",
                )
            ]
        },
    )
)
D["WorkBudget"] = obj(
    dict(
        max_candidates=V,
        max_source_characters=V,
        max_jobs_per_batch=V,
        max_dependency_hops=I,
        model_calls_allowed=B,
        generative_calls_allowed=B,
    )
)
D["ContextBundle"] = obj(
    dict(
        context_id=S,
        scope=ref("Scope"),
        source_snapshots=array(ref("SourceSnapshot"), 1),
        candidates=array(ref("Candidate")),
        evidence=array(ref("Evidence")),
        search=ref("CandidateSearch"),
        budget=ref("WorkBudget"),
        policy_version=S,
        producer=ref("Producer"),
        snapshot_hash=HASH,
    )
)
D["DerivationResult"] = obj(
    dict(
        operation_id=S,
        outcome=enum("complete", "partial", "unresolved", "rejected", "retryable"),
        effect_refs=array(ref("Ref")),
        unresolved_refs=array(ref("Ref")),
        reasons=array(S),
        continuation=nullable(S),
        policy_version=S,
    )
)

D["EntityRef"]["anyOf"] = [{"properties": {key: S}} for key in ("entity_id", "mention_id")]
D["Mention"]["allOf"] = [
    {
        "if": {"properties": {"resolution": {"const": "resolved"}}},
        "then": {"properties": {"entity_id": S}},
        "else": {"properties": {"entity_id": {"type": "null"}}},
    },
]
D["TemporalRef"]["allOf"] = [
    {
        "if": {"properties": {"resolution": {"const": "resolved"}}},
        "then": {
            "properties": {"value": {"type": "object"}, "precision": {"not": {"const": "unknown"}}}
        },
        "else": {"properties": {"value": {"type": "null"}, "precision": {"const": "unknown"}}},
    },
    {
        "if": {"properties": {"basis": {"const": "none"}}},
        "then": {"properties": {k: {"type": "null"} for k in ("value", "anchor", "relation")}},
    },
    {
        "if": {"properties": {"basis": {"const": "explicit"}}},
        "then": {"properties": {"value": {"type": "object"}, "anchor": {"type": "null"}}},
    },
    {
        "if": {"properties": {"basis": {"const": "source_time"}}},
        "then": {"properties": {"value": {"type": "object"}, "anchor": {"type": "object"}}},
    },
    {
        "if": {"properties": {"basis": {"const": "relation"}}},
        "then": {"properties": {"relation": {"type": "object"}}},
    },
]
D["Assessment"]["allOf"] = [
    {
        "if": {"properties": {"negated": {"const": True}}},
        "then": {"properties": {"modality": {"not": {"enum": ["asserted", "negated"]}}}},
    },
]
D["Claim"]["allOf"] = [
    {
        "if": {"properties": {"predicate": {"const": "has_attribute"}}},
        "then": {"properties": {"attribute_key": S}},
        "else": {"properties": {"attribute_key": {"type": "null"}}},
    },
]
D["Coverage"]["allOf"] = [
    {
        "if": {"properties": {"kind": {"const": "complete_scope"}}},
        "then": {
            "properties": {
                "enumerated_scope": S,
                "scope_snapshot_hash": HASH,
                "continuation": {"type": "null"},
            }
        },
    },
]
D["Computation"]["allOf"] = [
    {
        "if": {"properties": {"result_kind": {"const": "unresolved"}}},
        "then": {"properties": {"result": {"type": "null"}}},
        "else": {"properties": {"result": {"type": "object"}}},
    },
]

SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://memspine.invalid/review/enrichment-contracts-v1",
    "title": "Proposed enrichment contracts — review artifacts, not runtime schema",
    "$defs": D,
    "type": "object",
    "properties": {"kind": enum(*D), "value": {}},
    "required": ["kind", "value"],
    "additionalProperties": False,
    "allOf": [
        {"if": {"properties": {"kind": {"const": k}}}, "then": {"properties": {"value": ref(k)}}}
        for k in D
    ],
}


def examples():
    scope = dict(tenant="tenant-demo", owner="owner-demo", owner_kind="user", namespace="personal")
    clock = dict(
        created_at="2026-10-18T10:00:00+05:30",
        original_text="2026-10-18T10:00:00+05:30",
        timezone="Asia/Kolkata",
        timestamp_origin="provided",
        timezone_origin="provided",
    )
    text = "I moved from Porto to Lisbon last month."
    import hashlib

    source = dict(
        source=dict(id="raw-001", version=1),
        scope=scope,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        message_id="msg-001",
        session_id="S2",
        conversation_id="C1",
        sender_id="user-123",
        clock=clock,
        erasure_generation=0,
        available_at="2026-10-18T04:30:01Z",
        available_seq=100,
    )
    evidence = dict(
        evidence_id="ev-001",
        role="support",
        origin="raw_message",
        source=source,
        seg_index=0,
        span=dict(start=0, end=len(text), quote=text),
        source_family="principal:user-123",
    )
    producer = dict(
        kind="rule", name="explicit-move", version="1", settings_hash="0" * 64, model_id=None
    )
    temporal = dict(
        temporal_id="time-sep",
        text="last month",
        resolution="resolved",
        value=dict(at="2026-09-01T00:00:00+05:30", end_exclusive="2026-10-01T00:00:00+05:30"),
        precision="month",
        text_precision="month",
        approximate=False,
        basis="source_time",
        timezone="Asia/Kolkata",
        anchor=dict(
            kind="source_clock", source=source, event=None, boundary=None, evidence_ids=["ev-001"]
        ),
        relation=None,
        evidence_ids=["ev-001"],
    )
    # Event interval endpoints are not invented from the month reference.
    axes = dict(
        valid=None,
        occurred=temporal,
        source_clock=clock,
        recorded_at="2026-10-18T04:30:02Z",
        known_since="2026-10-18T04:30:02Z",
        known_seq=101,
    )
    entity = dict(
        entity_id="person-123",
        revision=1,
        scope=scope,
        type="person",
        world="real",
        label="Hembad",
        identity_basis="stable_id",
        identity_evidence_ids=[],
        status="active",
        known_since=source["available_at"],
        known_seq=100,
    )
    claim = dict(
        claim_id="claim-001",
        revision=1,
        scope=scope,
        subject=dict(entity_id="person-123", mention_id=None),
        predicate="move_destination",
        attribute_key=None,
        object=dict(kind="entity", ref=dict(entity_id="place-lisbon", mention_id=None)),
        event_id="move-001",
        asserted_by="user-123",
        reported_by=None,
        source_class="user_stated",
        text="The stated move destination is Lisbon.",
        time=axes,
        assessment=dict(
            source_trust=1.0,
            modality="asserted",
            negated=False,
            binding="bound",
            support_families=["principal:user-123"],
            restated=0,
            grounding="grounded",
        ),
        status="active",
        evidence=[evidence],
        links=[],
        producer=producer,
        effect_id="effect-001",
    )
    event = dict(
        event_id="move-001",
        revision=1,
        scope=scope,
        event_type="move",
        participants=[
            dict(
                role="mover",
                entity=dict(entity_id="person-123", mention_id=None),
                evidence_ids=["ev-001"],
            )
        ],
        stage="completed",
        participation_mode="physical",
        time=axes,
        claim_refs=[dict(id="claim-001", version=1)],
        same_event_links=[],
        identity_status="resolved",
        producer=producer,
    )
    coverage = dict(
        kind="observed_subset",
        requested_scope="all moves mentioned by this owner",
        enumerated_scope="this source only",
        scope_snapshot_hash=None,
        continuation=None,
        reasons=["No exhaustive lifetime evidence"],
        proof_evidence_ids=[],
    )
    cost = dict(value=60, unit="tokens", method="estimated", tokenizer_id=None)
    bundle = dict(
        bundle_id="bundle-001",
        scope=scope,
        claims=[dict(id="claim-001", version=1)],
        evidence=[evidence],
        copy_manifest=[
            dict(
                copy_id="copy-001",
                evidence_id="ev-001",
                source=dict(id="raw-001", version=1),
                excerpt_hash=hashlib.sha256(text.encode()).hexdigest(),
                erasure_generation=0,
                child_copy_ids=[],
            )
        ],
        dependencies=[
            dict(
                dependency_id="dep-001",
                kind="support",
                evidence_ids=["ev-001"],
                claim_refs=[dict(id="claim-001", version=1)],
                mandatory=True,
                branch_id=None,
            ),
            dict(
                dependency_id="dep-002",
                kind="time_anchor",
                evidence_ids=["ev-001"],
                claim_refs=[],
                mandatory=True,
                branch_id=None,
            ),
        ],
        temporal_refs=[temporal],
        coverage=coverage,
        cost=cost,
        producer=producer,
    )
    mention = dict(
        mention_id="mention-cambridge",
        source=source,
        seg_index=0,
        span=dict(start=0, end=len(text), quote=text),
        entity_type_hint="place",
        world_hint="real",
        resolution="ambiguous",
        entity_id=None,
        candidate_entity_ids=["place-cambridge-uk", "place-cambridge-us"],
        reasons=["Illustration only: candidate list does not establish location"],
        asserted=False,
    )
    # Separate source for ambiguous mention to keep span semantics genuine.
    amb_text = "I moved to Cambridge."
    amb_source = dict(
        source,
        source=dict(id="raw-002", version=1),
        message_id="msg-002",
        content_hash=hashlib.sha256(amb_text.encode()).hexdigest(),
    )
    mention.update(source=amb_source, span=dict(start=11, end=20, quote="Cambridge"))
    rows = [
        dict(kind=k, value=v)
        for k, v in [
            ("Entity", entity),
            ("Evidence", evidence),
            ("TemporalRef", temporal),
            ("Claim", claim),
            ("EventOccurrence", event),
            ("Bundle", bundle),
            ("Mention", mention),
        ]
    ]
    yield from rows
    import copy

    unresolved_time = copy.deepcopy(temporal)
    missing_text = "I moved last month."
    ms = copy.deepcopy(source)
    ms.update(
        source=dict(id="raw-missing", version=1),
        message_id="msg-missing",
        content_hash=hashlib.sha256(missing_text.encode()).hexdigest(),
    )
    ms["clock"]["timestamp_origin"] = "generated"
    ms["clock"]["original_text"] = None
    me = copy.deepcopy(evidence)
    me.update(
        evidence_id="ev-missing",
        source=ms,
        span=dict(start=0, end=len(missing_text), quote=missing_text),
    )
    yield dict(kind="Evidence", value=me)
    unresolved_time.update(
        temporal_id="time-unknown",
        value=None,
        resolution="unresolved",
        precision="unknown",
        basis="none",
        anchor=None,
        timezone=None,
        evidence_ids=["ev-missing"],
    )
    yield dict(kind="TemporalRef", value=unresolved_time)
    civil_time = copy.deepcopy(temporal)
    civil_text = "I visited on 7 May 2023."
    cvs = copy.deepcopy(source)
    cvs.update(
        source=dict(id="raw-civil", version=1),
        message_id="msg-civil",
        content_hash=hashlib.sha256(civil_text.encode()).hexdigest(),
    )
    cvs["clock"] = dict(
        created_at=None,
        original_text=None,
        timezone=None,
        timestamp_origin="unknown",
        timezone_origin="unknown",
    )
    cve = copy.deepcopy(evidence)
    cve.update(
        evidence_id="ev-civil",
        source=cvs,
        span=dict(start=0, end=len(civil_text), quote=civil_text),
    )
    yield dict(kind="Evidence", value=cve)
    civil_time.update(
        temporal_id="civil-date",
        text="7 May 2023",
        value=None,
        resolution="unresolved",
        precision="unknown",
        text_precision="day",
        basis="none",
        timezone=None,
        anchor=None,
        evidence_ids=["ev-civil"],
    )
    yield dict(kind="TemporalRef", value=civil_time)
    planned = copy.deepcopy(claim)
    planned.update(
        claim_id="claim-plan",
        predicate="intends",
        object=dict(kind="text", text="move to Lisbon", language="en"),
        event_id=None,
        text="The source proposes a future move.",
        status="tentative",
        effect_id="effect-plan",
    )
    planned["assessment"].update(modality="planned")
    # Illustrative structural fragment: evidence must be replaced for a real future-plan statement.
    plan_text = "I plan to move to Lisbon."
    ps = copy.deepcopy(source)
    ps.update(
        source=dict(id="raw-plan", version=1),
        message_id="msg-plan",
        content_hash=hashlib.sha256(plan_text.encode()).hexdigest(),
    )
    pe = copy.deepcopy(evidence)
    pe.update(
        evidence_id="ev-plan", source=ps, span=dict(start=0, end=len(plan_text), quote=plan_text)
    )
    planned["evidence"] = [pe]
    planned["time"]["occurred"] = None
    yield dict(kind="Claim", value=planned)
    corrected = copy.deepcopy(claim)
    correction_text = "Correction: I meant Coimbra, not Lisbon."
    cs = copy.deepcopy(source)
    cs.update(
        source=dict(id="raw-correction", version=1),
        message_id="msg-correction",
        content_hash=hashlib.sha256(correction_text.encode()).hexdigest(),
    )
    ce = copy.deepcopy(evidence)
    ce.update(
        evidence_id="ev-correction",
        source=cs,
        span=dict(start=0, end=len(correction_text), quote=correction_text),
    )
    binding = copy.deepcopy(evidence)
    binding["role"] = "binding"
    corrected.update(
        claim_id="claim-correction",
        object=dict(kind="entity", ref=dict(entity_id="place-coimbra", mention_id=None)),
        text="The corrected stated move destination is Coimbra.",
        evidence=[ce, binding],
        links=[dict(relation="corrects", target=dict(id="claim-001", version=1))],
        effect_id="effect-correction",
    )
    yield dict(kind="Claim", value=corrected)
    search = dict(
        scope=scope,
        as_of_known=None,
        as_of_seq=None,
        methods=["exact", "bm25"],
        limit=20,
        covered_all_relevant_scope=False,
        continuation="opaque-cursor",
    )
    unresolved = dict(
        annotation_id="unresolved-cambridge",
        revision=1,
        scope=scope,
        source=amb_source,
        mention_ids=["mention-cambridge"],
        reason="ambiguous_place",
        candidates=[],
        search=search,
        retry_eligible=True,
        retry_on=["new_evidence"],
        producer=producer,
        known_since=source["available_at"],
    )
    yield dict(kind="Unresolved", value=unresolved)
    computation = dict(
        computation_id="count-demo",
        operator="count",
        input_claim_refs=[dict(id="claim-001", version=1)],
        event_ids=["move-001"],
        coverage=coverage,
        result=dict(kind="number", amount=1, unit="event", currency=None),
        result_kind="lower_bound",
        duration=None,
        producer=producer,
    )
    yield dict(kind="Computation", value=computation)


GROUPS = [
    (
        [1, 2, 3, 4, 5, 49, 50, 51, 52],
        ["Entity", "Mention", "IdentityLink", "Claim", "EventOccurrence"],
        ["C02", "C03", "C06", "C09"],
    ),
    ([6, 35], ["Evidence", "SourceSnapshot", "ContextBundle"], ["C01", "C04", "C05"]),
    (
        [7, 8, 11, 12, 13, 14, 15, 16, 17],
        ["TemporalRef", "TimeAxes", "EventOccurrence", "Duration"],
        ["C07", "C08", "C09", "C10"],
    ),
    (
        [9, 10, 18, 19, 20, 21, 22],
        ["Claim", "PredicateSpec", "Assessment", "IdentityLink"],
        ["C03", "C06", "C11", "C12"],
    ),
    ([23, 24, 25], ["Computation", "Coverage", "EventOccurrence", "Bundle"], ["C09", "C13", "C14"]),
    (
        [26, 27, 28, 29, 30, 31, 32, 33, 34],
        ["CandidateSearch", "Candidate", "Bundle", "Selection", "Dependency"],
        ["C02", "C05", "C14", "C15", "C16"],
    ),
    ([36, 37, 38], ["Value", "Unresolved", "Evidence", "Producer"], ["C04", "C06", "C17"]),
    (
        [39, 40],
        ["Effect", "SourceSnapshot", "ExcerptCopy", "Bundle", "DerivationResult", "WorkBudget"],
        ["C01", "C02", "C18", "C19"],
    ),
    ([41, 42, 43, 44], ["Evidence", "Unresolved", "Coverage"], ["C20"]),
    ([45, 46, 47, 48], ["Producer", "Selection", "Coverage", "Effect"], ["C21", "C22"]),
]


if __name__ == "__main__":
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / "enrichment.schema.json").write_text(
        json.dumps(SCHEMA, indent=2) + "\n", encoding="utf-8"
    )
    example_rows = list(examples())
    (ROOT / "examples.json").write_text(json.dumps(example_rows, indent=2) + "\n", encoding="utf-8")
    mapping = [
        dict(
            gap_id=f"NL-{number:02d}",
            schemas=names,
            contract_checks=checks,
            status="proposed; schema shape plus behavioural validation required",
        )
        for numbers, names, checks in GROUPS
        for number in numbers
    ]
    mapping.sort(key=lambda x: x["gap_id"])
    assert len(mapping) == len({r["gap_id"] for r in mapping}) == 52
    (ROOT / "gap_coverage.json").write_text(json.dumps(mapping, indent=2) + "\n", encoding="utf-8")
    table = [
        "# Requirement-to-contract matrix",
        "",
        "Proposed design coverage, not implementation/test completion. See [contracts](../ENRICHMENT_SCHEMA_CONTRACTS.md).",
        "",
        "| Gap | Schema definitions | Behavioural checks |",
        "|---|---|---|",
    ]
    table += [
        "| "
        + r["gap_id"]
        + " | "
        + ", ".join(r["schemas"])
        + " | "
        + ", ".join(r["contract_checks"])
        + " |"
        for r in mapping
    ]
    (ROOT / "gap_coverage.md").write_text("\n".join(table) + "\n", encoding="utf-8")
    print(
        f"Generated {len(D)} proposed definitions, {len(example_rows)} examples and 52 requirement mappings."
    )
