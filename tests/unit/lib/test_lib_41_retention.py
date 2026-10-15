from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.erasure import CONTENT_KEYS
from memspine.schemas.steps.offline import RetentionClass
from memspine.trust.consent import Tier
from memspine.trust.retention import (
    DEFAULT_CLASS,
    GENESIS_HASH,
    AuditLog,
    RetentionFormatError,
    RetentionProfile,
    RetentionRecord,
    RetentionRule,
    audit_admin,
    audit_consent,
    audit_erase,
    audit_read,
    audit_retention_expiry,
    audit_write,
    classify,
    default_profile,
    must_audit,
    parse_duration,
    parse_ttl,
    profile_names,
    prune_audit,
    record_audit,
    retention_plan,
    verify_chain,
)

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
SCOPE = OwnerScope(tenant="acme", owner="ilse")


def rec(id_: str, age_days: float = 0, **kw) -> RetentionRecord:
    return RetentionRecord(id=id_, created_at=NOW - timedelta(days=age_days), **kw)


# ------------------------------------------------------------------ TTL parsing


@pytest.mark.parametrize(
    ("text", "years", "months", "days", "seconds"),
    [
        ("P30D", 0, 0, 30, 0),
        ("P2W", 0, 0, 14, 0),
        ("P1Y6M", 1, 6, 0, 0),
        ("PT12H", 0, 0, 0, 43200),
        ("P1DT1H30M15S", 0, 0, 1, 5415),
        ("p1y", 1, 0, 0, 0),
        ("P0D", 0, 0, 0, 0),
    ],
)
def test_parse_duration(text, years, months, days, seconds):
    d = parse_duration(text)
    assert (d.years, d.months, d.days, d.seconds) == (years, months, days, seconds)


@pytest.mark.parametrize(
    "bad", ["", "P", "PT", "P1DT", "30D", "P-1D", "P1.5D", "P1H", "PT1D", "abc", "P1Y2"]
)
def test_parse_duration_rejects(bad):
    with pytest.raises(RetentionFormatError):
        parse_duration(bad)


def test_parse_ttl_never():
    assert parse_ttl(None) is None
    assert parse_ttl("never") is None
    assert parse_ttl(" Forever ") is None
    assert parse_ttl("P1D") is not None


def test_calendar_arithmetic_clamps_month_end():
    jan31 = datetime(2026, 1, 31, tzinfo=UTC)
    assert parse_duration("P1M").add_to(jan31) == datetime(2026, 2, 28, tzinfo=UTC)
    assert parse_duration("P1Y").add_to(datetime(2024, 2, 29, tzinfo=UTC)) == datetime(
        2025, 2, 28, tzinfo=UTC
    )
    assert parse_duration("P2M").subtract_from(datetime(2026, 1, 15, tzinfo=UTC)) == datetime(
        2025, 11, 15, tzinfo=UTC
    )
    assert parse_duration("PT36H").add_to(jan31) == jan31 + timedelta(hours=36)


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        parse_duration("P1D").add_to(datetime(2026, 1, 1))
    with pytest.raises(ValidationError):
        RetentionRecord(id="a", created_at=datetime(2026, 1, 1))


# ------------------------------------------------------------------ match and precedence


def test_match_fields():
    r = rec("a", kind="working", layer="L2", tier=Tier.SENSITIVE, purpose="support:billing")
    assert RetentionRule(name="x", match="*").matches(r)
    assert RetentionRule(name="x", match="kind=working|episodic").matches(r)
    assert not RetentionRule(name="x", match="kind!=working").matches(r)
    assert RetentionRule(name="x", match="layer=L2 & tier>=sensitive").matches(r)
    assert not RetentionRule(name="x", match="tier>sensitive").matches(r)
    assert not RetentionRule(name="x", match="tier<=personal").matches(r)
    assert RetentionRule(name="x", match="purpose=support").matches(
        r
    )  # parent purpose covers child
    assert not RetentionRule(name="x", match="purpose=billing").matches(r)
    assert RetentionRule(name="x", match="tier=sensitive|special_category").matches(r)
    assert RetentionRule(name="x", applies_to_layers=("L2",)).matches(r)
    assert not RetentionRule(name="x", applies_to_layers=("L3",)).matches(r)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "kind",
        "color=red",
        "kind=",
        "kind=a||b",
        "kind>=a",
        "tier=gold",
        "layer=L9",
        "tier>=a|b",
        "kind=a &",
    ],
)
def test_match_rejects(bad):
    with pytest.raises(ValidationError):
        RetentionRule(name="x", match=bad)


def test_rule_rejects_bad_ttl():
    with pytest.raises(ValidationError):
        RetentionRule(name="x", ttl="soon")


def test_precedence_priority_then_specificity_then_name():
    r = rec("a", kind="working", tier=Tier.SENSITIVE)
    broad = RetentionRule(name="broad", match="kind=working", ttl="P30D")
    narrow = RetentionRule(name="narrow", match="kind=working & tier>=sensitive", ttl="P1D")
    assert classify(r, [broad, narrow]).name == "narrow"
    assert classify(r, [narrow, broad]).name == "narrow"
    boosted = RetentionRule(name="boosted", match="kind=working", ttl="P90D", priority=5)
    assert classify(r, [broad, narrow, boosted]).name == "boosted"
    a = RetentionRule(name="a", match="kind=working", ttl="P1D")
    b = RetentionRule(name="b", match="kind=working", ttl="P2D")
    assert classify(r, [b, a]).name == "a"


def test_classify_default_never_expires():
    r = rec("a", kind="semantic")
    got = classify(r, [RetentionRule(name="w", match="kind=working", ttl="P1D")])
    assert got is DEFAULT_CLASS
    assert got.duration is None


def test_schema_class_adoption():
    rc = RetentionClass(match="kind=episodic", ttl="P180D", applies_to_layers=["L1"])
    rule = RetentionRule.from_schema(rc, name="ep")
    assert rule.applies_to_layers == ("L1",)
    assert rule.matches(rec("a", kind="episodic", layer="L1"))
    assert not rule.matches(rec("a", kind="episodic", layer="L2"))


# ------------------------------------------------------------------ plans


def sample_records() -> list[RetentionRecord]:
    return [
        rec("e-old", 400, layer="L0", seq=1),
        rec("e-mid", 100, layer="L0", seq=2),
        rec("e-new", 1, layer="L0", seq=3),
        rec("w-old", 10, kind="working", layer="L2"),
        rec("w-new", 1, kind="working", layer="L2"),
        rec("s-old", 100, kind="session", layer="L2"),
        rec("sem-old", 900, kind="semantic", layer="L3"),
    ]


def test_full_mode_never_expires_l0_and_keeps_longterm():
    plan = retention_plan(sample_records(), NOW, default_profile("assistant"), scope=SCOPE)
    assert set(plan.expired_ids) == {"w-old", "s-old"}
    assert plan.l0_protected == 3
    assert {"e-old", "e-mid", "e-new", "sem-old", "w-new"} <= set(plan.retained)
    assert plan.boundary.mode == "full"
    assert plan.rebuild == "full"
    assert not plan.advertises_limited_rebuild
    assert set(plan.tombstones) == set(plan.expired_ids)
    assert plan.lineage_kept


def test_full_mode_ignores_a_class_that_matches_l0():
    prof = RetentionProfile(name="p", classes=(RetentionRule(name="all", match="*", ttl="P1D"),))
    plan = retention_plan([rec("e", 50, layer="L0", seq=1)], NOW, prof, scope=SCOPE)
    assert plan.expired_ids == ()
    assert plan.l0_protected == 1


def test_rolling_prunes_window_and_advertises_limited():
    plan = retention_plan(sample_records(), NOW, default_profile("coding"), scope=SCOPE)
    # window is 90 days: e-old (400d) pruned, e-mid (100d) pruned, e-new kept
    assert {"e-old", "e-mid"} <= set(plan.expired_ids)
    assert "e-new" in plan.retained
    assert plan.boundary.mode == "rolling"
    assert plan.boundary.boundary_seq == 2
    assert plan.boundary.boundary_ts == NOW - timedelta(days=90)
    assert plan.rebuild == "limited"
    assert plan.advertises_limited_rebuild
    assert plan.boundary.lineage_kept and plan.boundary.tombstones_kept
    assert "s-old" in plan.expired_ids  # session record (100d) past the P14D scratch ttl
    assert "w-old" in plan.retained  # 10d old, inside P14D


def test_rolling_boundary_without_pruned_events_still_valid():
    plan = retention_plan(
        [rec("e-new", 1, layer="L0", seq=9)], NOW, default_profile("coding"), scope=SCOPE
    )
    assert plan.expired_ids == ()
    assert plan.boundary.boundary_seq is None
    assert plan.boundary.boundary_ts is not None
    assert plan.rebuild == "limited"


def test_boundary_is_monotonic_and_pruning_is_sticky():
    first = retention_plan(sample_records(), NOW, default_profile("coding"), scope=SCOPE)
    later = NOW + timedelta(days=1)
    second = retention_plan(
        [rec("e-new", 1, layer="L0", seq=3)],
        later,
        default_profile("coding"),
        scope=SCOPE,
        previous=first.boundary,
    )
    assert second.boundary.boundary_ts >= first.boundary.boundary_ts
    assert second.boundary.boundary_seq == 2  # not lowered
    # switching the profile to full never re-advertises a full rebuild
    third = retention_plan(
        [], later, default_profile("assistant"), scope=SCOPE, previous=first.boundary
    )
    assert third.rebuild == "limited"
    assert third.boundary.mode == "rolling"
    assert third.boundary.boundary_ts == first.boundary.boundary_ts


def test_ephemeral_expires_all_events_and_is_unavailable():
    plan = retention_plan(sample_records(), NOW, default_profile("ephemeral"), scope=SCOPE)
    assert {"e-old", "e-mid", "e-new"} <= set(plan.expired_ids)
    assert plan.boundary.mode == "ephemeral"
    assert plan.rebuild == "unavailable"
    assert "sem-old" in plan.retained  # long-term derived memory is not auto-expired
    assert "w-old" in plan.expired_ids
    # ephemeral stays ephemeral even if the profile later says full
    again = retention_plan(
        [], NOW, default_profile("assistant"), scope=SCOPE, previous=plan.boundary
    )
    assert again.rebuild == "unavailable"


def test_holds_and_pending_work_block_expiry():
    records = [
        rec("w1", 100, kind="working", layer="L2", legal_hold=True),
        rec("w2", 100, kind="working", layer="L2", pending=True),
        rec("w3", 100, kind="working", layer="L2"),
        rec("e", 500, layer="L0", seq=1, pending=True),
    ]
    plan = retention_plan(records, NOW, default_profile("ephemeral"), scope=SCOPE)
    assert plan.expired_ids == ("w3",)
    reasons = {h.record_id: h.reason for h in plan.held}
    assert reasons == {"w1": "legal_hold", "w2": "pending_work", "e": "pending_work"}


def test_hold_class():
    prof = RetentionProfile(
        name="p", classes=(RetentionRule(name="keep", match="kind=working", ttl="P1D", hold=True),)
    )
    plan = retention_plan([rec("w", 100, kind="working", layer="L2")], NOW, prof, scope=SCOPE)
    assert plan.expired_ids == ()
    assert plan.held[0].reason == "hold_class"


def test_expiry_edge_is_inclusive():
    prof = RetentionProfile(
        name="p", classes=(RetentionRule(name="w", match="kind=working", ttl="P7D"),)
    )
    exact = retention_plan([rec("w", 7, kind="working", layer="L2")], NOW, prof, scope=SCOPE)
    just = retention_plan(
        [
            RetentionRecord(
                id="w",
                kind="working",
                layer="L2",
                created_at=NOW - timedelta(days=7) + timedelta(seconds=1),
            )
        ],
        NOW,
        prof,
        scope=SCOPE,
    )
    assert exact.expired_ids == ("w",)
    assert just.expired_ids == ()


def test_regulated_sensitive_scratch_expires_sooner():
    prof = default_profile("regulated")
    plan = retention_plan(
        [
            rec("a", 40, kind="working", layer="L2", tier=Tier.SENSITIVE),
            rec("b", 40, kind="working", layer="L2", tier=Tier.PERSONAL),
        ],
        NOW,
        prof,
        scope=SCOPE,
    )
    assert plan.expired_ids == ("a",)


def test_profile_validation():
    with pytest.raises(ValidationError):
        RetentionProfile(name="p", event_log_mode="rolling")
    with pytest.raises(ValidationError):
        RetentionProfile(name="p", event_log_mode="rolling", rolling_window="P0D")
    with pytest.raises(ValidationError):
        RetentionProfile(name="p", classes=(RetentionRule(name="a"), RetentionRule(name="a")))
    with pytest.raises(KeyError):
        default_profile("nope")
    for n in profile_names():
        assert default_profile(n).name == n
        assert default_profile(n, "server").store == "server"


def test_plan_deterministic_and_order_independent():
    recs = sample_records()
    p1 = retention_plan(recs, NOW, default_profile("coding"), scope=SCOPE)
    p2 = retention_plan(list(reversed(recs)), NOW, default_profile("coding"), scope=SCOPE)
    assert p1 == p2
    assert p1.model_dump_json() == p2.model_dump_json()


# ------------------------------------------------------------------ audit


def build_log() -> AuditLog:
    log = AuditLog()
    t = NOW
    log = log.append(audit_erase("ilse", ["r1", "r2"], t, reason="user_request", tenant="acme"))
    log = log.append(
        audit_read("bram", ["r3"], t + timedelta(seconds=1), purpose="support:billing")
    )
    log = log.append(audit_consent("ilse", ["g1"], t + timedelta(seconds=2), op="grant"))
    log = log.append(audit_admin("noor", [], t + timedelta(seconds=3), op="export"))
    log = log.append(
        audit_write(
            "aiko", ["r9"], t + timedelta(seconds=4), outcome="denied", reason="trust_floor"
        )
    )
    return log


def test_chain_links_and_verifies():
    log = build_log()
    assert [r.seq for r in log.records] == [1, 2, 3, 4, 5]
    assert log.records[0].prev_hash == GENESIS_HASH
    for a, b in zip(log.records, log.records[1:], strict=False):
        assert b.prev_hash == a.hash
    rep = log.verify()
    assert rep.ok and rep.checked == 5
    assert verify_chain(()).ok


def test_append_is_functional():
    log = build_log()
    longer = log.append(audit_read("bram", ["r4"], NOW + timedelta(seconds=10)))
    assert len(log.records) == 5 and len(longer.records) == 6


def test_tamper_detection():
    log = build_log()
    recs = list(log.records)

    edited = recs.copy()
    edited[1] = recs[1].model_copy(update={"ids": ("r-other",)})
    rep = verify_chain(edited)
    assert (rep.ok, rep.broken_at, rep.reason) == (False, 2, "hash")

    outcome = recs.copy()
    outcome[4] = recs[4].model_copy(update={"outcome": "ok"})
    assert verify_chain(outcome).broken_at == 5

    deleted = recs[:1] + recs[2:]
    rep = verify_chain(deleted)
    assert (rep.ok, rep.reason) == (False, "seq_gap")

    swapped = [recs[0], recs[2], recs[1], *recs[3:]]
    assert not verify_chain(swapped).ok

    # re-hashing a forged record still breaks the next link
    forged = recs[1].model_copy(update={"actor": "mallory"})
    forged = forged.model_copy(update={"hash": forged.compute_hash()})
    rep = verify_chain([recs[0], forged, *recs[2:]])
    assert (rep.ok, rep.broken_at, rep.reason) == (False, 3, "prev_hash")

    # truncating the tail is undetectable from the records alone but the head hash moves
    assert verify_chain(recs[:-1]).ok
    assert log.head_hash != recs[-2].hash


def test_wrong_anchor_fails():
    log = build_log()
    assert not verify_chain(log.records, anchor_hash="1" * 64).ok


def test_time_order_enforced():
    log = (
        AuditLog()
        .append(audit_read("a1", ["r"], NOW))
        .append(audit_read("a1", ["r"], NOW - timedelta(seconds=5)))
    )
    assert log.verify().reason == "time_order"


def test_audit_holds_no_content():
    with pytest.raises(ValidationError):
        audit_erase("ilse", ["Ilse's phone number is 555 0100"], NOW)
    with pytest.raises(ValidationError):
        audit_erase("ilse", ["r1"], NOW, reason="she asked to delete the note about her sister")
    with pytest.raises(ValidationError):
        audit_read("bram", ["r"], NOW, purpose="what is the plan for dinner")
    with pytest.raises(ValidationError):
        audit_read("Bram Jansen", ["r"], NOW)
    with pytest.raises(ValidationError):
        audit_read("bram", ["r"], NOW, op="free text op")
    # extra fields (where content could hide) are refused outright
    for key in CONTENT_KEYS:
        with pytest.raises(ValidationError):
            audit_erase.__globals__["AuditEntry"](
                actor="a", action="erase", op="forget", at=NOW, **{key: "secret text"}
            )
    log = build_log()
    dumped = log.model_dump_json()
    assert "payload" not in dumped and "content" not in dumped


def test_audit_naive_time_rejected():
    with pytest.raises(ValidationError):
        audit_read("a1", ["r"], datetime(2026, 1, 1))


def test_must_audit_per_profile():
    a, r, m, c = (default_profile(n) for n in ("assistant", "regulated", "multi_agent", "coding"))
    for p in (a, r, m, c):
        for action in ("erase", "consent", "admin"):
            assert must_audit(p, action)  # type: ignore[arg-type]
        assert must_audit(p, "write", "correct")
    assert not must_audit(a, "read") and not must_audit(c, "read")
    assert must_audit(r, "read") and must_audit(m, "read")
    assert not must_audit(a, "write", "remember")
    assert must_audit(r, "write", "remember")


def test_record_audit_skips_unaudited_reads():
    log = AuditLog()
    skipped = record_audit(log, default_profile("assistant"), audit_read("bram", ["r"], NOW))
    kept = record_audit(log, default_profile("regulated"), audit_read("bram", ["r"], NOW))
    assert skipped is log and len(kept.records) == 1


def test_retention_expiry_audit_carries_ids_only():
    plan = retention_plan(sample_records(), NOW, default_profile("assistant"), scope=SCOPE)
    entry = audit_retention_expiry(plan)
    assert entry is not None
    assert entry.action == "erase" and entry.op == "retention_expiry"
    assert set(entry.ids) == set(plan.expired_ids)
    empty = retention_plan([], NOW, default_profile("assistant"), scope=SCOPE)
    assert audit_retention_expiry(empty) is None
    assert AuditLog().append(entry).verify().ok


def test_audit_retention_per_profile_and_anchor():
    log = AuditLog()
    for i in range(6):
        log = log.append(audit_erase("ilse", [f"r{i}"], NOW - timedelta(days=400 - 80 * i)))
    # assistant keeps one year: records older than 365d dropped (only the 400d record)
    pruned = prune_audit(log, NOW, default_profile("assistant"))
    assert [r.seq for r in pruned.records] == [2, 3, 4, 5, 6]
    assert pruned.anchor_seq == 1 and pruned.anchor_hash == log.records[0].hash
    assert pruned.verify().ok
    assert pruned.next_seq == 7
    assert pruned.append(audit_erase("ilse", ["r9"], NOW)).verify().ok
    # without the anchor the pruned records do not verify from genesis
    assert not verify_chain(pruned.records).ok
    # a shorter profile drops more; regulated keeps everything
    assert [r.seq for r in prune_audit(log, NOW, default_profile("coding")).records] == [5, 6]
    assert prune_audit(log, NOW, default_profile("regulated")) is log
    forever = RetentionProfile(name="f", audit_retention=None)
    assert prune_audit(log, NOW, forever) is log
    assert prune_audit(AuditLog(), NOW, default_profile("assistant")).records == ()


def test_prune_all_records_keeps_anchor():
    log = AuditLog().append(audit_erase("ilse", ["r"], NOW - timedelta(days=1000)))
    pruned = prune_audit(log, NOW, default_profile("assistant"))
    assert pruned.records == () and pruned.anchor_seq == 1
    assert pruned.head_hash == log.head_hash
    assert pruned.append(audit_read("a1", ["r"], NOW)).verify().ok


def test_audit_determinism():
    assert build_log() == build_log()
    assert build_log().head_hash == build_log().head_hash
    assert build_log().model_dump_json() == build_log().model_dump_json()


def test_timezone_normalised_in_hash():
    from datetime import timezone

    plus2 = timezone(timedelta(hours=2))
    a = AuditLog().append(audit_read("a1", ["r"], NOW))
    b = AuditLog().append(audit_read("a1", ["r"], NOW.astimezone(plus2)))
    assert a.head_hash == b.head_hash
