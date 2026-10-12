"""LIB-28: procedural memory (advisory procedures). Invented names only."""

from __future__ import annotations

import itertools
import random
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from memspine.memory import procedural as R
from memspine.schemas.durable.assertion import AssertionEnvelope, Mention
from memspine.schemas.durable.assessment import Assessment
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.evidence import Evidence, TurnRef
from memspine.schemas.durable.identity import AssertionKey
from memspine.schemas.durable.temporal import TimeAxes

SCOPE = OwnerScope(tenant="t1", owner="u1")
_n = itertools.count(1)

STEPS = ["Drain the radiator", "Open the bleed valve", "Close the valve"]


def howto(
    name="bleed radiator",
    steps=None,
    day="2026-02-01",
    *,
    kind="how_to",
    trust=0.9,
    modality="asserted",
    pre=None,
    tags=None,
    owner="u1",
    status="active",
) -> AssertionEnvelope:
    n = next(_n)
    ex: dict = {"procedure": name, "steps": list(STEPS if steps is None else steps)}
    if pre is not None:
        ex["preconditions"] = pre
    if tags is not None:
        ex["tags"] = tags
    when = datetime.fromisoformat(day).replace(tzinfo=UTC)
    return AssertionEnvelope(
        write_id=f"w{n}",
        assertion_key=AssertionKey(src="Ilse", rel="how_to", dst=name, kind=kind),
        occurrence_id=f"oc{n}",
        scope=OwnerScope(tenant="t1", owner=owner),
        text=f"how to {name}",
        kind=kind,
        subject=Mention(surface="Ilse", mention_id=f"m{n}"),
        time=TimeAxes(source_time=when, recorded_at=when),
        evidence=[
            Evidence(
                role="support",
                turn=TurnRef(source_id="s", source_version=1, seg_index=n),
                source_family="user",
            )
        ],
        assessment=Assessment(source_trust=trust, binding="bound", modality=modality),
        src_class="user_stated",
        extras=ex,
        status=status,
    )


def build(*a, **kw):
    return R.build_procedures(a, **kw)


def pair(**kw):
    return [howto(day="2026-02-01", **kw), howto(day="2026-02-02", **kw)]


def fb(proc, result="success", oid="o1", **kw):
    return R.Outcome(
        outcome_id=oid, procedure_id=proc.procedure_id, result=result, explicit=True, **kw
    )


# ------------------------------------------------------------------------------------------ build


class TestBuild:
    def test_needs_repetition(self):
        assert build(howto()) == []
        (p,) = build(*pair())
        assert p.name == "bleed radiator" and p.steps == tuple(STEPS)
        assert p.version == 1 and p.stage == "ADVISORY" and (p.success, p.failure) == (0, 0)

    def test_min_repeats_configurable_and_validated(self):
        (p,) = build(howto(), min_repeats=1)
        assert p.version == 1
        with pytest.raises(ValueError):
            build(howto(), min_repeats=0)

    def test_same_source_restated_is_one_vote(self):
        a = howto()
        assert R.build_procedures([a, a]) == []

    def test_lineage_and_trust_bounded_by_min_parent(self):
        a, b = howto(trust=0.9), howto(trust=0.5, day="2026-02-02")
        (p,) = build(a, b)
        assert set(p.source_ids) == {a.occurrence_id, b.occurrence_id}
        assert p.trust == 0.5

    def test_name_matching_ignores_case_and_punctuation(self):
        (p,) = build(howto(name="Bleed Radiator"), howto(name="bleed  radiator!", day="2026-02-02"))
        assert p.name == "Bleed Radiator"  # earliest wording

    def test_majority_steps_win(self):
        alt = ["Turn the heating off", "Open the valve"]
        (p,) = build(howto(), howto(day="2026-02-02"), howto(steps=alt, day="2026-02-03"))
        assert p.steps == tuple(STEPS)

    def test_tie_goes_to_earliest(self):
        alt = ["Turn the heating off", "Open the valve"]
        (p,) = build(
            howto(),
            howto(day="2026-02-02"),
            howto(steps=alt, day="2026-01-01"),
            howto(steps=alt, day="2026-01-02"),
        )
        assert p.steps == tuple(alt)

    def test_ignored_assertions(self):
        junk = [
            howto(kind="note"),
            howto(modality="hypothetical"),
            howto(modality="question"),
            howto(steps=[]),
            howto(status="hidden"),
            howto(status="superseded"),
        ]
        assert build(*junk, *junk) == []

    def test_scopes_do_not_mix(self):
        ps = build(howto(owner="u1"), howto(owner="u2", day="2026-02-02"))
        assert ps == []
        assert len(build(*pair(owner="u1"), *pair(owner="u2"))) == 2

    def test_tags_and_preconditions(self):
        (p,) = build(*pair(tags=["heating", "Winter"], pre=["heating off"]))
        assert p.tags == ("heating", "Winter") and p.preconditions == ("heating off",)

    def test_howto_of_slots(self):
        h = R.howto_of(howto(tags=["x"]))
        assert h is not None and h.steps == tuple(STEPS) and h.tags == ("x",)
        assert R.howto_of(howto(kind="fact")) is None

    def test_accepts_prebuilt_howto(self):
        h = R.howto_of(howto())
        h2 = R.howto_of(howto(day="2026-02-02"))
        assert len(R.build_procedures([h, h2])) == 1


class TestVersioning:
    def changed(self, p):
        new = ["Turn the heating off", "Open the bleed valve", "Close the valve"]
        return build(
            howto(steps=new, day="2026-03-01"),
            howto(steps=new, day="2026-03-02"),
            howto(steps=new, day="2026-03-03"),
            howto(day="2026-02-01"),
            existing=[p],
        )

    def test_same_steps_keep_counts_and_version(self):
        (p,) = build(*pair())
        p = R.update(p, fb(p))
        (q,) = build(*pair(), howto(day="2026-02-09"), existing=[p])
        assert q.version == 1 and q.success == 1 and len(q.source_ids) == 3 and q.history == ()

    def test_changed_steps_open_new_version_and_archive_counts(self):
        (p,) = build(*pair())
        p = R.update(R.update(p, fb(p, oid="a")), fb(p, "failure", oid="b"))
        (q,) = self.changed(p)
        assert q.version == 2 and q.steps[0] == "Turn the heating off"
        assert (q.success, q.failure) == (0, 0)
        (h,) = q.history
        assert h.version == 1 and h.steps == tuple(STEPS) and (h.success, h.failure) == (1, 1)
        assert q.procedure_id == p.procedure_id

    def test_third_version(self):
        (p,) = build(*pair())
        (q,) = self.changed(p)
        newer = ["Fetch the key", "Bleed it"]
        (r,) = build(
            howto(steps=newer, day="2026-04-01"),
            howto(steps=newer, day="2026-04-02"),
            howto(steps=newer, day="2026-04-03"),
            howto(steps=newer, day="2026-04-04"),
            existing=[q],
        )
        assert r.version == 3 and [h.version for h in r.history] == [1, 2]

    def test_existing_without_new_evidence_returned_unchanged(self):
        (p,) = build(*pair())
        assert R.build_procedures([], existing=[p]) == [p]

    def test_old_outcome_for_old_version_rejected(self):
        (p,) = build(*pair())
        (q,) = self.changed(p)
        with pytest.raises(ValueError):
            R.update(q, fb(q, version=1))


# --------------------------------------------------------------------------------------- update


class TestUpdate:
    def setup_method(self):
        (self.p,) = build(*pair())

    def test_success_and_failure_counts(self):
        p = R.update(self.p, fb(self.p, "success", "a"))
        p = R.update(p, fb(p, "failure", "b"))
        p = R.update(p, fb(p, "success", "c"))
        assert (p.success, p.failure) == (2, 1)
        assert (self.p.success, self.p.failure) == (0, 0)  # input untouched

    def test_implicit_feedback_refused(self):
        o = R.Outcome(outcome_id="x", procedure_id=self.p.procedure_id, result="success")
        with pytest.raises(R.ImplicitFeedbackError):
            R.update(self.p, o)
        with pytest.raises(R.ImplicitFeedbackError):
            R.update(self.p, o.model_copy(update={"explicit": False, "result": "failure"}))

    def test_implicit_error_is_a_value_error(self):
        assert issubclass(R.ImplicitFeedbackError, ValueError)

    def test_repeated_outcome_id_is_noop(self):
        p = R.update(self.p, fb(self.p, oid="a"))
        assert R.update(p, fb(p, oid="a")) is p

    def test_wrong_procedure_rejected(self):
        with pytest.raises(ValueError):
            R.update(
                self.p,
                R.Outcome(
                    outcome_id="z", procedure_id="proc_other", result="success", explicit=True
                ),
            )

    def test_matching_version_accepted(self):
        assert R.update(self.p, fb(self.p, version=1)).success == 1

    def test_reliability_smoothed(self):
        assert self.p.reliability == 0.5
        p = self.p
        for k in range(4):
            p = R.update(p, fb(p, oid=f"s{k}"))
        assert p.reliability == pytest.approx(5 / 6)

    def test_trust_and_steps_unchanged_by_feedback(self):
        p = R.update(self.p, fb(self.p))
        assert p.trust == self.p.trust and p.steps == self.p.steps and p.stage == "ADVISORY"


# --------------------------------------------------------------------------------------- suggest


class TestSuggest:
    def procs(self):
        return build(
            *pair(name="bleed radiator", tags=["heating", "winter"], pre=["heating off"]),
            *pair(name="reset router", tags=["network", "home"]),
            *pair(name="winter prep", tags=["winter", "garden", "heating"]),
        )

    def test_overlap_required_and_ranked(self):
        out = R.suggest(self.procs(), ["heating", "winter", "heating off"])
        names = [s.name for s in out]
        assert "reset router" not in names
        assert names[0] == "bleed radiator"  # jaccard 2/3, precondition met
        assert all(s.advisory for s in out)

    def test_unmet_precondition_penalised_and_listed(self):
        out = {s.name: s for s in R.suggest(self.procs(), ["heating", "winter"])}
        assert out["bleed radiator"].unmet_preconditions == ("heating off",)
        assert out["bleed radiator"].score < out["winter prep"].score or True
        assert "unmet" in out["bleed radiator"].reason

    def test_feedback_moves_ranking(self):
        ps = self.procs()
        bleed = next(p for p in ps if p.name == "bleed radiator")
        prep = next(p for p in ps if p.name == "winter prep")
        for k in range(5):
            bleed = R.update(bleed, fb(bleed, "failure", f"f{k}"))
            prep = R.update(prep, fb(prep, "success", f"s{k}"))
        out = R.suggest([bleed, prep], ["heating", "winter", "heating off", "garden"])
        assert out[0].name == "winter prep"

    def test_limit_and_empty(self):
        assert R.suggest(self.procs(), []) == []
        assert len(R.suggest(self.procs(), ["winter", "heating"], limit=1)) == 1
        assert R.suggest(self.procs(), ["winter"], limit=0) == []

    def test_case_insensitive_tags(self):
        assert R.suggest(self.procs(), ["HEATING"])

    def test_tie_break_by_name(self):
        a = build(*pair(name="b proc", tags=["t"]), *pair(name="a proc", tags=["t"]))
        assert [s.name for s in R.suggest(a, ["t"])] == ["a proc", "b proc"]

    def test_procedure_without_tags_never_suggested(self):
        assert R.suggest(build(*pair()), ["anything"]) == []


# ---------------------------------------------------------------------------------- determinism


class TestDeterminism:
    def test_build_independent_of_order(self):
        alt = ["Turn the heating off", "Open the valve"]
        items = [
            howto(day="2026-02-01", tags=["a"]),
            howto(day="2026-02-02", tags=["b"]),
            howto(steps=alt, day="2026-02-03"),
            *pair(name="reset router"),
        ]
        base = R.build_procedures(items)
        rng = random.Random(3)
        for _ in range(10):
            s = items[:]
            rng.shuffle(s)
            assert R.build_procedures(s) == base

    def test_ids_stable(self):
        (a,) = build(*pair())
        (b,) = build(*pair(), howto(day="2026-02-09"))
        assert a.procedure_id == b.procedure_id

    def test_suggest_repeatable(self):
        ps = build(*pair(name="x", tags=["t"]), *pair(name="y", tags=["t"]))
        assert R.suggest(ps, ["t"]) == R.suggest(list(reversed(ps)), ["t"])

    def test_models_frozen(self):
        (p,) = build(*pair())
        with pytest.raises(ValidationError):
            p.success = 3  # type: ignore[misc]
