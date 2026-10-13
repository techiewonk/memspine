"""OW-E: O4-10 prospective / procedural memory and O4-11 explicit feedback / observer views.

Every behaviour is behind a flag that defaults off. Model-free: hash embedder, no LLM roles.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import WriteConfig
from memspine.derive.pipeline import advisory
from memspine.derive.pipeline.stages.watches import check_watches
from memspine.exceptions import MemspineError
from memspine.memories.procedural.skills import make_skill_record
from memspine.observability import tap
from memspine.store.records import MemoryRecord

T0 = datetime(2024, 5, 1, 9, 0, tzinfo=UTC)
PLAN_TAGS = ["mod:plan", "spk:caroline", "sub:caroline"]


@pytest.fixture(autouse=True)
def _tap_off() -> Any:
    tap.shutdown()
    yield
    tap.shutdown()


def _tap_on() -> None:
    from memspine.config.schema.observability import TapConfig

    tap.configure(TapConfig(enabled=True, sink="memory"))


def _events(name: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def walk(trace: dict[str, Any]) -> None:
        out.extend(e["fields"] for e in trace["events"] if e["stage"] == name)
        for child in trace.get("children", []):
            walk(child)

    for t in tap.memory_traces():
        walk(t)
    return out


def _engine(memories: dict[str, Any] | None = None, write: dict[str, Any] | None = None) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}, **(memories or {})},
        write=write or {},
        read={"hybrid": False},
    )


# ------------------------------------------------------------------------- flags default off


def test_flags_default_off() -> None:
    cfg = WriteConfig()
    assert cfg.feedback_v2 is False
    assert cfg.feedback_operators == []


async def test_policy_flags_off_by_default() -> None:
    eng = _engine({"prospective": {"enabled": True}, "procedural": {"enabled": True}})
    await eng.start()
    try:
        assert not advisory.prospective_v2_on(eng._config())
        assert not advisory.procedural_v2_on(eng._config())
        with pytest.raises(MemspineError, match=r"policies\.v2"):
            await eng.reminders("a")
        with pytest.raises(MemspineError, match=r"policies\.v2"):
            await eng.suggest_procedures(["x"], "a")
    finally:
        await eng.stop()


# ----------------------------------------------------------------------------- O4-10 prospective


def _rec(content: str, tags: list[str], valid_from: datetime, ns: str = "a") -> MemoryRecord:
    return MemoryRecord(
        namespace=ns, memory_type="semantic", content=content, tags=tags, valid_from=valid_from
    )


def test_record_assertion_modalities() -> None:
    plan = advisory.record_assertion(_rec("Caroline will go", PLAN_TAGS, T0))
    assert plan is not None and plan.assessment.modality == "planned"
    cancel = advisory.record_assertion(_rec("Caroline won't go", [*PLAN_TAGS, "pol:neg"], T0))
    assert cancel is not None and cancel.assessment.negated
    fact = advisory.record_assertion(_rec("Caroline went", ["mod:fact", "spk:caroline"], T0))
    assert fact is not None and fact.assessment.modality == "asserted"
    # hypothetical, mixed plan+fact, and bookkeeping records are not evidence
    assert advisory.record_assertion(_rec("maybe", ["mod:hypo"], T0)) is None
    assert advisory.record_assertion(_rec("both", ["mod:plan", "mod:fact"], T0)) is None
    watch = _rec("w", PLAN_TAGS, T0).model_copy(update={"memory_type": "prospective"})
    assert advisory.record_assertion(watch) is None


def test_prospective_report_window_done_and_expired() -> None:
    plan = _rec(
        "Caroline will visit the pharmacy tomorrow",
        [*PLAN_TAGS, "due_from:2024-05-02", "due_to:2024-05-02"],
        T0,
    )
    day = datetime(2024, 5, 2, 10, tzinfo=UTC)
    report = advisory.prospective_report([plan], day)
    assert [r.reasons for r in report.reminders] == [("in_window",)]
    assert report.reminders[0].advisory is True
    assert report.reminders[0].parents == (plan.record_id,)
    assert report.reminders[0].trust <= plan.trust

    later = _rec(
        "Caroline visited the pharmacy",
        ["mod:fact", "spk:caroline", "sub:caroline"],
        T0 + timedelta(days=2),
    )
    done = advisory.prospective_report([plan, later], day + timedelta(days=2))
    assert done.reminders == [] and done.done == 1

    cancel = _rec(
        "Caroline won't visit the pharmacy", [*PLAN_TAGS, "pol:neg"], T0 + timedelta(days=2)
    )
    cancelled = advisory.prospective_report([plan, cancel], day + timedelta(days=2))
    assert cancelled.cancelled == 1 and cancelled.reminders == []

    expired = advisory.prospective_report([plan], day + timedelta(days=30))
    assert expired.expired == 1 and expired.reminders == []


def test_prospective_cue_match() -> None:
    plan = _rec("Caroline will call Bram", PLAN_TAGS, T0)
    plan = plan.model_copy(update={"tags": PLAN_TAGS})
    # cue plans read the cue from extras, which a stored record does not carry: no cue, no reminder
    assert (
        advisory.prospective_report([plan], T0 + timedelta(hours=1), ["Bram calls"]).reminders == []
    )


async def test_reminders_verb_and_sweep_tap() -> None:
    eng = _engine(
        {"prospective": {"enabled": True, "policies": {"v2": True}}},
    )
    await eng.start()
    try:
        _tap_on()
        await eng.write(
            "Caroline will visit the pharmacy tomorrow",
            namespace="a",
            tags=[*PLAN_TAGS, "due_from:2024-05-02", "due_to:2024-05-02"],
            valid_from=T0,
        )
        out = await eng.reminders("a", now=datetime(2024, 5, 2, 10, tzinfo=UTC))
        assert len(out) == 1 and out[0].reasons == ("in_window",)
        with pytest.raises(Exception, match="timezone-aware"):
            await eng.reminders("a", now=datetime(2024, 5, 2, 10))

        await eng.write(
            "Caroline visited the pharmacy",
            namespace="a",
            tags=["mod:fact", "spk:caroline", "sub:caroline"],
            valid_from=T0 + timedelta(days=1, hours=8),
        )
        assert await eng.reminders("a", now=datetime(2024, 5, 2, 12, tzinfo=UTC)) == []

        stats = await eng.sleep()
        assert stats["check_watches"]["intentions_intentions"] == 1
        sweeps = _events("job.prospective.sweep")
        assert sweeps and sweeps[-1]["intentions"] == 1
        assert sweeps[-1]["done"] == 1
        assert any(e["status"] == "done" for e in _events("job.prospective.closed"))
        assert _events("job.prospective.read")
    finally:
        await eng.stop()


async def test_check_watches_stats_unchanged_flag_off() -> None:
    eng = _engine({"prospective": {"enabled": True}})
    await eng.start()
    try:
        await eng.write("Caroline will go", namespace="a", tags=PLAN_TAGS, valid_from=T0)
        stats = await eng.sleep()
        assert set(stats["check_watches"]) == {"status", "fired", "due", "invalidated"}
    finally:
        await eng.stop()


# ----------------------------------------------------------------------------- O4-10 procedural


def _skill(name: str, content: str, kind: str, tags: list[str], ns: str = "a") -> MemoryRecord:
    rec = make_skill_record(ns, name, content, kind=kind)  # type: ignore[arg-type]
    return rec.model_copy(update={"tags": tags})


def test_howto_split_and_build_versions() -> None:
    a = _skill("Deploy", "1. build\n2. ship", "skill", ["release"])
    b = _skill("deploy", "build -> ship", "plan", ["release"])
    howtos = [h for h in (advisory.howto_of_record(r) for r in (a, b)) if h]
    assert [h.steps for h in howtos] == [("build", "ship"), ("build", "ship")]
    procs = advisory.build_record_procedures(howtos)
    assert len(procs) == 1 and procs[0].version == 1 and procs[0].stage == "ADVISORY"
    assert set(procs[0].source_ids) == {a.record_id, b.record_id}
    assert procs[0].trust <= min(a.trust, b.trust)
    # a single source is not a procedure yet
    assert advisory.build_record_procedures(howtos[:1]) == []

    # three agreeing statements of new steps outvote the old pair: version 2, old counts in history
    later = [
        _skill("deploy", "build -> test -> ship", "plan", ["release"]).model_copy(
            update={"recorded_at": T0 + timedelta(days=i)}
        )
        for i in range(1, 4)
    ]
    older = [r.model_copy(update={"recorded_at": T0 - timedelta(days=9)}) for r in (a, b)]
    hs = [h for h in (advisory.howto_of_record(r) for r in (*older, *later)) if h]
    v = advisory.build_record_procedures(hs)
    assert len(v) == 1 and v[0].version == 2 and v[0].steps == ("build", "test", "ship")
    assert [x.version for x in v[0].history] == [1]


def test_howto_ignores_deprecated_and_lessons() -> None:
    from memspine.store.records import SkillStage

    dep = _skill("x", "a\nb", "skill", [])
    dep = dep.model_copy(update={"skill_stage": SkillStage.DEPRECATED})
    assert advisory.howto_of_record(dep) is None
    lesson = make_skill_record("a", "t", "do this", kind="lesson")
    assert advisory.howto_of_record(lesson) is None


async def test_procedures_explicit_feedback_only_and_suggest() -> None:
    eng = _engine({"procedural": {"enabled": True, "policies": {"v2": True}}})
    await eng.start()
    try:
        _tap_on()
        recs = [
            await eng._write_procedural(
                _skill("Deploy", "1. build\n2. ship", "skill", ["release"]), "user"
            ),
            await eng._write_procedural(
                _skill("Deploy", "1. build\n2. ship", "plan", ["release"]), "user"
            ),
        ]
        out = await eng.suggest_procedures(["release"], "a")
        assert len(out) == 1 and out[0].advisory is True and out[0].reliability == 0.5

        await eng.feedback(recs[0].record_id, "like", actor="user", namespace="a")  # explicit
        # an outcome receipt is the engine's own signal: implicit, refused
        await eng.record_outcome(
            "deploy", "success", "a", used_ids=[recs[1].record_id], lesson=False
        )
        out = await eng.suggest_procedures(["release"], "a")
        assert out[0].reliability == pytest.approx(2 / 3)  # one explicit success only
        refused = _events("job.procedural.update_refused")
        assert any(e["reason"] == "implicit" for e in refused)
        built = _events("job.procedural.built")
        assert built and built[-1]["success"] == 1 and built[-1]["failure"] == 0

        stats = await eng.sleep()
        assert stats["check_watches"]["procedures_procedures"] == 1
        assert stats["check_watches"]["procedures_implicit_refused"] == 1
    finally:
        await eng.stop()


# --------------------------------------------------------------------------- O4-11 feedback


async def test_feedback_keywords_refused_flag_off() -> None:
    eng = _engine()
    await eng.start()
    try:
        r = await eng.write(
            "Caroline lives in Paris", namespace="a", entity="Caroline", attribute="city"
        )
        with pytest.raises(MemspineError, match="feedback_v2"):
            await eng.feedback(r.record_id, "like", namespace="a", channel="api")
        with pytest.raises(MemspineError, match="like, dislike or note"):
            await eng.feedback(r.record_id, "correction", namespace="a")
        liked = await eng.feedback(r.record_id, "like", namespace="a")
        assert liked.scoring.likes == 1
        with pytest.raises(MemspineError, match="feedback_v2"):
            await eng.correction_latency("a")
    finally:
        await eng.stop()


async def test_feedback_v2_refuses_implicit_and_non_owners() -> None:
    eng = _engine(write={"feedback_v2": True, "feedback_operators": ["ops"]})
    await eng.start()
    try:
        _tap_on()
        r = await eng.write(
            "Caroline lives in Paris", namespace="a", entity="Caroline", attribute="city"
        )
        for channel in ("dwell", "regeneration", "classifier"):
            with pytest.raises(MemspineError, match="implicit_channel"):
                await eng.feedback(r.record_id, "like", namespace="a", channel=channel)
        with pytest.raises(MemspineError, match="unknown_channel"):
            await eng.feedback(r.record_id, "like", namespace="a", channel="telepathy")
        with pytest.raises(MemspineError, match="not_permitted"):
            await eng.feedback(r.record_id, "like", namespace="a", actor="assistant")
        with pytest.raises(MemspineError, match="not_permitted"):
            await eng.feedback(r.record_id, "like", namespace="a", actor="mallory")
        with pytest.raises(MemspineError, match="not_permitted"):  # an operator cannot confirm
            await eng.feedback(r.record_id, "confirmation", namespace="a", actor="ops")
        with pytest.raises(MemspineError, match="invalid"):
            await eng.feedback(r.record_id, "correction", namespace="a")  # no new_value
        refused = [e for e in _events("api.feedback") if e["accepted"] is False]
        assert {e["reason"] for e in refused} >= {
            "implicit_channel", "unknown_channel", "not_permitted", "invalid"
        }  # fmt: skip
        ok = await eng.feedback(r.record_id, "like", namespace="a", channel="ui_control")
        assert ok.scoring.likes == 1
        assert [e for e in _events("api.feedback") if e["accepted"]][-1]["kind"] == "like"
    finally:
        await eng.stop()


async def test_feedback_v2_correction_goes_through_the_door_and_never_edits() -> None:
    eng = _engine(write={"feedback_v2": True, "feedback_operators": ["ops"]})
    await eng.start()
    try:
        _tap_on()
        old = await eng.write(
            "Caroline lives in Paris", namespace="a", entity="Caroline", attribute="city"
        )
        new = await eng.feedback(
            old.record_id,
            "correction",
            namespace="a",
            new_value="Berlin",
            new_text="Caroline lives in Berlin",
            quote="no, Berlin",
        )
        assert new.record_id != old.record_id
        assert new.content == "Caroline lives in Berlin"
        assert "feedback:correction" in new.tags and new.source.channel == "feedback"
        kept = await eng._require_started().get_record(old.record_id)
        assert kept is not None and kept.content == "Caroline lives in Paris"  # never edited
        ev = [e for e in _events("api.feedback") if e["accepted"] and e["kind"] == "correction"]
        assert ev and ev[-1]["assertions"] == 1 and ev[-1]["written"] == [new.record_id]

        rej = await eng.feedback(
            new.record_id, "rejection", namespace="a", actor="ops", channel="operator_edit"
        )
        assert rej.content.startswith("not: ") and "pol:neg" in rej.tags

        # a restatement joins the record it restates (the door merges the duplicate)
        conf = await eng.feedback(rej.record_id, "confirmation", namespace="a")
        assert conf.record_id == rej.record_id
        assert [e for e in _events("api.feedback") if e["kind"] == "confirmation" and e["accepted"]]

        rated = await eng.feedback(old.record_id, "rating", namespace="a", rating=4)
        assert rated.record_id == old.record_id
        assert [e for e in _events("api.feedback") if e["kind"] == "rating" and e["accepted"]]
    finally:
        await eng.stop()


async def test_correction_latency_counts_reads_between_exposure_and_correction() -> None:
    eng = _engine(write={"feedback_v2": True})
    await eng.start()
    try:
        _tap_on()
        old = await eng.write(
            "Caroline lives in Paris", namespace="a", entity="Caroline", attribute="city"
        )
        await eng.search("Where does Caroline live", namespace="a")  # turn 1: wrong memory surfaced
        await eng.search("Caroline city", namespace="a")  # turn 2
        await eng.feedback(old.record_id, "correction", namespace="a", new_value="Berlin")
        stats = await eng.correction_latency("a")
        assert stats.total == 1 and stats.measured == 1
        assert stats.minimum == 1 and stats.maximum == 1
        assert _events("api.feedback.latency")[-1]["measured"] == 1
    finally:
        await eng.stop()


# --------------------------------------------------------------------- O4-11 observer views


async def _shared_engine(**policies: Any) -> Engine:
    eng = _engine({"shared": {"enabled": True, "policies": policies}})
    await eng.start()
    await eng.write("Alpha wrote the launch codename Bluebird", namespace="a")
    await eng.write("Alpha private grocery list", namespace="a", memory_type="episodic")
    await eng.grant("b", namespace="a", memory_types=["semantic"])
    return eng


async def test_observer_views_flag_off_unchanged() -> None:
    eng = await _shared_engine()
    try:
        _tap_on()
        res = await eng.shared_search("launch codename Bluebird", namespace="b", top_k=5)
        foreign = [r for r, _ in res if r.namespace == "a"]
        assert foreign and all(r.memory_type == "semantic" for r in foreign)
        assert _events("read.observer_view") == []
    finally:
        await eng.stop()


async def test_observer_views_include_and_attenuate() -> None:
    eng = await _shared_engine(observer_views_v2=True)
    try:
        _tap_on()
        res = await eng.shared_search("launch codename Bluebird", namespace="b", top_k=5)
        foreign = [r for r, _ in res if r.namespace == "a"]
        assert foreign and all(r.memory_type == "semantic" for r in foreign)
        assert all(r.trust <= 0.3 for r in foreign)  # E1 cap still holds
        ev = _events("read.observer_view")
        assert ev and ev[-1]["included"] == len(foreign)
        assert set(ev[-1]["included_ids"]) == {r.record_id for r in foreign}
    finally:
        await eng.stop()


def test_observer_results_exclusion_reasons() -> None:
    from memspine.api.ops_memory import _observer_results
    from memspine.core.integrity import IntegrityPolicy

    sem = MemoryRecord(namespace="a", memory_type="semantic", content="s", trust=0.9)
    epi = MemoryRecord(namespace="a", memory_type="episodic", content="e", trust=0.9)
    _tap_on()
    integ = IntegrityPolicy(enabled=True, kappa=0.5, admission_threshold=0.6)
    # grant covers semantic only: the episodic hit is excluded with a reason
    out = _observer_results(
        "b",
        [(sem, 1.0, 0.9), (epi, 1.0, 0.9)],
        {"a": frozenset({"semantic"})},
        IntegrityPolicy(enabled=True),
    )
    assert [r.record_id for r, _ in out] == [sem.record_id]
    assert out[0][0].trust == pytest.approx(0.45)  # 0.9 * kappa 0.5
    ev = _events("read.observer_view")[-1]
    assert ev["reasons"] == {"scope_not_granted": 1}
    # admission threshold applies on top
    out = _observer_results("b", [(sem, 1.0, 0.9)], {"a": None}, integ)
    assert out == []
    assert _events("read.observer_view")[-1]["reasons"] == {"below_threshold": 1}


# ------------------------------------------------------------------ stage layering / dispatch


async def test_check_watches_flag_off_makes_no_advisory_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from memspine.clients.sqlite import SQLiteClient
    from memspine.config.loader import load_config
    from memspine.derive.pipeline.stages.context import PipelineContext
    from memspine.services.storage.sqlite.engine import SQLiteStorage

    client = SQLiteClient(":memory:")
    await client.connect()
    storage = SQLiteStorage(client)
    await storage.start()

    async def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("advisory sweep ran with the flags off")

    monkeypatch.setattr(advisory, "prospective_sweep", boom)
    monkeypatch.setattr(advisory, "procedural_sweep", boom)

    async def noop(event: Any) -> None:
        return None

    ctx = PipelineContext(storage=storage, config=load_config().config, append_event=noop)
    assert await check_watches(ctx) == {"status": "ok", "fired": 0, "due": 0, "invalidated": 0}
