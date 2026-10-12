"""LIB-30: source envelope intake, receipt and readiness, write ids, re-ingest."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from memspine.ingest.intake import (
    InMemoryIntakeStore,
    IntakeBook,
    IntakeConflictError,
    IntakeCost,
    IntakeError,
    IntakeLimits,
    normalise_text,
    scope_key,
    validate_envelope,
    write_id,
)
from memspine.schemas.public import QuotaExceeded
from memspine.schemas.public.receipt import CAPABILITIES
from memspine.schemas.steps.shared import AssetRef, Scope, Segment, SourceEnvelope

NOW = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
ALL = list(CAPABILITIES)
SCOPE = Scope(tenant="t1", user="ilse", agent="a1", session="s1")


def turn(i: int, text: str, role: str = "user", **kw) -> Segment:
    return Segment(seg_index=i, role=role, content=text, **kw)


def conv(texts, source_id="chat-1", scope=SCOPE, **kw) -> SourceEnvelope:
    return SourceEnvelope(
        source_id=source_id,
        kind="conversation",
        scope=scope,
        segments=[turn(i, t) for i, t in enumerate(texts)],
        **kw,
    )


def doc(chunks, version=None, source_id="doc-1") -> SourceEnvelope:
    return SourceEnvelope(
        source_id=source_id,
        kind="document",
        scope=SCOPE,
        version=version,
        segments=[turn(i, c, role="document") for i, c in enumerate(chunks)],
    )


def accept(book, env, caps=ALL, quota=None, now=NOW):
    return book.accept(env, caps, quota, now=now)


# ---------------------------------------------------------------------
# Validation and normalisation
# ---------------------------------------------------------------------
def test_normalisation():
    assert normalise_text("a\r\nb\rc\x00d\te") == "a\nb\ncd\te"
    assert normalise_text("é") == "é"
    env = validate_envelope(conv(["hi\r\nthere"]))
    assert env.segments[0].content == "hi\nthere"


@pytest.mark.parametrize(
    "mutate,fragment",
    [
        (lambda: conv([]), "at least one"),
        (lambda: conv(["ok", "  "]), "segment 1"),
        (lambda: conv(["x"], source_id="  "), "source_id"),
        (lambda: conv(["x"], scope=Scope(tenant="t1")), "tenant and a user"),
        (lambda: conv(["x"], version=2), "version above 1"),
    ],
)
def test_malformed_envelope(mutate, fragment):
    with pytest.raises(IntakeError, match=fragment):
        validate_envelope(mutate())


def test_dense_index_required():
    env = SourceEnvelope(
        source_id="s", kind="conversation", scope=SCOPE, segments=[turn(0, "a"), turn(2, "b")]
    )
    with pytest.raises(IntakeError, match="dense"):
        validate_envelope(env)


def test_role_per_kind():
    bad_doc = SourceEnvelope(source_id="d", kind="document", scope=SCOPE, segments=[turn(0, "a")])
    with pytest.raises(IntakeError, match="role 'document'"):
        validate_envelope(bad_doc)
    bad_tool = SourceEnvelope(source_id="d", kind="tool_log", scope=SCOPE, segments=[turn(0, "a")])
    with pytest.raises(IntakeError, match="role 'tool'"):
        validate_envelope(bad_tool)
    ok = SourceEnvelope(
        source_id="d", kind="tool_log", scope=SCOPE, segments=[turn(0, "a", role="tool")]
    )
    validate_envelope(ok)


def test_size_limits():
    lim = IntakeLimits(max_segments=2, max_segment_chars=5, max_envelope_chars=8)
    with pytest.raises(IntakeError, match="more than 2"):
        validate_envelope(conv(["a", "b", "c"]), lim)
    with pytest.raises(IntakeError, match="longer than 5"):
        validate_envelope(conv(["abcdef"]), lim)
    with pytest.raises(IntakeError, match="envelope longer"):
        validate_envelope(conv(["abcde", "abcde"]), lim)


def test_asset_segment_needs_reference_and_may_have_empty_text():
    ref = AssetRef(asset_id="a1", hash="h1", mime="image/png", url_or_path="x.png")
    ok = SourceEnvelope(
        source_id="img",
        kind="asset",
        scope=SCOPE,
        segments=[Segment(seg_index=0, role="user", content="", assets=[ref])],
    )
    validate_envelope(ok)
    bad = SourceEnvelope(
        source_id="img", kind="asset", scope=SCOPE, segments=[turn(0, "caption only")]
    )
    with pytest.raises(IntakeError, match="asset reference"):
        validate_envelope(bad)
    many = ok.model_copy(
        update={"segments": [Segment(seg_index=0, role="user", content="", assets=[ref] * 3)]}
    )
    with pytest.raises(IntakeError, match="too many"):
        validate_envelope(many, IntakeLimits(max_assets_per_segment=2))


def test_asset_hash_changes_write_id():
    s = scope_key(SCOPE)
    r1 = AssetRef(asset_id="a", hash="h1", mime="image/png", url_or_path="p")
    r2 = AssetRef(asset_id="a", hash="h2", mime="image/png", url_or_path="p")
    a = Segment(seg_index=0, role="user", content="", assets=[r1])
    b = Segment(seg_index=0, role="user", content="", assets=[r2])
    assert write_id(s, "x", 1, a) != write_id(s, "x", 1, b)


# ---------------------------------------------------------------------
# Write ids
# ---------------------------------------------------------------------
def test_caller_id_is_ignored():
    s = scope_key(SCOPE)
    a = Segment(seg_index=0, role="user", content="hello", caller_id="k1")
    b = Segment(seg_index=0, role="user", content="hello", caller_id="k2")
    assert write_id(s, "c", 1, a) == write_id(s, "c", 1, b)


def test_same_words_later_source_is_new_write():
    s = scope_key(SCOPE)
    seg = turn(0, "hello")
    assert write_id(s, "chat-1", 1, seg) != write_id(s, "chat-2", 1, seg)
    assert write_id(s, "chat-1", 1, seg) != write_id(s, "chat-1", 1, turn(1, "hello"))


def test_source_time_is_position_when_present():
    s = scope_key(SCOPE)
    t = datetime(2026, 1, 1, 12, tzinfo=UTC)
    t_plus = t.astimezone(timezone(timedelta(hours=9)))
    a = turn(0, "hello", source_at=t)
    b = turn(5, "hello", source_at=t_plus)  # same instant, other index and offset
    assert write_id(s, "c", 1, a) == write_id(s, "c", 1, b)


def test_session_and_agent_not_part_of_identity():
    other = Scope(tenant="t1", user="ilse", agent="a2", session="s9")
    assert scope_key(SCOPE) == scope_key(other)
    assert scope_key(SCOPE) != scope_key(Scope(tenant="t1", user="bram"))


# ---------------------------------------------------------------------
# Accept, idempotency
# ---------------------------------------------------------------------
def test_accept_conversation_emits_drafts():
    book = IntakeBook()
    out = accept(book, conv(["I moved to Delft", "Nice"]))
    assert len(out.events) == 2
    assert all(e.kind == "memory.write" and e.seq is None for e in out.events)
    assert out.events[0].payload["content"] == "I moved to Delft"
    assert out.events[0].scope.owner == "ilse"
    assert out.events[0].provenance.session_id == "s1"
    r = out.receipt
    assert r.durability == "durable"
    assert r.event_ids == [e.event_id for e in out.events]
    assert r.source_version == 1 and r.retried == []
    assert out.plan is None and not out.replayed


def test_idempotent_retry_returns_original_receipt_and_no_events():
    book = IntakeBook()
    env = conv(["a", "b", "c"])
    first = accept(book, env)
    again = accept(book, env)
    assert again.replayed and again.events == ()
    assert again.receipt == first.receipt
    assert again.receipt.intent_id == first.receipt.intent_id


def test_retry_ignores_client_keys_and_new_clock():
    book = IntakeBook()
    first = accept(book, conv(["a", "b"]))
    env2 = conv(["a", "b"])
    env2.segments[0].caller_id = "different"
    again = accept(book, env2, now=NOW + timedelta(hours=5))
    assert again.receipt == first.receipt and again.events == ()


def test_partial_retry_accepts_only_new_segments():
    book = IntakeBook()
    first = accept(book, conv(["a", "b"]))
    out = accept(book, conv(["a", "b", "c"]))
    assert not out.replayed
    assert len(out.events) == 1 and out.events[0].payload["content"] == "c"
    assert out.receipt.retried == [0, 1]
    assert out.receipt.event_ids[:2] == first.receipt.event_ids
    assert out.receipt.intent_id != first.receipt.intent_id
    # and now the larger batch is a pure replay
    third = accept(book, conv(["a", "b", "c"]))
    assert third.replayed and third.events == ()
    assert third.receipt.event_ids == out.receipt.event_ids
    assert third.receipt.retried == [0, 1, 2]


def test_repeated_identical_turns_in_one_envelope_stay_distinct():
    book = IntakeBook()
    out = accept(book, conv(["ok", "ok", "ok"]))
    assert len(set(out.receipt.event_ids)) == 3
    assert accept(book, conv(["ok", "ok", "ok"])).replayed


def test_same_words_in_later_session_are_new():
    book = IntakeBook()
    accept(book, conv(["ok"], source_id="chat-1"))
    out = accept(book, conv(["ok"], source_id="chat-2"))
    assert not out.replayed and len(out.events) == 1


def test_scopes_do_not_share_idempotency():
    book = IntakeBook()
    accept(book, conv(["ok"]))
    out = accept(book, conv(["ok"], scope=Scope(tenant="t1", user="bram")))
    assert not out.replayed and len(out.events) == 1


def test_tool_log_and_asset_kinds_accepted():
    book = IntakeBook()
    t = SourceEnvelope(
        source_id="tl", kind="tool_log", scope=SCOPE, segments=[turn(0, "ls output", role="tool")]
    )
    assert len(accept(book, t).events) == 1
    ref = AssetRef(asset_id="a1", hash="h1", mime="image/png", url_or_path="x.png")
    a = SourceEnvelope(
        source_id="img",
        kind="asset",
        scope=SCOPE,
        segments=[Segment(seg_index=0, role="user", content="", assets=[ref])],
    )
    out = accept(book, a)
    assert out.events[0].payload["assets"][0]["hash"] == "h1"


# ---------------------------------------------------------------------
# Quota
# ---------------------------------------------------------------------
def reject(_cost: IntakeCost) -> QuotaExceeded:
    return QuotaExceeded(quota="rate", limit=1, current=1, retry_after=2.5)


def test_quota_rejection_leaves_no_state():
    store = InMemoryIntakeStore()
    book = IntakeBook(store)
    env = conv(["a", "b"])
    res = accept(book, env, quota=reject)
    assert isinstance(res, QuotaExceeded) and res.retry_after == 2.5
    assert store._sources == {} and store._writes == {} and store._receipts == {}
    # a later accepted attempt is a fresh, normal acceptance
    ok = accept(book, env)
    assert len(ok.events) == 2 and not ok.replayed


def test_quota_checked_once_with_cost_and_not_for_retries():
    seen = []

    def check(cost):
        seen.append(cost)
        return None

    book = IntakeBook()
    env = conv(["héllo", "b"])
    accept(book, env, quota=check)
    accept(book, env, quota=check)  # retry: nothing new, no check
    assert len(seen) == 1
    assert seen[0].storage_add == len("héllo".encode()) + 1
    assert seen[0].segments_add == 2 and seen[0].queue_add == 1


def test_quota_rejection_on_partial_retry_counts_only_new():
    seen = []
    book = IntakeBook()
    accept(book, conv(["a"]))
    accept(book, conv(["a", "bb"]), quota=lambda c: seen.append(c) or None)
    assert seen[0].segments_add == 1 and seen[0].storage_add == 2


def test_validation_error_precedes_quota():
    called = []
    with pytest.raises(IntakeError):
        accept(IntakeBook(), conv([" "]), quota=lambda c: called.append(c))
    assert called == []


# ---------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------
def test_document_first_ingest_has_no_plan():
    book = IntakeBook()
    out = accept(book, doc(["intro", "body", "end"]))
    assert out.plan is None and out.receipt.source_version == 1 and len(out.events) == 3


def test_document_same_content_noop():
    book = IntakeBook()
    first = accept(book, doc(["intro", "body"]))
    again = accept(book, doc(["intro", "body"]))
    assert again.noop and again.replayed and again.events == () and again.receipt == first.receipt
    explicit = accept(book, doc(["intro", "body"], version=7))
    assert explicit.noop and explicit.receipt == first.receipt


def test_document_edit_produces_version_plan():
    book = IntakeBook()
    v1 = accept(book, doc(["intro", "old body", "end"]))
    v2 = accept(book, doc(["intro", "new body", "end"]))
    plan = v2.plan
    assert plan.from_version == 1 and plan.to_version == 2
    assert plan.retract_event_ids == (v1.receipt.event_ids[1],)
    assert plan.accept_seg_indexes == (1,)
    assert plan.kept == ((0, v1.receipt.event_ids[0]), (2, v1.receipt.event_ids[2]))
    kinds = [e.kind for e in v2.events]
    assert kinds == ["memory.write", "memory.link"]
    link = v2.events[1].payload
    assert link["relation"] == "version_of" and link["retract_event_ids"] == list(
        plan.retract_event_ids
    )
    assert v2.events[0].payload["source_version"] == 2
    assert v2.receipt.source_version == 2
    assert v2.receipt.event_ids[0] == v1.receipt.event_ids[0]  # unchanged chunk keeps its event


def test_document_insert_shifts_indices_but_keeps_chunks():
    book = IntakeBook()
    v1 = accept(book, doc(["a", "b"]))
    v2 = accept(book, doc(["a", "new", "b"]))
    assert v2.plan.retract_event_ids == () and v2.plan.accept_seg_indexes == (1,)
    assert dict(v2.plan.kept) == {0: v1.receipt.event_ids[0], 2: v1.receipt.event_ids[1]}


def test_document_removed_chunk_only_retracts():
    book = IntakeBook()
    v1 = accept(book, doc(["a", "b", "c"]))
    v2 = accept(book, doc(["a", "c"]))
    assert v2.plan.retract_event_ids == (v1.receipt.event_ids[1],)
    assert v2.plan.accept_seg_indexes == ()
    assert [e.kind for e in v2.events] == ["memory.link"]


def test_document_revert_reaccepts_retracted_chunk():
    book = IntakeBook()
    accept(book, doc(["a", "b"]))
    accept(book, doc(["a", "c"]))
    v3 = accept(book, doc(["a", "b"]))
    assert v3.plan.accept_seg_indexes == (1,) and len(v3.plan.retract_event_ids) == 1
    assert v3.receipt.source_version == 3


def test_document_explicit_versions():
    book = IntakeBook()
    first = accept(book, doc(["a"], version=1))
    # same version, same content: retry
    assert accept(book, doc(["a"], version=1)).receipt == first.receipt
    # same version, other content: conflict
    with pytest.raises(IntakeConflictError):
        accept(book, doc(["z"], version=1))
    v3 = accept(book, doc(["b"], version=3))
    assert v3.receipt.source_version == 3
    # unknown lower version is stale
    with pytest.raises(IntakeError, match="stale"):
        accept(book, doc(["q"], version=2))
    # next automatic version follows the latest
    assert accept(book, doc(["c"])).receipt.source_version == 4


def test_document_retry_of_old_version_returns_its_receipt():
    book = IntakeBook()
    v1 = accept(book, doc(["a"], version=1))
    accept(book, doc(["b"], version=2))
    again = accept(book, doc(["a"], version=1))
    assert again.replayed and again.receipt == v1.receipt


def test_document_quota_rejection_leaves_no_state():
    book = IntakeBook()
    first = accept(book, doc(["a", "b"]))
    res = accept(book, doc(["a", "different"]), quota=reject)
    assert isinstance(res, QuotaExceeded)
    # state still at version 1: the same edit succeeds afterwards as version 2
    out = accept(book, doc(["a", "different"]))
    assert out.plan.from_version == 1 and out.receipt.source_version == 2
    assert first.receipt.source_version == 1


def test_document_removal_only_skips_quota():
    book = IntakeBook()
    accept(book, doc(["a", "b"]))
    out = accept(book, doc(["a"]), quota=reject)  # nothing new is stored
    assert out.plan.retract_event_ids and not isinstance(out, QuotaExceeded)


def test_duplicate_chunks_in_document_multiset_diff():
    book = IntakeBook()
    v1 = accept(book, doc(["x", "x", "y"]))
    v2 = accept(book, doc(["x", "y"]))
    assert len(v2.plan.retract_event_ids) == 1
    assert v2.plan.retract_event_ids[0] == v1.receipt.event_ids[1]


# ---------------------------------------------------------------------
# Receipt, readiness, ephemeral
# ---------------------------------------------------------------------
def test_unconfigured_capability_is_unavailable_never_pending():
    book = IntakeBook()
    out = accept(book, conv(["a"]), caps=["raw_search", "facts"])
    r = out.receipt.readiness
    assert (r.raw_search, r.facts) == ("pending", "pending")
    assert (r.graph, r.summaries, r.profile) == ("unavailable",) * 3


def test_capabilities_accepts_mapping_and_rejects_unknown():
    book = IntakeBook()
    out = accept(book, conv(["a"]), caps={"raw_search": True, "facts": False})
    assert out.receipt.readiness.raw_search == "pending"
    assert out.receipt.readiness.facts == "unavailable"
    with pytest.raises(IntakeError, match="unknown capability"):
        accept(book, conv(["b"]), caps=["vibes"])


def test_ephemeral_durability():
    book = IntakeBook(ephemeral=True)
    assert accept(book, conv(["a"])).receipt.durability == "memory"
    assert accept(IntakeBook(), conv(["a"])).receipt.durability == "durable"


def test_readiness_progress_and_original_receipt_is_stable():
    book = IntakeBook()
    out = accept(book, conv(["a"]), caps=["raw_search", "facts"])
    iid = out.receipt.intent_id
    r = book.readiness(iid, {"raw_search": "ready"})
    assert r.raw_search == "ready" and r.facts == "pending"
    assert book.readiness(iid) == r
    # failed facts may later be retried to ready
    book.readiness(iid, {"facts": "failed"})
    assert book.readiness(iid, {"facts": "ready"}).facts == "ready"
    # the original receipt is unchanged by progress
    again = accept(book, conv(["a"]))
    assert again.receipt.readiness.raw_search == "pending"
    assert again.receipt == out.receipt


def test_readiness_rules():
    book = IntakeBook()
    iid = accept(book, conv(["a"]), caps=["raw_search"]).receipt.intent_id
    with pytest.raises(IntakeError, match="not configured"):
        book.readiness(iid, {"graph": "ready"})
    book.readiness(iid, {"raw_search": "ready"})
    with pytest.raises(IntakeError, match="already ready"):
        book.readiness(iid, {"raw_search": "failed"})
    with pytest.raises(IntakeError, match="unknown capability"):
        book.readiness(iid, {"nope": "ready"})
    with pytest.raises(KeyError):
        book.readiness("intent-missing")
    iid2 = accept(book, conv(["b"]), caps=["facts"]).receipt.intent_id
    with pytest.raises(IntakeError, match="unavailable"):
        book.readiness(iid2, {"facts": "unavailable"})


def test_failed_update_is_atomic():
    book = IntakeBook()
    iid = accept(book, conv(["a"]), caps=["raw_search", "facts"]).receipt.intent_id
    with pytest.raises(IntakeError):
        book.readiness(iid, {"raw_search": "ready", "graph": "ready"})
    assert book.readiness(iid).raw_search == "pending"


def test_wait_snapshot():
    book = IntakeBook()
    iid = accept(book, conv(["a"]), caps=["raw_search", "facts"]).receipt.intent_id
    w = book.wait_snapshot(iid, ["raw_search", "facts"], waited_ms=50)
    assert w.timed_out and w.missing == ["raw_search", "facts"]
    book.readiness(iid, {"raw_search": "ready", "facts": "ready"})
    w2 = book.wait_snapshot(iid, ["raw_search", "facts", "raw_search"])
    assert not w2.timed_out and w2.requested == ["raw_search", "facts"]
    # an unavailable capability does not hold a wait open
    iid3 = accept(book, conv(["z"]), caps=["raw_search"]).receipt.intent_id
    assert not book.wait_snapshot(iid3, ["graph"]).timed_out


def test_each_document_version_has_its_own_readiness():
    book = IntakeBook()
    v1 = accept(book, doc(["a"]))
    book.readiness(v1.receipt.intent_id, {"raw_search": "ready"})
    v2 = accept(book, doc(["b"]))
    assert book.readiness(v2.receipt.intent_id).raw_search == "pending"
    assert book.readiness(v1.receipt.intent_id).raw_search == "ready"


# ---------------------------------------------------------------------
# Determinism and misc
# ---------------------------------------------------------------------
def test_determinism_across_books():
    env = conv(["a", "b"])
    a = accept(IntakeBook(), env)
    b = accept(IntakeBook(), env)
    assert a.receipt == b.receipt
    assert [e.model_dump() for e in a.events] == [e.model_dump() for e in b.events]
    d1 = accept(IntakeBook(), doc(["x", "y"]))
    d2 = accept(IntakeBook(), doc(["x", "y"]))
    assert d1.receipt == d2.receipt


def test_input_envelope_not_mutated():
    env = conv(["a\r\nb"])
    accept(IntakeBook(), env)
    assert env.segments[0].content == "a\r\nb"


def test_naive_now_rejected():
    with pytest.raises(IntakeError, match="aware"):
        IntakeBook().accept(conv(["a"]), ALL, None, now=datetime(2026, 1, 1))


def test_envelope_participants_normalised_into_events():
    env = conv(["a"], participants=["Noor", "Ilse", "Noor"])
    out = accept(IntakeBook(), env)
    assert out.events[0].payload["participants"] == ["Ilse", "Noor"]
