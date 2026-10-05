"""#62 Nemori predict-calibrate (ADR-049) and #56 engine wiring (ADR-048), fake LLMs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from memspine import Engine
from memspine.config import constants
from memspine.core.records import RecordStatus
from memspine.prompts.models import ExtractedFact


def _engine(policies: dict[str, object] | None) -> Engine:
    consolidation = {"consolidation": policies} if policies else {}
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            "episodic": {"enabled": True, "policies": consolidation},
            "semantic": {"enabled": True},
        },
    )


async def _session(eng: Engine) -> list[str]:
    t0 = datetime(2023, 5, 8, 13, 0, tzinfo=UTC)
    msgs = [
        {"role": "user", "content": c, "timestamp": (t0 + timedelta(minutes=i)).isoformat()}
        for i, c in enumerate(
            [
                "Alice: guess what happened at work",
                "Alice: I still work at Acme, but I got promoted to team lead",
                "Alice: and I am moving to Paris next month",
            ]
        )
    ]
    written = await eng.write_messages(msgs, namespace="a", session_id="s1", group_id="s1")
    return [r.record_id for r in written]


def _fakes(eng: Engine, monkeypatch: pytest.MonkeyPatch) -> dict[str, list[object]]:
    calls: dict[str, list[object]] = {"predict": [], "calibrate": []}

    async def predict(cue: str, date: str, knowledge: list[str]) -> str:
        calls["predict"].append((cue, date, list(knowledge)))
        return "Alice works at Acme\nAlice lives in Berlin"

    async def calibrate(prediction: str, content: str) -> list[ExtractedFact]:
        calls["calibrate"].append((prediction, content))
        return [
            # Already predicted (in other order/case): must not be re-stored.
            ExtractedFact(entity="Alice", attribute="job", value="Alice works at Acme"),
            # Novel: stored.
            ExtractedFact(entity="Alice", attribute="job", value="Alice was promoted to team lead"),
            ExtractedFact(entity="Alice", attribute="plan", value="Alice is moving to Paris"),
        ]

    monkeypatch.setattr(eng, "_build_episode_predictor", lambda: predict)
    monkeypatch.setattr(eng, "_build_calibrator", lambda: calibrate)
    return calls


async def _surprises(eng: Engine) -> list[object]:
    return [
        r
        for r in await eng.retrieve(namespace="a", memory_type="semantic")
        if constants.SURPRISE_FACT_TAG in r.tags
    ]


async def test_predicted_facts_are_not_stored_novel_ones_are(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng = _engine({"predict_calibrate": True})
    calls = _fakes(eng, monkeypatch)
    await eng.start()
    try:
        await eng.write("Alice lives in Berlin", namespace="a")
        turns = await _session(eng)
        first = await eng.sleep()
        assert first["predict_calibrate"]["surprises"] == 2
        assert len(calls["predict"]) == 1 and len(calls["calibrate"]) == 1
        cue, date, knowledge = calls["predict"][0]  # type: ignore[misc]
        assert cue == "Alice: guess what happened at work" and date == "2023-05-08"
        assert "Alice lives in Berlin" in knowledge  # predicted from stored memory
        stored = await _surprises(eng)
        texts = sorted(r.content for r in stored)  # type: ignore[attr-defined]
        assert texts == [
            "Alice job: Alice was promoted to team lead",
            "Alice plan: Alice is moving to Paris",
        ]
        for record in stored:
            assert set(record.source.parents) == set(turns)  # type: ignore[attr-defined]
            assert record.source.channel == "calibration"  # type: ignore[attr-defined]
        again = await eng.sleep()
        assert again["predict_calibrate"]["surprises"] == 0  # once per session
        assert len(calls["calibrate"]) == 1
        # Erasure of a turn cascades to the surprises derived from it.
        await eng.forget(turns[1], namespace="a", hard=True)
        live = [
            r
            for r in await _surprises(eng)
            if r.status is RecordStatus.ACTIVATED  # type: ignore[attr-defined]
        ]
        assert live == []
    finally:
        await eng.stop()


async def test_predict_calibrate_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(None)
    calls = _fakes(eng, monkeypatch)
    await eng.start()
    try:
        await _session(eng)
        stats = await eng.sleep()
        assert "predict_calibrate" not in stats  # not even in the cycle
        assert calls["predict"] == [] and await _surprises(eng) == []
    finally:
        await eng.stop()


async def test_predict_calibrate_skips_without_a_role() -> None:
    eng = _engine({"predict_calibrate": True})
    await eng.start()
    try:
        await _session(eng)
        stats = await eng.sleep()
        assert stats["predict_calibrate"]["status"] == "skipped"
    finally:
        await eng.stop()


async def test_engine_incremental_summary_wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine({"session_summary": {"incremental": True, "rebuild_every": 4}})
    calls: list[str] = []

    async def full(content: str) -> str:
        calls.append("full")
        return "Alice news."

    async def update(previous: str, content: str) -> str:
        calls.append("inc")
        return previous + " More."

    monkeypatch.setattr(eng, "_build_summarize", lambda: full)
    monkeypatch.setattr(eng, "_build_summarize_incremental", lambda: update)
    await eng.start()
    try:
        turns = await _session(eng)
        await eng.sleep()
        later = datetime(2023, 5, 8, 13, 5, tzinfo=UTC)
        more = await eng.write_messages(
            [{"role": "user", "content": "Alice: also a cat", "timestamp": later.isoformat()}],
            namespace="a",
            session_id="s1",
            group_id="s1",
        )
        await eng.sleep()
        assert calls == ["full", "inc"]
        summaries = [
            r
            for r in await eng.retrieve(namespace="a", memory_type="semantic")
            if r.source.channel == "consolidation" and r.status is RecordStatus.ACTIVATED
        ]
        assert [s.content for s in summaries] == ["Alice news. More."]
        assert set(summaries[0].source.parents) == {*turns, more[0].record_id}
        await eng.forget(more[0].record_id, namespace="a", hard=True)
        live = [
            r
            for r in await eng.retrieve(namespace="a", memory_type="semantic")
            if r.source.channel == "consolidation" and r.status is RecordStatus.ACTIVATED
        ]
        assert live == []  # erasure cascades through parents (ADR-039)
    finally:
        await eng.stop()
