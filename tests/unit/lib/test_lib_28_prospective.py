"""LIB-28: prospective memory (advisory reminders). Invented names and dates only."""

from __future__ import annotations

import itertools
import random
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from memspine.memory import prospective as P
from memspine.schemas.durable.assertion import AssertionEnvelope, Mention
from memspine.schemas.durable.assessment import Assessment
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.evidence import Evidence, TurnRef
from memspine.schemas.durable.identity import AssertionKey
from memspine.schemas.durable.temporal import Interval, TemporalRef, TimeAxes

SCOPE = OwnerScope(tenant="t1", owner="u1")
_n = itertools.count(1)


def dt(day: str, hour: int = 0) -> datetime:
    return datetime.fromisoformat(day).replace(hour=hour, tzinfo=UTC)


def ref(day: str, precision: str = "day", hour: int = 0) -> TemporalRef:
    return TemporalRef(at=dt(day, hour), precision=precision, basis="explicit")


def make(
    text: str,
    said: str,
    *,
    subject: str | None = "Aiko",
    modality: str = "planned",
    negated: bool = False,
    event: Interval | None = None,
    trust: float = 0.8,
    extras: dict | None = None,
    status: str = "active",
    speaker: str | None = None,
) -> AssertionEnvelope:
    n = next(_n)
    time = TimeAxes(event=event, source_time=dt(said), recorded_at=dt(said))
    return AssertionEnvelope(
        write_id=f"w{n}",
        assertion_key=AssertionKey(src=subject or "x", rel="plan", dst=text, kind="plan"),
        occurrence_id=f"oc{n}",
        scope=SCOPE,
        text=text,
        kind="plan",
        subject=Mention(surface=subject, mention_id=f"m{n}") if subject else None,
        speaker=speaker,
        time=time,
        evidence=[
            Evidence(
                role="support",
                turn=TurnRef(source_id="s", source_version=1, seg_index=n),
                source_family="user",
            )
        ],
        assessment=Assessment(
            source_trust=trust,
            binding="bound",
            modality=modality,
            negated=negated,
            time_precision=time.event_precision,
        ),
        src_class="user_stated",
        extras=extras or {},
        status=status,
    )


def on_day(day: str) -> Interval:
    return Interval(start=ref(day))


def plan(text="Aiko plans to visit the dentist", day="2026-03-10", said="2026-03-01", **kw):
    ev = on_day(day) if day else None
    return make(text, said, event=ev, **kw)


def derive(*items, now="2026-03-02"):
    return P.derive_intentions(items, now=dt(now))


# ------------------------------------------------------------------------------------------ derive


class TestDerive:
    def test_planned_becomes_tentative_intention_with_lineage(self):
        a = plan()
        (i,) = derive(a)
        assert i.status == "open" and i.tentative
        assert i.who == "Aiko" and i.parents == (a.occurrence_id,)
        assert i.trigger.starts_at == dt("2026-03-10")
        assert i.trigger.ends_at == dt("2026-03-11")  # day precision -> one-day window
        assert i.trigger.kind == "time"
        assert i.trust == 0.8

    @pytest.mark.parametrize(
        "modality", ["asserted", "hypothetical", "reported", "question", "negated"]
    )
    def test_other_modalities_make_no_intention(self, modality):
        assert derive(plan(modality=modality)) == []

    def test_planned_negated_is_not_an_intention(self):
        assert derive(plan(negated=True)) == []

    def test_hidden_and_superseded_skipped(self):
        assert derive(plan(status="hidden"), plan(status="superseded")) == []

    def test_restated_plan_merges_and_latest_trigger_wins(self):
        first = plan(day="2026-03-10", said="2026-03-01", trust=0.9)
        second = plan(day="2026-03-14", said="2026-03-05", trust=0.6)
        (i,) = derive(first, second, now="2026-03-06")
        assert i.trigger.starts_at == dt("2026-03-14")
        assert set(i.parents) == {first.occurrence_id, second.occurrence_id}
        assert i.created_at == dt("2026-03-01") and i.updated_at == dt("2026-03-05")
        assert i.trust == 0.6  # <= min over parents

    def test_trust_never_exceeds_min_parent(self):
        (i,) = derive(plan(trust=0.9, said="2026-03-02"), plan(trust=0.4, said="2026-03-01"))
        assert i.trust == 0.4

    def test_different_subjects_stay_separate(self):
        a = plan("Aiko plans to call the plumber")
        b = plan("Bram plans to call the plumber", subject="Bram")
        assert len(derive(a, b)) == 2

    def test_cues_read_from_extras_and_cue_of(self):
        a = plan(
            day=None, extras={"event_cue": "when Bram calls", "location_cue": "at the pharmacy"}
        )
        (i,) = derive(a)
        assert i.trigger.event_cue == "when Bram calls"
        assert i.trigger.location_cue == "at the pharmacy"
        assert i.trigger.kind == "cue"
        (j,) = P.derive_intentions(
            [plan(day=None)], now=dt("2026-03-02"), cue_of=lambda _a: (None, "at the market")
        )
        assert j.trigger.location_cue == "at the market"

    def test_unresolved_interval_end_gives_no_time(self):
        a = plan(day=None)
        (i,) = derive(a)
        assert i.trigger.kind == "none"

    def test_deadline_only_interval(self):
        a = make(
            "Noor plans to file taxes",
            "2026-03-01",
            subject="Noor",
            event=Interval(end=ref("2026-04-15")),
        )
        (i,) = derive(a)
        assert i.trigger.starts_at is None and i.trigger.ends_at == dt("2026-04-16")

    def test_requires_aware_now(self):
        with pytest.raises(ValueError):
            P.derive_intentions([], now=datetime(2026, 3, 1))

    def test_no_timestamp_falls_back_to_now(self):
        a = plan()
        a = a.model_copy(update={"time": TimeAxes(event=a.time.event)})
        (i,) = derive(a, now="2026-03-02")
        assert i.created_at == dt("2026-03-02")

    def test_speaker_used_when_no_subject(self):
        (i,) = derive(plan("I will renew the passport", subject=None, speaker="Tomasz"))
        assert i.who == "Tomasz"


# ----------------------------------------------------------------------------------------- due


class TestDue:
    def setup_method(self):
        self.i = derive(plan())[0]  # window 2026-03-10 .. 2026-03-11

    def test_in_window(self):
        (r,) = P.due([self.i], dt("2026-03-10", 9))
        assert r.reasons == ("in_window",) and r.advisory and r.tentative
        assert r.parents == self.i.parents and r.trust == self.i.trust

    def test_upcoming_within_lead_only(self):
        assert P.due([self.i], dt("2026-03-09", 12))[0].reasons == ("upcoming",)
        assert P.due([self.i], dt("2026-03-08", 12)) == []

    def test_window_start_inclusive_end_exclusive(self):
        assert P.due([self.i], dt("2026-03-10"))[0].reasons == ("in_window",)
        assert P.due([self.i], dt("2026-03-11"))[0].reasons == ("overdue",)

    def test_overdue_then_expired(self):
        assert P.due([self.i], dt("2026-03-15"))[0].reasons == ("overdue",)
        assert P.due([self.i], dt("2026-03-19")) == []  # end 03-11 + 7d grace

    def test_deadline_only(self):
        a = make(
            "Noor plans to file taxes",
            "2026-03-01",
            subject="Noor",
            event=Interval(end=ref("2026-04-15")),
        )
        (i,) = derive(a)
        assert P.due([i], dt("2026-04-01")) == []
        assert P.due([i], dt("2026-04-16") - timedelta(hours=3))[0].reasons == ("deadline_near",)
        assert P.due([i], dt("2026-04-17"))[0].reasons == ("overdue",)

    def test_hour_precision_window(self):
        ev = Interval(start=ref("2026-03-10", "hour", 14))
        (i,) = derive(make("Ilse plans to phone the bank", "2026-03-01", subject="Ilse", event=ev))
        assert P.due([i], dt("2026-03-10", 14))[0].reasons == ("in_window",)
        assert P.due([i], dt("2026-03-10", 15))[0].reasons == ("overdue",)

    def test_cue_match_event_and_location(self):
        a = plan(day=None, extras={"event_cue": "when Bram calls", "location_cue": "pharmacy"})
        (i,) = derive(a)
        (r,) = P.due([i], dt("2026-03-03"), cues=["Bram calls about dinner"])
        assert r.reasons == ("cue_match",) and r.due_at is None
        (r2,) = P.due([i], dt("2026-03-03"), cues=["walking past the Pharmacy on Main St"])
        assert any("location cue" in d for d in r2.details)
        assert P.due([i], dt("2026-03-03"), cues=["Tomasz calls"]) == []
        assert P.due([i], dt("2026-03-03")) == []

    def test_time_and_cue_combine_in_priority_order(self):
        a = plan(extras={"event_cue": "dentist reminder"})
        (i,) = derive(a)
        (r,) = P.due([i], dt("2026-03-10", 8), cues=["the dentist reminder came in"])
        assert r.reasons == ("in_window", "cue_match") and len(r.details) == 2

    def test_cue_only_plan_expires_after_ttl(self):
        a = plan(day=None, said="2026-03-01", extras={"event_cue": "bram calls"})
        (i,) = derive(a)
        assert P.due([i], dt("2026-05-29"), cues=["bram calls"])
        assert P.due([i], dt("2026-06-01"), cues=["bram calls"]) == []

    def test_resolved_never_surface(self):
        for status in ("done", "cancelled", "expired"):
            j = self.i.model_copy(update={"status": status})
            assert P.due([j], dt("2026-03-10", 9)) == []

    def test_ordering_urgency_then_time_then_id(self):
        a = plan("Aiko plans to visit the dentist", day="2026-03-10")
        b = plan("Bram plans to fix the bike", day="2026-03-09", subject="Bram")
        c = plan("Noor plans to renew the lease", day="2026-03-10", subject="Noor")
        items = derive(a, b, c)
        reasons = P.due(items, dt("2026-03-10", 6))
        assert [r.reasons for r in reasons] == [("in_window",), ("in_window",), ("overdue",)]
        assert reasons[2].who == "Bram"  # window closed on 03-10, within grace
        assert {r.who for r in reasons[:2]} == {"Aiko", "Noor"}
        assert [r.intention_id for r in reasons[:2]] == sorted(r.intention_id for r in reasons[:2])

    def test_requires_aware_now(self):
        with pytest.raises(ValueError):
            P.due([self.i], datetime(2026, 3, 10))

    def test_custom_rules(self):
        rules = P.ProspectiveRules(lead=timedelta(days=5))
        assert P.due([self.i], dt("2026-03-06"), rules=rules)[0].reasons == ("upcoming",)


# ------------------------------------------------------------------------ completion / cancellation


class TestResolution:
    def setup_method(self):
        self.i = derive(plan())[0]

    def resolve(self, *later):
        return P.detect_resolution([self.i], later, now=dt("2026-03-20"))[0]

    def test_asserted_match_is_done(self):
        a = make("Aiko went to the dentist", "2026-03-10", modality="asserted")
        r = self.resolve(a)
        assert r.status == "done" and r.resolved_by == a.occurrence_id
        assert r.resolved_at == dt("2026-03-10")

    def test_negated_match_is_cancelled(self):
        r = self.resolve(make("Aiko is not visiting the dentist", "2026-03-05", modality="negated"))
        assert r.status == "cancelled"

    def test_planned_negated_is_cancelled(self):
        r = self.resolve(make("Aiko won't visit the dentist", "2026-03-05", negated=True))
        assert r.status == "cancelled"

    def test_cancel_word_in_asserted_text(self):
        r = self.resolve(make("Aiko cancelled the dentist", "2026-03-05", modality="asserted"))
        assert r.status == "cancelled"

    def test_different_subject_does_not_close(self):
        assert (
            self.resolve(
                make("Bram went to the dentist", "2026-03-10", subject="Bram", modality="asserted")
            ).status
            == "open"
        )

    def test_unrelated_content_does_not_close(self):
        assert (
            self.resolve(make("Aiko bought a bicycle", "2026-03-10", modality="asserted")).status
            == "open"
        )

    def test_subject_only_overlap_does_not_close(self):
        assert (
            self.resolve(make("Aiko is tired", "2026-03-10", modality="asserted")).status == "open"
        )

    @pytest.mark.parametrize("modality", ["hypothetical", "reported", "question"])
    def test_non_committal_modalities_ignored(self, modality):
        assert (
            self.resolve(make("Aiko went to the dentist", "2026-03-10", modality=modality)).status
            == "open"
        )

    def test_earlier_or_same_time_assertion_ignored(self):
        same = make("Aiko went to the dentist", "2026-03-01", modality="asserted")
        before = make("Aiko went to the dentist", "2026-02-20", modality="asserted")
        assert self.resolve(same, before).status == "open"

    def test_source_assertion_cannot_close_itself(self):
        a = plan(said="2026-03-01")
        (i,) = derive(a)
        r = P.detect_resolution(
            [i],
            [
                a.model_copy(
                    update={"time": a.time.model_copy(update={"source_time": dt("2026-03-09")})}
                )
            ],
            now=dt("2026-03-20"),
        )
        assert r[0].status == "open"

    def test_earliest_qualifying_assertion_wins_regardless_of_order(self):
        cancel = make("Aiko is not visiting the dentist", "2026-03-05", modality="negated")
        done = make("Aiko went to the dentist", "2026-03-10", modality="asserted")
        assert self.resolve(done, cancel).status == "cancelled"
        assert self.resolve(cancel, done).status == "cancelled"

    def test_longer_mention_of_same_person(self):
        r = self.resolve(
            make(
                "Aiko Tanaka went to the dentist",
                "2026-03-10",
                subject="Aiko Tanaka",
                modality="asserted",
            )
        )
        assert r.status == "done"

    def test_closed_intentions_untouched_and_order_kept(self):
        other = derive(plan("Bram plans to fix the bike", subject="Bram"))[0]
        closed = other.model_copy(update={"status": "cancelled"})
        out = P.detect_resolution(
            [closed, self.i],
            [make("Bram fixed the bike", "2026-03-10", subject="Bram", modality="asserted")],
            now=dt("2026-03-20"),
        )
        assert out[0] is closed and out[1].status == "open"

    def test_hidden_assertions_ignored(self):
        a = make("Aiko went to the dentist", "2026-03-10", modality="asserted", status="hidden")
        assert self.resolve(a).status == "open"

    def test_no_time_based_resolution(self):
        assert P.detect_resolution([self.i], [], now=dt("2030-01-01"))[0].status == "open"


# ----------------------------------------------------------------------------------------- expiry


class TestExpiry:
    def test_window_expires_after_grace(self):
        i = derive(plan())[0]
        assert P.expire([i], dt("2026-03-17", 23))[0].status == "open"
        assert P.expire([i], dt("2026-03-18"))[0].status == "expired"

    def test_no_trigger_ttl(self):
        i = derive(plan(day=None, said="2026-03-01"))[0]
        assert P.effective_status(i, dt("2026-03-30")) == "open"
        assert P.effective_status(i, dt("2026-03-31")) == "expired"

    def test_done_and_cancelled_never_expire(self):
        i = derive(plan())[0]
        for s in ("done", "cancelled"):
            j = i.model_copy(update={"status": s})
            assert P.expire([j], dt("2030-01-01"))[0].status == s

    def test_expire_does_not_mutate_input(self):
        i = derive(plan())[0]
        P.expire([i], dt("2030-01-01"))
        assert i.status == "open"


# ------------------------------------------------------------------------------------ determinism


class TestDeterminism:
    def test_derive_independent_of_input_order(self):
        items = [
            plan("Aiko plans to visit the dentist", day="2026-03-10", said="2026-03-01"),
            plan("Aiko plans to visit the dentist", day="2026-03-12", said="2026-03-02"),
            plan("Bram plans to fix the bike", day="2026-03-09", subject="Bram", said="2026-03-01"),
            plan(
                "Noor plans to renew the lease", day="2026-03-20", subject="Noor", said="2026-03-01"
            ),
        ]
        base = P.derive_intentions(items, now=dt("2026-03-05"))
        rng = random.Random(7)
        for _ in range(10):
            shuffled = items[:]
            rng.shuffle(shuffled)
            assert P.derive_intentions(shuffled, now=dt("2026-03-05")) == base

    def test_due_repeatable_and_input_order_independent(self):
        items = derive(
            plan("Aiko plans to visit the dentist", day="2026-03-10"),
            plan("Bram plans to fix the bike", day="2026-03-10", subject="Bram"),
            plan("Noor plans to renew the lease", day="2026-03-10", subject="Noor"),
        )
        a = P.due(items, dt("2026-03-10", 5))
        assert a == P.due(items, dt("2026-03-10", 5))
        assert a == P.due(list(reversed(items)), dt("2026-03-10", 5))

    def test_ids_stable_across_added_sources(self):
        (a,) = derive(plan(said="2026-03-01"))
        (b,) = derive(plan(said="2026-03-01"), plan(said="2026-03-02"))
        assert a.intention_id == b.intention_id

    def test_intention_is_frozen(self):
        i = derive(plan())[0]
        with pytest.raises(ValidationError):
            i.status = "done"  # type: ignore[misc]
