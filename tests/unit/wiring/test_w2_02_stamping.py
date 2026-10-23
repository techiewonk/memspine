"""W2-02: stamping (LIB-29 wired). ``write.stamping_v2``, default OFF.

Model-free: the hash embedder. All text is invented (Ilse, Bram, Noor, Tomasz). Covers: flag off is
unchanged; three clocks stamped and the zone kept; speaker / addressee / participants on multi-party
turns; provenance class from roles plus assent (a labelled multilingual assent set); asserter / subject
mismatch flagged; the stamping runs inside the intake gateway too; storage and migration round trip.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from memspine.api.engine import Engine
from memspine.derive.asserter import AsserterSubjectCheck, CitedLine
from memspine.ingest.provenance import AssentChecker, SourceClassifier
from memspine.ingest.stamping import ClockStamper, RoleStamper, participants_of_call
from memspine.store.records import MemoryRecord, RecordAxes

NS = "acme/ilse"


async def build(db: Path, **write: Any) -> Engine:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {"provider": "sqlite", "settings": {"path": str(db), "busy_timeout_s": 5.0}}
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"episodic": {"enabled": True}},
        write=write,
    )
    await eng.start()
    return eng


async def rows(eng: Engine) -> list[MemoryRecord]:
    return sorted(
        await eng._require_started().list_records(NS, "episodic"), key=lambda r: r.content
    )


GROUP = [
    {
        "message_id": "stamped-0",
        "conversation_id": "stamping-conversation",
        "role": "user",
        "speaker": "Ilse",
        "addressee": "Bram",
        "content": "Bram, I moved to Oslo",
        "timestamp": "2026-03-01T09:00:00+09:00",
        "tz": "Asia/Tokyo",
    },
    {
        "message_id": "stamped-1",
        "conversation_id": "stamping-conversation",
        "role": "user",
        "speaker": "Bram",
        "addressee": "Ilse",
        "content": "Noor told me about it",
        "timestamp": "2026-03-01T01:05:00",
    },
    {
        "message_id": "stamped-2",
        "conversation_id": "stamping-conversation",
        "role": "assistant",
        "content": "Shall I note the Oslo address?",
        "timestamp": "2026-03-01T01:06:00",
    },
    {
        "message_id": "stamped-3",
        "conversation_id": "stamping-conversation",
        "role": "user",
        "speaker": "Ilse",
        "content": "yes please, note it",
        "timestamp": "2026-03-01T01:07:00",
    },
]


# ------------------------------------------------------------------------------------------ flag off
async def test_flag_off_is_unchanged(tmp_path: Path) -> None:
    eng = await build(tmp_path / "off.db")
    try:
        assert eng._config().write.stamping_v2 is False
        await eng.ingest_messages(GROUP, namespace=NS, session_id="s1")
        for r in await rows(eng):
            assert r.source_tz is None
            assert r.axes.participants == []
            assert not any(t.startswith(("participant:", "assent_of:")) for t in r.tags)
            assert "source_tz" not in r.model_dump(mode="json")
            assert "participants" not in r.model_dump(mode="json").get("axes", {})
    finally:
        await eng.stop()


# ------------------------------------------------------------------------------- clocks and zone
async def test_three_clocks_and_zone(tmp_path: Path) -> None:
    eng = await build(tmp_path / "on.db", stamping_v2=True)
    before = datetime.now(UTC)
    try:
        await eng.ingest_messages(GROUP, namespace=NS, session_id="s1")
        by = {r.content: r for r in await rows(eng)}
        first = by["Bram, I moved to Oslo"]
        # declared zone preserved, instant exact (09:00 at +09:00 is 00:00 UTC)
        assert first.source_tz == "Asia/Tokyo"
        # (storage keeps the instant in UTC; the zone lives in source_tz)
        assert first.source_at is not None
        assert first.source_at.astimezone(UTC) == datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
        # naive time, no zone: wall clock kept, zone unknown, never shifted
        second = by["Noor told me about it"]
        assert second.source_tz == "unknown"
        assert (second.source_at.hour, second.source_at.minute) == (1, 5)
        # recorded_at is the engine clock, not the source time
        assert first.recorded_at >= before
        assert first.recorded_at != first.source_at
    finally:
        await eng.stop()


async def test_undated_turn_gets_generated_source_time(tmp_path: Path) -> None:
    eng = await build(tmp_path / "u.db", stamping_v2=True)
    try:
        before = datetime.now(UTC)
        await eng.ingest_messages(
            [{"message_id": "goats-1", "role": "user", "content": "Tomasz keeps goats"}],
            namespace=NS,
            session_id="s2",
        )
        (rec,) = await rows(eng)
        assert rec.source_at is not None and rec.source_at >= before
        assert rec.source_tz == "UTC"
        assert rec.source.message["created_at_generated"] is True
        assert rec.recorded_at is not None
    finally:
        await eng.stop()


def test_clock_stamper_flags() -> None:
    cs = ClockStamper(default_tz="UTC")
    now = datetime(2026, 3, 2, tzinfo=UTC)
    naive = cs.stamp("2026-03-01T10:00:00", now)
    assert naive.tz_unknown and naive.source_tz == "unknown" and "tz_unknown" in naive.flags
    assert naive.source_at.hour == 10  # wall clock, not shifted
    aware = cs.stamp("2026-03-01T10:00:00+05:30", now, tz="Asia/Kolkata")
    assert not aware.tz_unknown and aware.source_tz == "Asia/Kolkata"
    future = cs.stamp("2026-04-01T10:00:00Z", now)
    assert "future_skew" in future.flags
    late = cs.stamp("2026-03-01T08:00:00Z", now, last_source_at=datetime(2026, 3, 1, 9, tzinfo=UTC))
    assert "out_of_order" in late.flags
    bad = cs.stamp("last tuesday-ish", now)
    assert bad.source_at is None and "stamp_unparsed" in bad.flags


# ----------------------------------------------------------------- speaker / addressee / participants
async def test_multiparty_axes(tmp_path: Path) -> None:
    eng = await build(tmp_path / "m.db", stamping_v2=True)
    try:
        await eng.ingest_messages(GROUP, namespace=NS, session_id="s1")
        by = {r.content: r for r in await rows(eng)}
        a = by["Bram, I moved to Oslo"]
        assert (a.axes.speaker, a.axes.addressee) == ("ilse", "bram")
        assert a.axes.participants[:2] == ["ilse", "bram"]
        assert set(a.axes.participants) == {"ilse", "bram", "assistant"}
        b = by["Noor told me about it"]
        assert (b.axes.speaker, b.axes.addressee) == ("bram", "ilse")
        # the v1 tag is still written alongside the typed column
        assert {t for t in a.tags if t.startswith("participant:")} == {
            "participant:ilse",
            "participant:bram",
            "participant:assistant",
        }
        # an unnamed assistant turn has no speaker axis but is a participant
        assistant = by["Shall I note the Oslo address?"]
        assert assistant.axes.speaker is None and "assistant" in assistant.axes.participants
    finally:
        await eng.stop()


async def test_participant_tags_can_be_off(tmp_path: Path) -> None:
    eng = await build(tmp_path / "t.db", stamping_v2=True, stamp_participant_tags="none")
    try:
        await eng.ingest_messages(GROUP, namespace=NS, session_id="s1")
        for r in await rows(eng):
            assert r.axes.participants  # typed column still filled
            assert not any(t.startswith("participant:") for t in r.tags)
    finally:
        await eng.stop()


def test_role_stamper_and_helper() -> None:
    rs = RoleStamper()
    out = rs.stamp("user", "Noor: the boxes are green", session_speakers=["bram"])
    assert out.spk == "noor" and "speaker_from_prefix" in out.notes
    assert out.participants == ("bram", "noor")
    assert out.src_class == "user_stated"
    assert participants_of_call(
        [
            {"role": "user", "name": "Ilse"},
            {"role": "assistant"},
            {"role": "user", "speaker": "ilse"},
        ]
    ) == ["ilse", "assistant"]


async def test_axes_round_trip_through_storage(tmp_path: Path) -> None:
    db = tmp_path / "rt.db"
    eng = await build(db, stamping_v2=True)
    try:
        await eng.ingest_messages(GROUP, namespace=NS, session_id="s1")
        got = await rows(eng)
        ids = [r.record_id for r in got]
    finally:
        await eng.stop()
    eng2 = await build(db, stamping_v2=True)
    try:
        again = {r.record_id: r for r in await rows(eng2)}
        for r in got:
            assert again[r.record_id].axes == r.axes
            assert again[r.record_id].source_tz == r.source_tz
        assert len(again) == len(ids)
    finally:
        await eng2.stop()
    con = sqlite3.connect(db)
    try:
        cols = {c[1] for c in con.execute("pragma table_info(memory_records)")}
    finally:
        con.close()
    assert {"source_tz", "participants", "spk", "addr"} <= cols


def test_record_axes_dump_omits_empty_participants() -> None:
    assert "participants" not in RecordAxes(speaker="ilse").model_dump(mode="json")
    assert RecordAxes(participants=["ilse"]).model_dump(mode="json")["participants"] == ["ilse"]


# ---------------------------------------------------------------------- provenance class and assent
async def test_provenance_class_from_roles(tmp_path: Path) -> None:
    eng = await build(tmp_path / "p.db", stamping_v2=True)
    try:
        await eng.ingest_messages(
            [
                {"role": "user", "content": "I keep bees"},
                {"role": "assistant", "content": "You could try a top-bar hive"},
                {"role": "tool", "content": "weather: rain"},
            ],
            namespace=NS,
            session_id="s3",
            channel="messages",
        )
        got = {r.content: r for r in await rows(eng)}
        assert got["I keep bees"].provenance_class == "user_stated"
        assert got["You could try a top-bar hive"].provenance_class == "assistant_proposed"
    finally:
        await eng.stop()


async def test_assent_tag_only_for_real_assent(tmp_path: Path) -> None:
    eng = await build(tmp_path / "a.db", stamping_v2=True)
    try:
        await eng.ingest_messages(
            [
                {
                    "conversation_id": "assent-test",
                    "role": "assistant",
                    "content": "Shall I book the hive inspection for Friday?",
                },
                {"conversation_id": "assent-test", "role": "user", "content": "ok, thanks"},
                {
                    "conversation_id": "assent-test",
                    "role": "assistant",
                    "content": "Shall I also order three new frames for the hive?",
                },
                {
                    "conversation_id": "assent-test",
                    "role": "user",
                    "content": "yes please, order them",
                },
            ],
            namespace=NS,
            session_id="s4",
        )
        got = {r.content: r for r in await rows(eng)}
        ack = got["ok, thanks"]
        yes = got["yes please, order them"]
        proposal = got["Shall I also order three new frames for the hive?"]
        assert not any(t.startswith("assent_of:") for t in ack.tags)
        assert f"assent_of:{proposal.record_id}" in yes.tags
    finally:
        await eng.stop()


ASSENT = [
    "yes",
    "Yes please",
    "yeah, go ahead",
    "sure",
    "sounds good",
    "do it",
    "absolutely",
    "that's right",
    "sí, adelante",
    "claro",
    "de acuerdo",
    "oui, allez-y",
    "d'accord",
    "bien sûr",
    "ja, genau",
    "einverstanden",
    "sim, pode ser",
    "va bene",
    "haan, bilkul",
    "да, конечно",
    "好的",
    "はい",
    "네",
    "نعم",
    "yes, order them",
]
NOT_ASSENT = [
    "ok, thanks",
    "ok",
    "thanks",
    "got it",
    "I see",
    "noted",
    "understood",
    "mm",
    "cool",
    "gracias",
    "merci",
    "danke",
    "no",
    "no, don't",
    "nope",
    "never mind",
    "nein",
    "non merci",
    "actually, that's wrong",
    "yes, but make it Monday",
    "ja, aber lieber später",
    "sure, unless Noor objects",
    "what time is it?",
    "should I?",
    "",
    "the hive is green",
    "I was thinking about bees all week, and Tomasz told me about the frames while we painted the boxes yesterday, so yes",
]


@pytest.mark.parametrize("line", ASSENT)
def test_assent_set(line: str) -> None:
    v = AssentChecker().check(line, "Shall I book the hive inspection for Friday?")
    assert v.kind == "assent" and v.binds and v.confirms, (line, v)


@pytest.mark.parametrize("line", NOT_ASSENT)
def test_not_assent_set(line: str) -> None:
    v = AssentChecker().check(line, "Shall I book the hive inspection for Friday?")
    assert not v.confirms, (line, v)


def test_assent_without_proposal_does_not_bind() -> None:
    v = AssentChecker().check("yes please", None)
    assert v.kind == "assent" and not v.binds


def test_source_classifier() -> None:
    sc = SourceClassifier()
    assert sc.classify("user").src_class == "user_stated"
    assert sc.classify("assistant").src_class == "assistant_proposed"
    assert sc.classify("tool").src_class == "tool"
    chk = AssentChecker()
    prop = "Shall I book the hive inspection for Friday?"
    # a proposal item the user really assented to
    c = sc.classify_item("assistant", assent=chk.check("yes please", prop))
    assert c.src_class == "user_confirmed" and c.base_trust == sc.classify("user").base_trust
    # "ok, thanks" never promotes
    c2 = sc.classify_item("assistant", assent=chk.check("ok, thanks", prop))
    assert c2.src_class == "assistant_proposed"
    # a requested user_confirmed without a confirming verdict (or without a check) is downgraded
    d = sc.classify_item(
        "assistant", assent=chk.check("ok, thanks", prop), requested="user_confirmed"
    )
    assert d.src_class == "assistant_proposed" and d.downgraded
    d2 = sc.classify_item("assistant", assent=None, requested="user_confirmed")
    assert (
        d2.src_class == "assistant_proposed"
        and d2.downgraded
        and "assent_check_unavailable" in d2.reasons
    )
    # a user's own statement is never lowered
    assert sc.classify_item("user", requested="user_confirmed").src_class == "user_stated"


# ----------------------------------------------------------------------- asserter / subject check
def test_asserter_subject_mismatch_flagged() -> None:
    chk = AsserterSubjectCheck()
    cited = [CitedLine(speaker="Bram", text="Ilse moved to Oslo last week")]
    # Bram said Ilse moved: asserter Bram, subject Ilse -> fine
    ok = chk.check("Bram", "Ilse", cited)
    assert ok.ok and not ok.flags
    # the item claims Ilse asserted it: no cited line is Ilse's -> mismatch
    bad = chk.check("Ilse", "Ilse", cited)
    assert not bad.ok and "asserter_mismatch" in bad.flags
    # first person by Bram is about Bram, not Ilse
    fp = [CitedLine(speaker="Bram", text="I moved to Oslo last week")]
    mism = chk.check("Bram", "Ilse", fp)
    assert not mism.ok and "subject_mismatch_first_person" in mism.flags
    assert chk.check("Bram", "Bram", fp).ok
    # an attribution in the cited line supports a different asserter
    att = chk.check("Ilse", "Ilse", [CitedLine(speaker="Bram", text="Ilse said she moved to Oslo")])
    assert att.ok and "attributed" in att.flags
    # soft flag: subject appears nowhere in the cited lines
    soft = chk.check("Bram", "Tomasz", [CitedLine(speaker="Bram", text="the weather is mild")])
    assert soft.ok and "subject_not_in_cited" in soft.flags
    assert not chk.check("Bram", "Ilse", []).ok
    assert not chk.check(None, "Ilse", cited).ok


# ------------------------------------------------------------------------------------ gateway path
async def test_stamps_inside_the_gateway(tmp_path: Path) -> None:
    eng = await build(tmp_path / "g.db", stamping_v2=True)
    try:
        await eng.ingest_messages(GROUP, namespace=NS, session_id="s1")
        by = {r.content: r for r in await rows(eng)}
        first = by["Bram, I moved to Oslo"]
        assert first.source_tz == "Asia/Tokyo"
        assert first.axes.speaker == "ilse" and "bram" in first.axes.participants
        yes = by["yes please, note it"]
        prop = by["Shall I note the Oslo address?"]
        assert f"assent_of:{prop.record_id}" in yes.tags
        # a retry writes nothing new and stamps nothing twice
        again = await eng.ingest_messages(GROUP, namespace=NS, session_id="s1")
        assert len(await rows(eng)) == len(GROUP)
        assert [r.record_id for r in again] == [by[t["content"]].record_id for t in GROUP]
    finally:
        await eng.stop()


# ---------------------------------------------------------------------------------------- migration
def test_migration_head_and_columns(tmp_path: Path) -> None:
    from alembic.script import ScriptDirectory

    from memspine.services.storage.sqlite.migrations import alembic_config, upgrade_to_head

    script = ScriptDirectory.from_config(alembic_config(tmp_path / "x.db"))
    assert script.get_heads() == ["rm113_asset_registry"]
    bindings = script.get_revision("projector_bindings")
    assert bindings is not None and bindings.down_revision == "provider_changes"
    rev = script.get_revision("w202_stamping")
    assert rev is not None and rev.down_revision == "f02b_tri_temporal"
    db = tmp_path / "old.db"
    upgrade_to_head(db)
    con = sqlite3.connect(db)
    try:
        cols = {c[1] for c in con.execute("pragma table_info(memory_records)")}
    finally:
        con.close()
    assert {"source_tz", "participants"} <= cols
