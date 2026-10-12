# ruff: noqa: E501
"""Public tier: receipts, readiness, handles, errors, read surface, cost, plug-in manifest, cache key."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from memspine.schemas import collect_models
from memspine.schemas.durable import AssertionEnvelope, Evidence, OwnerScope
from memspine.schemas.public import (
    CacheKey,
    CapabilityManifest,
    ContextBlock,
    CostRecord,
    DuplicateSlotError,
    EvidenceBundle,
    JobHandle,
    LifetimeViolation,
    QuotaExceeded,
    QuotaExceededError,
    Readiness,
    ReadRequest,
    ReadResult,
    Receipt,
    ScoredItem,
    WaitResult,
    check_enabled,
    check_lifetime,
    normalise_query,
)

SCOPE = OwnerScope(tenant="t", owner="o")
FIXTURES = Path(__file__).parent / "fixtures" / "durable"


def _ready(**over):
    base = {
        "raw_search": "ready",
        "facts": "pending",
        "graph": "unavailable",
        "summaries": "pending",
        "profile": "pending",
    }
    base.update(over)
    return Readiness(**base)


def test_every_public_model_declares_a_semver():
    for cls in collect_models()["public"]:
        parts = cls.__semver__.split(".")
        assert len(parts) == 3 and all(p.isdigit() for p in parts), cls.__name__
        assert cls.__semver__ != "0.0.0"


# --- receipt, wait, handle ---------------------------------------------------------------------------------


def test_receipt_reports_durability_and_readiness_per_capability():
    r = Receipt(
        durability="durable",
        event_ids=["e"],
        record_ids=["r"],
        intent_id="i",
        source_id="s",
        source_version=1,
        accepted_seq=3,
        readiness=_ready(),
    )
    dumped = r.model_dump(mode="json")
    assert set(dumped["readiness"]) == {"raw_search", "facts", "graph", "summaries", "profile"}
    assert Receipt.model_validate(dumped) == r
    assert Receipt(**{**r.model_dump(), "durability": "memory"}).durability == "memory"
    with pytest.raises(ValidationError):
        Receipt(**{**r.model_dump(), "durability": "maybe"})
    with pytest.raises(ValidationError):
        Readiness(
            raw_search="ready", facts="done", graph="ready", summaries="ready", profile="ready"
        )
    with pytest.raises(ValidationError):  # a capability may not be omitted
        Readiness(raw_search="ready")


def test_readiness_helpers():
    ready = _ready()
    assert ready.get("graph") == "unavailable"
    assert ready.pending() == ["facts", "summaries", "profile"]
    assert ready.pending(["raw_search", "facts"]) == ["facts"]
    with pytest.raises(KeyError):
        ready.get("vectors")


def test_wait_result_timeout_consistency():
    ok = WaitResult(
        requested=["raw_search", "graph"], readiness=_ready(), timed_out=False, waited_ms=5
    )
    assert ok.missing == []
    timed = WaitResult(requested=["facts"], readiness=_ready(), timed_out=True, waited_ms=1000)
    assert timed.missing == ["facts"]
    with pytest.raises(ValidationError, match="still pending"):
        WaitResult(requested=["facts"], readiness=_ready(), timed_out=False, waited_ms=1)
    with pytest.raises(ValidationError, match="settled"):
        WaitResult(requested=["raw_search"], readiness=_ready(), timed_out=True, waited_ms=1)
    with pytest.raises(ValidationError):
        WaitResult(requested=[], readiness=_ready(), timed_out=False, waited_ms=0)
    with pytest.raises(ValidationError, match="duplicate"):
        WaitResult(
            requested=["raw_search", "raw_search"], readiness=_ready(), timed_out=False, waited_ms=0
        )


def test_job_handle_states():
    for state in ("queued", "running", "succeeded", "cancelled"):
        assert JobHandle(job_id="j", kind="sleep", state=state)
    assert JobHandle(job_id="j2", kind="sleep", state="coalesced", coalesced_into="j").terminal
    assert not JobHandle(job_id="j", kind="sleep", state="running").terminal
    with pytest.raises(ValidationError, match="coalesced_into"):
        JobHandle(job_id="j", kind="sleep", state="coalesced")
    with pytest.raises(ValidationError, match="coalesced_into"):
        JobHandle(job_id="j", kind="sleep", state="queued", coalesced_into="x")
    with pytest.raises(ValidationError, match="failed job"):
        JobHandle(job_id="j", kind="sleep", state="running", error="boom")
    with pytest.raises(ValidationError):
        JobHandle(job_id="j", kind="sleep", state="paused")


# --- quota, cost -------------------------------------------------------------------------------------------


def test_quota_exceeded_payload():
    q = QuotaExceeded(quota="queue_depth", limit=100, current=100, retry_after=2.5)
    assert q.model_dump(mode="json")["error"] == "quota_exceeded"
    assert "100" in str(QuotaExceededError(q)) and QuotaExceededError(q).payload is q
    with pytest.raises(ValidationError):
        QuotaExceeded(quota="memory", limit=1, current=1)
    with pytest.raises(ValidationError):
        QuotaExceeded(quota="rate", limit=-1, current=0)


def test_cost_record_classes():
    assert CostRecord(
        cost_class="generative", request_id="r", model_id="m", tokens_in=10, tokens_out=5
    )
    assert CostRecord(cost_class="embedding", job_id="j", model_id="e", tokens_in=10)
    assert CostRecord(cost_class="tool", job_id="j", tool_name="fetch")
    with pytest.raises(ValidationError, match="request or a job"):
        CostRecord(cost_class="rerank", model_id="m")
    with pytest.raises(ValidationError, match="output tokens"):
        CostRecord(cost_class="rerank", request_id="r", model_id="m", tokens_out=1)
    with pytest.raises(ValidationError, match="names the tool"):
        CostRecord(cost_class="tool", request_id="r")
    with pytest.raises(ValidationError, match="names the model"):
        CostRecord(cost_class="generative", request_id="r")
    with pytest.raises(ValidationError):
        CostRecord(cost_class="vector", request_id="r", model_id="m")


# --- read surface ------------------------------------------------------------------------------------------


def test_read_request_viewer_rules():
    assert ReadRequest(query="q", scope=SCOPE)  # single party: no viewer needed
    assert ReadRequest(query="q", scope=SCOPE, multi_party=True, principal="p", viewer="p")
    with pytest.raises(ValidationError, match="requires a viewer"):
        ReadRequest(query="q", scope=SCOPE, multi_party=True)
    with pytest.raises(ValidationError, match="requires a viewer"):
        ReadRequest(query="q", scope=SCOPE, multi_party=True, principal="p")
    with pytest.raises(ValidationError, match="authenticated principal"):
        ReadRequest(query="q", scope=SCOPE, viewer="p")
    with pytest.raises(ValidationError, match="must be the authenticated principal"):
        ReadRequest(query="q", scope=SCOPE, principal="p", viewer="someone-else")
    with pytest.raises(ValidationError):
        ReadRequest(query="q", scope=SCOPE, budget_tokens=0)
    with pytest.raises(ValidationError):
        ReadRequest(query="q", scope=SCOPE, as_of_valid=datetime(2026, 1, 1))  # naive


def _env():
    return AssertionEnvelope.model_validate_json(
        (FIXTURES / "assertion_envelope.v1.json").read_text(encoding="utf-8")
    )


def test_evidence_bundle_is_whole_or_not_at_all():
    env = _env()
    support = [e for e in env.evidence if e.role == "support"]
    binding = [e for e in env.evidence if e.role == "binding"]
    assert support and binding
    assert EvidenceBundle(record_id="rec-1", assertion=env, support=support, binding=binding)
    with pytest.raises(ValidationError, match="all binding evidence"):
        EvidenceBundle(record_id="rec-1", assertion=env, support=support, binding=[])
    with pytest.raises(ValidationError, match="all support evidence"):
        EvidenceBundle(record_id="rec-1", assertion=env, support=[], binding=binding)
    with pytest.raises(ValidationError, match="support evidence only"):
        EvidenceBundle(record_id="rec-1", assertion=env, support=binding, binding=binding)
    ctx = Evidence(role="context", source_family="f")
    with pytest.raises(ValidationError, match="support evidence only"):
        EvidenceBundle(record_id="rec-1", assertion=env, support=[ctx], binding=binding)


def test_context_block_accounting_and_cap():
    assert ContextBlock(kind="raw", tokens=10, cap=20)
    assert ContextBlock(kind="raw", tokens=10, accounting="exact", tokenizer="reader-tok")
    with pytest.raises(ValidationError, match="names the tokenizer"):
        ContextBlock(kind="raw", accounting="exact")
    with pytest.raises(ValidationError, match="exceeds its cap"):
        ContextBlock(kind="raw", tokens=30, cap=20)
    assert ContextBlock(kind="raw").accounting == "estimated"


def test_unscored_items_are_explicit_never_zero():
    assert ScoredItem(record_id="r", score=0.0).scored  # a real zero is a real score
    unscored = ScoredItem(record_id="r", scored=False)
    assert unscored.score is None
    with pytest.raises(ValidationError, match="never 0"):
        ScoredItem(record_id="r", scored=False, score=0.0)
    with pytest.raises(ValidationError, match="needs a score"):
        ScoredItem(record_id="r")


def test_read_result_partial_and_accounting():
    assert ReadResult()
    assert ReadResult(partial=True, missing_capabilities=["facts"])
    with pytest.raises(ValidationError, match="exactly when"):
        ReadResult(partial=True)
    with pytest.raises(ValidationError, match="exactly when"):
        ReadResult(missing_capabilities=["facts"])
    with pytest.raises(ValidationError, match="counted exactly"):
        ReadResult(accounting="exact", blocks=[ContextBlock(kind="raw")])
    exact = ContextBlock(kind="raw", accounting="exact", tokenizer="t")
    assert ReadResult(
        accounting="exact", blocks=[exact], items=[ScoredItem(record_id="r", scored=False)]
    )
    dumped = ReadResult(trace={"legs": {}}, contract={"answer_type": "date"}).model_dump(
        mode="json"
    )
    assert dumped["trace"] == {"legs": {}}


# --- plug-ins and cache key --------------------------------------------------------------------------------

H = "sha256:" + "a" * 64


def test_manifest_requires_pin_and_slot():
    m = CapabilityManifest(
        name="reranker-x", version="1.4.2", slot="rerank", lifetime="app", hash=H
    )
    assert m.model_dump()["hash"] == H
    assert CapabilityManifest(name="r", version="==1.4.2", slot="s", lifetime="job")
    for bad in ("*", "latest", ">=1.0", "~=1.2", "", "1.x"):
        with pytest.raises(ValidationError):
            CapabilityManifest(name="r", version=bad, slot="s", lifetime="app")
    with pytest.raises(ValidationError):
        CapabilityManifest(name="r", version="1.0", slot="s", lifetime="forever")
    with pytest.raises(ValidationError):
        CapabilityManifest(name="r", version="1.0", slot="s", lifetime="app", hash="abc")
    with pytest.raises(ValidationError):
        CapabilityManifest(name="Bad Name", version="1.0", slot="s", lifetime="app")


def test_duplicate_slot_and_lifetime_checks():
    a = CapabilityManifest(name="a", version="1.0", slot="rerank", lifetime="app")
    b = CapabilityManifest(name="b", version="1.0", slot="embed", lifetime="app")
    check_enabled([a, b])
    with pytest.raises(DuplicateSlotError, match="rerank"):
        check_enabled(
            [a, CapabilityManifest(name="c", version="1.0", slot="rerank", lifetime="job")]
        )
    with pytest.raises(DuplicateSlotError, match="twice"):
        check_enabled([a, a.model_copy(update={"slot": "other"})])
    check_lifetime("request", "app")
    check_lifetime("job", "job")
    with pytest.raises(LifetimeViolation):
        check_lifetime("app", "request")
    with pytest.raises(LifetimeViolation):
        check_lifetime("app", "job")


def _key(**over):
    base = {
        "scope": SCOPE,
        "viewer": "p",
        "query": "Where does Alice live?",
        "config_hash": "cfg1",
        "model_ids": ["m2", "m1"],
        "store_generation": 3,
        "erasure_generation": 1,
    }
    base.update(over)
    return CacheKey(**base)


def test_cache_key_normalises_and_separates_every_field():
    k = _key()
    assert k.query == "where does alice live?" == normalise_query("  Where  does\tALICE live? ")
    assert k.model_ids == ["m1", "m2"]
    assert _key(query="where does  alice live?").digest() == k.digest()
    assert _key(model_ids=["m1", "m2", "m1"]).digest() == k.digest()
    differing = [
        {"scope": OwnerScope(tenant="t", owner="other")},
        {"viewer": "q"},
        {"viewer": None},
        {"query": "something else"},
        {"config_hash": "cfg2"},
        {"model_ids": ["m1"]},
        {"store_generation": 4},
        {"erasure_generation": 2},
    ]
    digests = {k.digest()} | {_key(**d).digest() for d in differing}
    assert len(digests) == 1 + len(differing)
    with pytest.raises(ValidationError):
        _key(query="   ")
    assert json.loads(k.model_dump_json())["store_generation"] == 3
