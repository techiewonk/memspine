"""W2-14: temporal extraction by every means (``write.temporal_v2``): anchor ladder, basis and precision,
relations without dates. Model-free: envelopes built directly, plus one window run with the stub window LLM.
All text is invented (Ilse, Bram, Noor, Tomasz)."""

from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_w2_06a_envelope import ItemLLM, _engine
from test_window_write import _facts, _send

from memspine.config.schema import MemspineConfig
from memspine.derive.envelope_stages import EnvelopeStageContext
from memspine.derive.temporal_stage import (
    EXTRAS_KEY,
    apply_temporal,
    audit_precision,
    find_explicit,
    find_relative,
    happened_label_for,
    resolve_text,
)
from memspine.read.legs.core import LegContext
from memspine.read.legs.temporal import TemporalLeg
from memspine.schemas.durable import (
    AssertionEnvelope,
    AssertionKey,
    Assessment,
    Evidence,
    Interval,
    OwnerScope,
    TemporalRef,
    TimeAxes,
    TurnRef,
    occurrence_id,
    write_id,
)
from memspine.schemas.durable.assertion import Mention
from memspine.schemas.durable.evidence import Span

SCOPE = OwnerScope(tenant="t", owner="o")
SRC = datetime(2023, 6, 15, 10, 0, tzinfo=UTC)  # a Thursday


def _env(
    rid: str,
    text: str,
    *,
    when: str | None = None,
    until: str | None = None,
    quote: str | None = None,
    turn: str = "r1",
) -> AssertionEnvelope:
    key = AssertionKey(src=rid, rel="r", dst="d", kind="event")
    start = TemporalRef(text=when) if when else None
    end = TemporalRef(text=until) if until else None
    return AssertionEnvelope(
        write_id=write_id(SCOPE, "c", 0, rid),
        assertion_key=key,
        occurrence_id=occurrence_id("e", [f"{turn}#0"], key),
        record_id=rid,
        scope=SCOPE,
        text=text,
        kind="event",
        evidence=[
            Evidence(
                role="support",
                turn=TurnRef(source_id=turn, source_version=1, seg_index=0),
                span=Span(start=0, end=len(quote), quote=quote) if quote else None,
                source_family="Ilse",
            )
        ],
        time=TimeAxes(event=Interval(start=start, end=end) if (start or end) else None),
        assessment=Assessment(
            source_trust=0.8, binding="bound", modality="asserted", time_precision="unknown"
        ),
        src_class="user_stated",
    )


def _ctx(source_at: datetime | None, *, valid_from: datetime | None = None) -> EnvelopeStageContext:
    rec = SimpleNamespace(
        record_id="r1",
        source_at=source_at,
        source_tz="UTC",
        valid_from=valid_from or datetime(2030, 1, 1, tzinfo=UTC),
    )
    return EnvelopeStageContext(
        namespace="a",
        window_id="w1",
        lines={},
        records=[rec],
        compile_ctx=SimpleNamespace(source_time=rec.valid_from),
    )


def _start(env: AssertionEnvelope) -> TemporalRef:
    assert env.time.event is not None and env.time.event.start is not None
    return env.time.event.start


# --- anchor ladder: relative phrases against a known source_at (de / fr / es / en) -----------------------
@pytest.mark.parametrize(
    "phrase",
    ["last Tuesday", "letzten Dienstag", "mardi dernier", "el martes pasado"],
)
def test_last_tuesday_resolves_against_the_turn_source_at(phrase: str) -> None:
    (out,) = apply_temporal([_env("a", "Ilse met Bram", when=phrase)], _ctx(SRC))
    ref = _start(out)
    assert ref.at is not None and ref.at.date() == date(2023, 6, 13)
    assert ref.precision == "day" and ref.basis == "source_time" and ref.anchor == SRC
    assert out.assessment.time_precision == "day" == out.time.event_precision
    assert (
        out.extras[EXTRAS_KEY]["rung"] == 2 and out.extras[EXTRAS_KEY]["basis_label"] == "relative"
    )


@pytest.mark.parametrize(
    ("phrase", "day", "prec"),
    [
        ("yesterday", date(2023, 6, 14), "day"),
        ("gestern", date(2023, 6, 14), "day"),
        ("hier", date(2023, 6, 14), "day"),
        ("ayer", date(2023, 6, 14), "day"),
        ("letzte Woche", date(2023, 6, 5), "week"),
        ("la semaine dernière", date(2023, 6, 5), "week"),
        ("la semana pasada", date(2023, 6, 5), "week"),
        ("il y a trois jours", date(2023, 6, 12), "day"),
        ("hace dos semanas", date(2023, 5, 29), "week"),
        ("vor zwei Monaten", date(2023, 4, 1), "month"),
        ("dentro de 2 semanas", date(2023, 6, 26), "week"),
    ],
)
def test_multilingual_relative_phrases(phrase: str, day: date, prec: str) -> None:
    f = find_relative(phrase, whole=True)
    assert f is not None, phrase
    res = resolve_text([(phrase, True)], turn_time=SRC, window_time=None, session_anchor=None)
    assert res is not None and res.rung == 2
    assert res.ref.precision == prec
    assert res.ref.at is not None and res.ref.at.date() == day


def test_relative_phrase_found_inside_a_longer_sentence() -> None:
    f = find_relative("Bram mi ha detto che sono partiti ayer sera")
    assert f is not None and f.candidate.rel == "ago"
    assert find_relative("Ilse flew out the day before yesterday") is not None
    assert (
        find_relative("Noor will order the morgen plan") is None
    )  # ambiguous single word, free text


def test_turn_time_beats_window_time_and_window_time_is_the_fallback() -> None:
    later = datetime(2023, 7, 20, 9, 0, tzinfo=UTC)
    (a,) = apply_temporal([_env("a", "x", when="yesterday")], _ctx(SRC))
    assert _start(a).at.date() == date(2023, 6, 14)  # type: ignore[union-attr]
    # the cited turn has no source_at, another turn of the window has one: the window source time
    rec_cited = SimpleNamespace(record_id="r1", source_at=None, source_tz="UTC", valid_from=SRC)
    rec_other = SimpleNamespace(record_id="r2", source_at=later, source_tz="UTC", valid_from=later)
    ctx = EnvelopeStageContext(
        namespace="a",
        window_id="w",
        lines={},
        records=[rec_cited, rec_other],
        compile_ctx=SimpleNamespace(source_time=SRC),
    )
    (b,) = apply_temporal([_env("b", "x", when="yesterday")], ctx)
    assert _start(b).at.date() == date(2023, 7, 19) and _start(b).anchor == later  # type: ignore[union-attr]


def test_no_source_at_gives_unknown_never_now() -> None:
    # stamping on (trust_valid_from False): valid_from is never read as a source time
    (out,) = apply_temporal([_env("a", "Ilse met Bram", when="last Tuesday")], _ctx(None))
    ref = _start(out)
    assert ref.at is None and ref.basis == "none" and ref.precision == "unknown"
    assert ref.text == "last Tuesday"
    assert out.assessment.time_precision == "unknown" and EXTRAS_KEY not in out.extras
    # the pure ladder agrees
    assert (
        resolve_text(
            [("last Tuesday", True)], turn_time=None, window_time=None, session_anchor=None
        )
        is None
    )


def test_session_anchor_rung_is_inferred_and_approximate_and_only_without_stamping() -> None:
    ctx = _ctx(None, valid_from=SRC)
    (out,) = apply_temporal([_env("a", "x", when="last Tuesday")], ctx, trust_valid_from=True)
    ref = _start(out)
    assert (
        ref.basis == "inferred"
        and ref.approximate
        and ref.at is not None
        and ref.at.date() == date(2023, 6, 13)
    )
    assert out.extras[EXTRAS_KEY]["rung"] == 3


# --- precision ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "prec", "label", "expected_at"),
    [
        ("in 2019", "year", "2019", date(2019, 1, 1)),
        ("im Jahr 2019", "year", "2019", date(2019, 1, 1)),
        ("en 2019", "year", "2019", date(2019, 1, 1)),
        ("2019", "year", "2019", date(2019, 1, 1)),
        ("March 2021", "month", "2021-03", date(2021, 3, 1)),
        ("mars 2021", "month", "2021-03", date(2021, 3, 1)),
        ("marzo de 2021", "month", "2021-03", date(2021, 3, 1)),
        ("2021-03", "month", "2021-03", date(2021, 3, 1)),
        ("3 March 2021", "day", "2021-03-03", date(2021, 3, 3)),
        ("March 3, 2021", "day", "2021-03-03", date(2021, 3, 3)),
        ("3. März 2021", "day", "2021-03-03", date(2021, 3, 3)),
        ("2021-03-03", "day", "2021-03-03", date(2021, 3, 3)),
        ("summer 2019", "season", "2019-06-01..2019-08-31", date(2019, 6, 1)),
    ],
)
def test_explicit_dates_keep_their_own_precision(
    text: str, prec: str, label: str, expected_at: date
) -> None:
    (out,) = apply_temporal([_env("a", "Ilse moved", when=text)], _ctx(SRC))
    ref = _start(out)
    assert ref.precision == prec and ref.basis == "explicit" and ref.anchor is None
    assert ref.at is not None and ref.at.date() == expected_at
    assert happened_label_for(ref) == label
    assert out.extras[EXTRAS_KEY]["rung"] == 1


def test_year_only_in_the_cited_span_is_year_precision_and_beats_a_relative_phrase() -> None:
    (out,) = apply_temporal(
        [_env("a", "Ilse moved to Oslo", quote="we moved to Oslo in 2019, long ago")], _ctx(SRC)
    )
    ref = _start(out)
    assert ref.precision == "year" and ref.at is not None and ref.at.month == 1 and ref.at.day == 1
    (out2,) = apply_temporal([_env("b", "x", when="yesterday", quote="we left in 2019")], _ctx(SRC))
    assert _start(out2).precision == "year"  # an explicit date outranks a relative phrase (rung 1)


def test_until_makes_a_closed_interval_and_since_stays_open() -> None:
    (out,) = apply_temporal([_env("a", "x", when="2018", until="2021")], _ctx(SRC))
    ev = out.time.event
    assert ev is not None and ev.start and ev.end and ev.end.precision == "year"
    assert happened_label_for(ev.start, ev.end) == "2018-01-01..2021-12-31"
    (open_,) = apply_temporal([_env("b", "x", when="since 2018")], _ctx(SRC))
    assert (
        open_.time.event is not None
        and open_.time.event.end is None
        and _start(open_).precision == "year"
    )


# --- the precision audit --------------------------------------------------------------------------------------
_AUDIT_TEXTS = [
    "in 2019",
    "2019",
    "March 2021",
    "3 March 2021",
    "2021-03-03",
    "summer 2019",
    "last week",
    "last month",
    "last year",
    "yesterday",
    "three days ago",
    "2 months ago",
    "vor zwei Jahren",
    "letzte Woche",
    "il y a deux ans",
    "hace tres meses",
    "la semaine prochaine",
    "mardi prochain",
    "next Friday",
    "in 3 weeks",
    "im Jahr 1998",
    "en 2005",
    "dentro de 2 meses",
]


@pytest.mark.parametrize("text", _AUDIT_TEXTS)
def test_precision_audit_no_output_is_finer_than_its_evidence(text: str) -> None:
    res = resolve_text([(text, True)], turn_time=SRC, window_time=None, session_anchor=None)
    assert res is not None, text
    assert audit_precision(res.ref, res.evidence_precision)
    # independent check from the words: a phrase naming a unit is never finer than that unit
    low = text.lower()
    if low.strip().isdigit() or low.startswith(("in 19", "in 20", "en 20", "im jahr 19")):
        assert res.ref.precision == "year"
    if any(w in low for w in ("week", "woche", "semaine", "semana")):
        assert res.ref.precision == "week"
    if any(w in low for w in ("month", "monat", "meses", "mois")):
        assert res.ref.precision == "month"


def test_precision_audit_catches_an_overclaim() -> None:
    year = TemporalRef(
        text="in 2019", at=datetime(2019, 1, 1, tzinfo=UTC), precision="year", basis="explicit"
    )
    day = TemporalRef(
        text="in 2019", at=datetime(2019, 1, 1, tzinfo=UTC), precision="day", basis="explicit"
    )
    assert audit_precision(year, "year") and not audit_precision(day, "year")


def test_every_stage_output_passes_the_audit() -> None:
    phrases = [*_AUDIT_TEXTS, "sometime", "when we were kids", "after dinner"]
    envs = [_env(f"e{i}", f"Ilse did thing {i}", when=p) for i, p in enumerate(phrases)]
    for out in apply_temporal(envs, _ctx(SRC)):
        ev = out.time.event
        meta = out.extras.get(EXTRAS_KEY)
        if ev is None or ev.start is None or ev.start.at is None:
            assert out.assessment.time_precision == "unknown"
            continue
        assert meta is not None and audit_precision(ev.start, meta["evidence_precision"])
        assert out.assessment.time_precision == ev.start.precision


def test_unparseable_text_is_kept_as_text_without_a_date() -> None:
    (out,) = apply_temporal([_env("a", "Ilse laughed", when="when we were kids")], _ctx(SRC))
    ref = _start(out)
    assert ref.at is None and ref.basis == "none" and ref.text == "when we were kids"
    assert find_explicit("no dates here, only 12 apples") is None


# --- relations without dates ----------------------------------------------------------------------------------
def test_the_week_before_her_trip_is_a_relation_and_no_date() -> None:
    trip = _env("trip", "Noor took a trip to Lisbon", turn="r1")
    shop = _env("shop", "Noor bought a suitcase", when="the week before her trip", turn="r1")
    out = apply_temporal([trip, shop], _ctx(SRC))
    got = out[1]
    ref = _start(got)
    assert ref.at is None and ref.basis == "relation" and ref.precision == "unknown"
    assert ref.relation is not None
    assert (
        ref.relation.relation == "before" and ref.relation.target_assertion_id == trip.occurrence_id
    )
    assert got.assessment.time_precision == "unknown"
    assert got.extras[EXTRAS_KEY]["relations"][0]["word"] == "before"
    assert happened_label_for(ref) is None  # no invented date reaches the store
    assert out[0].time.event is None


@pytest.mark.parametrize(
    ("phrase", "rel", "word"),
    [
        ("after the Lisbon trip", "after", "after"),
        ("während der Lisbon-Reise trip", "during", "during"),
        ("seit the trip", "after", "since"),
        ("until the trip", "before", "until"),
        ("avant son voyage trip", "before", "before"),
        ("despues de su viaje trip", "after", "after"),
    ],
)
def test_relation_words_since_until_after_during(phrase: str, rel: str, word: str) -> None:
    trip = _env("trip", "Noor took a trip to Lisbon", turn="r1")
    other = _env("o", "Bram painted the fence", when=phrase, turn="r1")
    got = apply_temporal([trip, other], _ctx(SRC))[1]
    ref = _start(got)
    assert ref.basis == "relation" and ref.relation is not None and ref.relation.relation == rel
    assert got.extras[EXTRAS_KEY]["relations"][0]["word"] == word


def test_a_relation_to_a_date_is_a_date_not_a_relation_and_no_target_is_not_guessed() -> None:
    (until,) = apply_temporal([_env("a", "x", when="until 2024")], _ctx(SRC))
    assert _start(until).basis == "explicit" and _start(until).precision == "year"
    (vor,) = apply_temporal([_env("b", "x", when="vor 3 Tagen")], _ctx(SRC))
    assert _start(vor).basis == "source_time"
    ctx = _ctx(SRC)
    (lonely,) = apply_temporal([_env("c", "Bram painted", when="the week before the wedding")], ctx)
    assert _start(lonely).at is None and _start(lonely).basis == "none"
    assert ctx.notes["temporal_relation_no_target"] == 1


def test_a_dated_assertion_keeps_its_date_and_lists_the_relation_in_the_audit_only() -> None:
    trip = _env("trip", "Noor took a trip to Lisbon", turn="r1")
    shop = _env(
        "shop", "Noor bought a suitcase", quote="bought it in 2022, before the trip", turn="r1"
    )
    got = apply_temporal([trip, shop], _ctx(SRC))[1]
    assert _start(got).basis == "explicit" and _start(got).precision == "year"
    assert got.extras[EXTRAS_KEY]["relations"][0]["relation"] == "before"


# --- read side: the temporal leg reads the written precision -------------------------------------------------
def _rec(rid: str, label: str, valid_from: datetime) -> Any:
    from memspine.store.records import MemoryRecord

    return MemoryRecord(
        record_id=rid,
        namespace="a",
        content=f"fact {rid}",
        layer="L1",
        memory_type="semantic",
        tags=[f"happened:{label}"],
        valid_from=valid_from,
    )


async def _run_leg(records: list[Any], query: str, *, aware: bool) -> list[tuple[str, float]]:
    ctx = LegContext(
        query=query,
        namespace="a",
        fetch_k=10,
        records=records,
        reference_time=datetime(2023, 6, 15, tzinfo=UTC),
        precision_aware=aware,
    )
    out = await TemporalLeg().run(ctx)
    return [(h.record_id, h.score) for h in out.hits]


async def test_the_temporal_leg_respects_the_stored_precision() -> None:
    year = _rec("year", "2019", datetime(2019, 1, 1, tzinfo=UTC))
    day = _rec("day", "2019-03-03", datetime(2019, 3, 3, tzinfo=UTC))
    other_year = _rec("other", "2020", datetime(2020, 1, 1, tzinfo=UTC))
    # the interval overlap is precision-correct even with the flag off: a year fact is in March, not in 2020
    hits = await _run_leg([year, day, other_year], "what happened on 2019-03-03?", aware=False)
    assert {h for h, _ in hits} == {"year", "day"}
    # a year-precision fact must not claim the exact day of its start
    hits = await _run_leg([year], "what happened on 2019-01-01?", aware=True)
    assert hits and hits[0][1] < 0.01
    # flag on: the day-precision fact outranks the year-precision one for a day question
    hits = await _run_leg([year, day], "what happened on 2019-03-03?", aware=True)
    assert [h for h, _ in hits] == ["day", "year"] and hits[0][1] > hits[1][1]
    # a year question: the year fact keeps full weight, the day fact is inside it
    hits = await _run_leg([year, day], "what happened in 2019?", aware=True)
    assert {h for h, _ in hits} == {"year", "day"}
    assert dict(hits)["year"] == pytest.approx(1.0)


async def test_precision_aware_off_is_the_old_ranking() -> None:
    year = _rec("year", "2019", datetime(2019, 1, 1, tzinfo=UTC))
    day = _rec("day", "2019-03-03", datetime(2019, 3, 3, tzinfo=UTC))
    a = await _run_leg([year, day], "what happened on 2019-03-03?", aware=False)
    assert all(0 < s <= 1.0 for _, s in a)
    assert MemspineConfig().read.temporal_precision_aware is False


# --- config + one window end to end ---------------------------------------------------------------------------
def test_flag_defaults_off() -> None:
    assert MemspineConfig().write.temporal_v2 is False


class DatedLLM(ItemLLM):
    """The window stub, but every item carries a model-written time phrase."""

    def __init__(self, whens: list[str | None]) -> None:
        super().__init__()
        self.mutations = {i: (lambda item, w=w: {**item, "when": w}) for i, w in enumerate(whens)}


async def test_window_end_to_end_with_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = DatedLLM(["last Tuesday", "in 2019", None, "the week before the zoo visit"])
    eng = await _engine(monkeypatch, llm, temporal_v2=True, stamping_v2=True)
    try:
        msgs = [
            {
                "role": "user",
                "content": "Ilse: she visited the zoo",
                "timestamp": "2023-06-15T10:00:00+00:00",
            },
            {
                "role": "user",
                "content": "Ilse: she moved house",
                "timestamp": "2023-06-15T10:01:00+00:00",
            },
            {
                "role": "user",
                "content": "Ilse: she likes tea",
                "timestamp": "2023-06-15T10:02:00+00:00",
            },
            {
                "role": "user",
                "content": "Ilse: she bought a zoo ticket",
                "timestamp": "2023-06-15T10:03:00+00:00",
            },
        ]
        await _send(eng, "a", "s1", msgs)
        stats = await eng.flush_windows()
        facts = await _facts(eng)
        assert len(facts) == 4 and stats["items_valid"] == 4
        tags = {
            f.content: [t for t in f.tags if t.startswith(("happened:", "said:"))] for f in facts
        }
        by = {k.split("said ", 1)[1]: v for k, v in tags.items()}
        assert "happened:2023-06-13" in by["she visited the zoo"]
        assert "happened:2019" in by["she moved house"]
        assert by["she likes tea"] == []  # no phrase: no date invented
        assert not any(
            t.startswith("happened:") for t in by["she bought a zoo ticket"]
        )  # a relation, no date
    finally:
        await eng.stop()


async def test_window_flag_off_keeps_the_text_only_behaviour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = DatedLLM(["last Tuesday", "in 2019", None, None])
    eng = await _engine(monkeypatch, llm)
    try:
        msgs = [
            {
                "role": "user",
                "content": f"Ilse: she did thing {i}",
                "timestamp": f"2023-06-15T10:0{i}:00+00:00",
            }
            for i in range(4)
        ]
        await _send(eng, "a", "s1", msgs)
        await eng.flush_windows()
        facts = await _facts(eng)
        assert len(facts) == 4 and not any(t.startswith("happened:") for f in facts for t in f.tags)
    finally:
        await eng.stop()


def test_json_roundtrip_of_a_written_envelope() -> None:
    (out,) = apply_temporal([_env("a", "x", when="in 2019")], _ctx(SRC))
    again = AssertionEnvelope.model_validate(json.loads(out.model_dump_json()))
    assert again.time.event_precision == "year" and Mention is not None
