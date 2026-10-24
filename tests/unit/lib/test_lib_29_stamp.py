"""LIB-29: stamping (three clocks, zone, axes, provenance class, write id)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from memspine.ingest.stamp import (
    StampIn,
    StampPolicy,
    normalise_principal,
    parse_source_time,
    stamp,
    write_id,
)

REC = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)


def mk(**kw) -> StampIn:
    base = dict(
        tenant="t1",
        owner="ilse",
        source_id="conv-1",
        seg_index=0,
        role="user",
        content="I moved to Porto",
    )
    base.update(kw)
    return StampIn(**base)


def st(**kw):
    rec = kw.pop("recorded_at", REC)
    dtz = kw.pop("default_tz", "UTC")
    pol = kw.pop("policy", None)
    return stamp(mk(**kw), recorded_at=rec, default_tz=dtz, policy=pol)


# ------------------------------------------------------------------------------------ time formats


def test_iso_with_z() -> None:
    r = st(source_at="2026-10-01T08:30:00Z")
    assert r.source_at == datetime(2026, 10, 1, 8, 30, tzinfo=UTC)
    assert r.source_tz == "UTC" and not r.tz_unknown and r.precision == "second"


def test_iso_with_offset_keeps_wall_clock() -> None:
    r = st(source_at="2026-10-01T08:30:00+05:30")
    assert r.source_at is not None and r.source_at.hour == 8
    assert r.source_at_utc == datetime(2026, 10, 1, 3, 0, tzinfo=UTC)
    assert r.source_tz == "+05:30" and not r.tz_unknown


def test_aware_datetime() -> None:
    r = st(source_at=datetime(2026, 10, 1, 8, 0, tzinfo=UTC))
    assert r.source_at_utc == datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


def test_naive_datetime_with_iana_zone() -> None:
    r = st(source_at=datetime(2026, 10, 1, 8, 0), tz="Asia/Tokyo")
    assert r.source_at_utc == datetime(2026, 9, 30, 23, 0, tzinfo=UTC)
    assert r.source_tz == "Asia/Tokyo" and not r.tz_unknown


def test_naive_string_with_offset_string_zone() -> None:
    r = st(source_at="2026-10-01 08:00:00", tz="UTC+9")
    assert r.source_at_utc == datetime(2026, 9, 30, 23, 0, tzinfo=UTC)
    assert r.source_tz == "+09:00"


def test_envelope_zone_used_when_segment_has_none() -> None:
    r = st(source_at="2026-10-01T08:00:00", envelope_tz="Europe/Lisbon")
    assert r.source_tz == "Europe/Lisbon"
    assert r.source_at_utc == datetime(2026, 10, 1, 7, 0, tzinfo=UTC)  # WEST is UTC+1


def test_segment_zone_beats_envelope_zone() -> None:
    r = st(source_at="2026-10-01T08:00:00", tz="Asia/Tokyo", envelope_tz="Europe/Lisbon")
    assert r.source_tz == "Asia/Tokyo"


@pytest.mark.parametrize("val", [1_790_000_000, 1_790_000_000.0, "1790000000"])
def test_epoch_seconds(val) -> None:
    r = st(source_at=val)
    assert r.source_at_utc == datetime.fromtimestamp(1_790_000_000, UTC)
    assert r.tz_unknown and r.source_tz == "unknown"  # instant exact, zone not declared


def test_epoch_milliseconds() -> None:
    r = st(source_at=1_790_000_000_000)
    assert r.source_at_utc == datetime.fromtimestamp(1_790_000_000, UTC)
    assert "epoch_ms" in r.notes


def test_epoch_with_declared_zone() -> None:
    r = st(source_at=1_790_000_000, tz="Asia/Tokyo")
    assert (
        not r.tz_unknown
        and r.source_at is not None
        and r.source_at.utcoffset() == timedelta(hours=9)
    )


def test_bare_date_has_day_precision() -> None:
    r = st(source_at="2026-10-01", tz="Europe/Lisbon")
    assert r.precision == "day" and r.source_at is not None and r.source_at.hour == 0
    r2 = st(source_at=date(2026, 10, 1), tz="UTC")
    assert r2.precision == "day"


@pytest.mark.parametrize(
    "bad", ["not a date", "2026-13-45", True, float("nan"), "99999999999999999999.5.5"]
)
def test_unparseable_is_null_and_flagged(bad) -> None:
    r = st(source_at=bad)
    assert r.source_at is None and r.source_at_utc is None
    assert "stamp_unparsed" in r.flags and r.precision == "unknown"


def test_missing_time_is_null_never_now() -> None:
    r = st(source_at=None)
    assert r.source_at is None and "stamp_unparsed" not in r.flags
    assert r.recorded_at == REC and not r.tz_unknown


# ------------------------------------------------------------------------------------ unknown zone


def test_unknown_zone_uses_default_and_is_flagged() -> None:
    r = st(source_at="2026-10-01T08:00:00", default_tz="Asia/Tokyo")
    assert r.tz_unknown and r.source_tz == "unknown"
    assert "tz_unknown" in r.flags and "tz_assumed_default" in r.notes
    assert r.source_at is not None and r.source_at.hour == 8  # wall clock kept
    assert r.source_at_utc == datetime(2026, 9, 30, 23, 0, tzinfo=UTC)


def test_unknown_zone_default_utc_keeps_wall_clock() -> None:
    r = st(source_at=datetime(2026, 10, 1, 23, 30))
    assert (
        r.tz_unknown
        and r.source_at is not None
        and (r.source_at.hour, r.source_at.minute) == (23, 30)
    )


def test_invalid_declared_zone_falls_back_flagged() -> None:
    r = st(source_at="2026-10-01T08:00:00", tz="Mars/Olympus")
    assert r.tz_unknown and {"tz_invalid", "tz_unknown"} <= set(r.flags)


def test_bad_default_zone_raises() -> None:
    with pytest.raises(ValueError):
        st(source_at="2026-10-01T08:00:00", default_tz="Nowhere/Land")


# ------------------------------------------------------------------------------------ DST edges


def test_dst_gap_flagged() -> None:
    # 2026-03-08 02:30 does not exist in New York
    r = st(source_at="2026-03-08T02:30:00", tz="America/New_York")
    assert "dst_gap" in r.flags and r.source_at_utc is not None


def test_dst_ambiguous_flagged_and_first_occurrence() -> None:
    # 2026-11-01 01:30 happens twice in New York; the first is EDT (UTC-4)
    r = st(source_at="2026-11-01T01:30:00", tz="America/New_York")
    assert "dst_ambiguous" in r.flags
    assert r.source_at_utc == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)


def test_dst_ordinary_time_has_no_flag() -> None:
    r = st(source_at="2026-07-01T12:00:00", tz="America/New_York")
    assert not r.flags and r.source_at_utc == datetime(2026, 7, 1, 16, 0, tzinfo=UTC)


def test_dst_boundary_just_after_gap() -> None:
    r = st(source_at="2026-03-08T03:00:00", tz="America/New_York")
    assert "dst_gap" not in r.flags and r.source_at_utc == datetime(2026, 3, 8, 7, 0, tzinfo=UTC)


# ------------------------------------------------------------------------------- skew and order


def test_future_skew_flagged_and_kept() -> None:
    r = st(source_at="2026-10-13T13:00:00Z")
    assert "future_skew" in r.flags and r.source_at is not None


def test_skew_within_tolerance_ok() -> None:
    r = st(source_at="2026-10-13T12:03:00Z")
    assert "future_skew" not in r.flags


def test_skew_tolerance_configurable() -> None:
    pol = StampPolicy(future_skew_tolerance_s=0)
    assert "future_skew" in st(source_at="2026-10-13T12:00:01Z", policy=pol).flags


def test_recorded_at_must_be_aware() -> None:
    with pytest.raises(ValueError):
        st(recorded_at=datetime(2026, 10, 13, 12, 0))


def test_out_of_order_flagged() -> None:
    last = datetime(2026, 10, 2, tzinfo=UTC)
    r = st(source_at="2026-10-01T00:00:00Z", last_source_at=last)
    assert "out_of_order" in r.flags and r.source_at is not None


def test_in_order_and_equal_not_flagged() -> None:
    last = datetime(2026, 10, 1, tzinfo=UTC)
    assert "out_of_order" not in st(source_at="2026-10-01T00:00:00Z", last_source_at=last).flags
    assert "out_of_order" not in st(source_at="2026-10-02T00:00:00Z", last_source_at=last).flags


def test_out_of_order_compares_instants_across_zones() -> None:
    last = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    r = st(source_at="2026-10-01T20:00:00", tz="Asia/Tokyo", last_source_at=last)  # 11:00 UTC
    assert "out_of_order" in r.flags


def test_out_of_order_tolerance() -> None:
    last = datetime(2026, 10, 1, 12, 0, 10, tzinfo=UTC)
    pol = StampPolicy(out_of_order_tolerance_s=30)
    assert (
        "out_of_order"
        not in st(source_at="2026-10-01T12:00:00Z", last_source_at=last, policy=pol).flags
    )


# ------------------------------------------------------------------------------- axes and parties


def test_speaker_addressee_normalised() -> None:
    r = st(speaker="  Bram  ", addressee="NOOR")
    assert (r.spk, r.addr) == ("bram", "noor")


def test_alias_map() -> None:
    pol = StampPolicy(aliases={"bram": "user:42"})
    assert st(speaker="Bram", policy=pol).spk == "user:42"


def test_speaker_from_name_prefix() -> None:
    r = st(content="Tomasz: see you at six", speaker=None)
    assert r.spk == "tomasz" and "speaker_from_prefix" in r.notes


def test_prefix_ignored_for_non_names_and_tool_role() -> None:
    assert st(content="Note: bring a coat").spk is None
    assert st(content="Aiko: hello", role="tool").spk is None


def test_explicit_speaker_beats_prefix() -> None:
    assert st(content="Aiko: hello", speaker="Bram").spk == "bram"


def test_multi_party_participants_from_envelope() -> None:
    r = st(
        speaker="Bram",
        addressee="Noor",
        content="Ilse moved to Porto",
        participants=["Bram", "Noor", "Ilse"],
    )
    assert r.participants == ("bram", "noor", "ilse")
    assert r.spk == "bram"


def test_participants_fall_back_to_session_speakers_plus_this_turn() -> None:
    r = st(speaker="Aiko", session_speakers=["Bram", "Noor"])
    assert r.participants == ("bram", "noor", "aiko")


def test_participants_deduplicated_and_speaker_added() -> None:
    r = st(speaker="Bram", participants=["noor", "NOOR", " Noor "])
    assert r.participants == ("noor", "bram")


def test_no_parties_gives_empty_participants() -> None:
    assert st().participants == ()


# ---------------------------------------------------------------------------------- provenance


@pytest.mark.parametrize(
    ("role", "cls", "trust", "use"),
    [
        ("user", "user_stated", 0.7, "stated"),
        ("assistant", "assistant_proposed", 0.5, "proposal"),
        ("tool", "tool", 0.4, "evidence"),
        ("system", "tool", 0.9, "excluded"),
        ("document", "document", 0.4, "evidence"),
        ("external", "external", 0.3, "evidence"),
        ("operator", "user_stated", 0.9, "stated"),
    ],
)
def test_provenance_from_role(role, cls, trust, use) -> None:
    r = st(role=role)
    assert (r.src_class, r.base_trust, r.derive_use) == (cls, trust, use)


def test_unknown_role_rejected() -> None:
    with pytest.raises(ValueError):
        st(role="narrator")


def test_family_is_asserting_principal() -> None:
    assert st(role="user", speaker="Bram").family == "person:bram"
    assert st(role="user").family == "person:ilse"  # owner when speaker unknown
    assert st(role="assistant").family == "agent:assistant"
    assert st(role="tool", speaker="calendar").family == "tool:calendar"
    assert st(role="document").family == "document:conv-1"


# --------------------------------------------------------------------------------- write id (S3-3)


def test_retry_gives_same_write_id_even_with_other_recorded_time_and_client_key() -> None:
    a = st(caller_id="abc")
    b = st(caller_id="zzz", recorded_at=REC + timedelta(hours=3))
    assert a.write_id == b.write_id
    assert a.recorded_at != b.recorded_at


def test_same_words_other_position_is_new_write() -> None:
    assert st(seg_index=0).write_id != st(seg_index=1).write_id


def test_same_words_other_session_is_new_write() -> None:
    assert st(source_id="conv-1").write_id != st(source_id="conv-2").write_id


def test_scope_and_content_change_the_id() -> None:
    base = st().write_id
    assert st(tenant="t2").write_id != base
    assert st(owner="bram").write_id != base
    assert st(content="I moved to Porto.").write_id != base


def test_source_time_is_position_when_no_index() -> None:
    a = st(seg_index=None, source_at="2026-10-01T08:00:00Z")
    b = st(seg_index=None, source_at="2026-10-01T10:00:00+02:00")  # same instant
    c = st(seg_index=None, source_at="2026-10-02T08:00:00Z")
    assert a.write_id == b.write_id != c.write_id


def test_no_position_flagged() -> None:
    assert "write_id_no_position" in st(seg_index=None).flags


def test_write_id_field_boundaries_do_not_collide() -> None:
    assert write_id("a", "bc", "s", 0, "x") != write_id("ab", "c", "s", 0, "x")


# ------------------------------------------------------------------------------------ determinism


def test_determinism() -> None:
    kw = dict(
        source_at="2026-03-08T02:30:00",
        tz="America/New_York",
        speaker="Bram",
        participants=["Noor"],
    )
    assert st(**kw) == st(**kw)
    assert st(**kw).model_dump_json() == st(**kw).model_dump_json()


def test_stamped_turn_is_frozen() -> None:
    r = st()
    with pytest.raises(ValueError):
        r.spk = "x"  # type: ignore[misc]


def test_parse_source_time_direct() -> None:
    at, tz, unk, prec, flags, _ = parse_source_time("2026-10-01T08:00:00Z", None, "UTC")
    assert at is not None and tz == "UTC" and not unk and prec == "second" and not flags


def test_normalise_principal_edges() -> None:
    assert normalise_principal(None) is None and normalise_principal("   ") is None
    wide = "".join(chr(c) for c in (0xFF29, 0xFF2C, 0xFF33, 0xFF25))
    assert normalise_principal(wide) == "ilse"  # full-width letters fold under NFKC
