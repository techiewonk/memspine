"""Round-2 retrieval fixes (R2-1 bridge hop, R2-2 comparison votes, R2-3 wide trigger). Opt-in."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import ReadConfig
from memspine.models.encoders.query.shape import is_set_question, is_set_question_wide
from memspine.models.encoders.query.temporal_query import (
    LegHit,
    bridge_phrases,
    comparison_speaker_legs,
    has_bridge_cue,
)
from memspine.observability.trace import search_forensics
from memspine.read.pipeline.state import _RERANK_SUPPORT
from memspine.store.records import MemoryRecord

T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)
LOCOMO = Path(__file__).resolve().parents[2] / "data" / "locomo10.json"


def _engine(**read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        embedding={"provider": "hash", "settings": {}},
        memories={"episodic": {"enabled": True}},
        read={"record_access": False, **read},
    )


def _rec(text: str) -> MemoryRecord:
    return MemoryRecord(namespace="a", memory_type="episodic", content=text)


async def _seed(eng: Engine, lines: list[str]) -> None:
    for i, text in enumerate(lines):
        await eng.write(
            text, namespace="a", memory_type="episodic", valid_from=T0 + timedelta(minutes=i)
        )


# -- R2-3 wide trigger -------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "How did Mirel promote her clothes store?",
        "How has Bram supported her community?",
        "How does Sven relax after work?",
        "What gifts did Ilse buy?",
        "Which hobbies does Sven enjoy?",  # already a base hit: stays a hit
    ],
)
def test_wide_trigger_fires(question: str) -> None:
    assert is_set_question_wide(question)


@pytest.mark.parametrize(
    "question",
    [
        "When did Bram go camping?",
        "How many kids does Bram have?",
        "How did Bram feel after the race?",
        "Did Bram go to the park?",
        "What is Bram's job?",
        "Where did Sven travel last summer?",
    ],
)
def test_wide_trigger_stays_quiet(question: str) -> None:
    assert not is_set_question_wide(question)


def test_wide_trigger_is_a_superset_of_the_base_trigger() -> None:
    for q in ("What activities does Bram partake in?", "What kind of books does Sven write?"):
        assert is_set_question(q) and is_set_question_wide(q)
    assert not is_set_question("How did Mirel promote her clothes store?")


def test_wide_trigger_precision_over_locomo(capsys: pytest.CaptureFixture[str]) -> None:
    """Offline report: how many LoCoMo questions each trigger fires on, per category."""
    if not LOCOMO.exists():
        pytest.skip("data/locomo10.json not present")
    data = json.loads(LOCOMO.read_text(encoding="utf-8"))
    total: Counter[Any] = Counter()
    base: Counter[Any] = Counter()
    wide: Counter[Any] = Counter()
    for conv in data:
        for qa in conv.get("qa", []):
            cat, q = qa.get("category"), qa.get("question", "")
            total[cat] += 1
            base[cat] += is_set_question(q)
            wide[cat] += is_set_question_wide(q)
    with capsys.disabled():
        print("\nLoCoMo trigger counts (category: base / wide / total)")
        for cat in sorted(total, key=str):
            print(f"  cat {cat}: {base[cat]} / {wide[cat]} / {total[cat]}")
        print(f"  all: {sum(base.values())} / {sum(wide.values())} / {sum(total.values())}")
    assert sum(wide.values()) >= sum(base.values())


async def test_engine_uses_wide_trigger_only_when_selected() -> None:
    lines = ["Mirel: I gave out flyers for the store", "Sven: I read a book"]
    question = "How did Mirel promote her clothes store?"
    for trigger, expect in (("set_question", False), ("set_question_wide", True)):
        eng = _engine(list_mode=True, list_trigger=trigger)
        await eng.start()
        try:
            await _seed(eng, lines)
            with search_forensics() as stages:
                await eng.read(question, namespace="a", mode="replay", top_k=3)
            names = [n for n, _ in stages.get("extra_legs", [])]
            assert ("speaker_vote" in names) is expect
        finally:
            await eng.stop()


# -- R2-2 comparison votes ---------------------------------------------------------------


def test_comparison_legs_one_per_named_speaker() -> None:
    recs = [
        _rec("Bram: I love camping"),
        _rec("Ilse: I love painting"),
        _rec("Bram: pottery"),
        _rec("Ilse: marches"),
    ]
    hits = [LegHit(r.record_id, 1.0 - i / 10) for i, r in enumerate(recs)]
    out = comparison_speaker_legs("What do Ilse and Bram both like?", recs, hits, 5)
    assert [n for n, _ in out] == ["ilse", "bram"]  # order of mention
    assert [h.record_id for h in out[0][1]] == [recs[1].record_id, recs[3].record_id]
    assert [h.record_id for h in out[1][1]] == [recs[0].record_id, recs[2].record_id]
    # no cue, or one speaker: nothing
    assert comparison_speaker_legs("What do Ilse and Bram like?", recs, hits) == []
    assert comparison_speaker_legs("What do they both like, Bram?", recs, hits) == []


async def test_two_speaker_comparison_fires_two_votes() -> None:
    eng = _engine(list_mode=True)
    await eng.start()
    try:
        await _seed(
            eng,
            [
                "Bram: I love camping with the kids",
                "Ilse: I joined a march for equality",
                "Bram: We went swimming at the beach",
                "Ilse: Our support group meets weekly",
            ],
        )
        with search_forensics() as stages:
            await eng.read(
                "What do Bram and Ilse both like?", namespace="a", mode="replay", top_k=3
            )
        legs = dict(stages.get("extra_legs", []))
        assert "speaker_vote_a" in legs and "speaker_vote_b" in legs
        assert "speaker_vote" not in legs
        recs = {
            rid: (await eng._require_started().get_record(rid)).content
            for rid, _ in legs["speaker_vote_a"]
        }
        assert all(c.startswith("Bram:") for c in recs.values())
    finally:
        await eng.stop()


# -- R2-1 bridge hop ---------------------------------------------------------------------


def test_bridge_phrases_skip_question_words() -> None:
    texts = [
        "Ilse: I moved from my home country four years ago, so adoption agencies matter to me",
        "Ilse: The adoption agencies helped a lot",
    ]
    got = bridge_phrases("Where did Ilse move from?", texts)
    assert "home country" in got
    assert "adoption agencies" in got  # 'adoption' is in the question, so only ...
    assert len(got) <= 3


def test_bridge_phrases_exclude_phrases_made_of_question_words() -> None:
    got = bridge_phrases("Tell me about the home country", ["Ann: my home country is far"])
    assert got == []


async def test_bridge_hop_adds_leg_and_records_phrases() -> None:
    eng = _engine(bridge_hop=True)
    await eng.start()
    try:
        await _seed(
            eng,
            [
                "Ilse: I moved from my home country years ago",
                "Sven: unrelated chatter about football",
                "Ilse: Life in my home country was very different",
                "Tom: pottery class",
            ],
        )
        with search_forensics() as stages:
            await eng.read("Where did Ilse move from?", namespace="a", mode="replay", top_k=2)
        assert "home country" in stages["bridge_phrases"]
        assert "bridge" in dict(stages["extra_legs"])
    finally:
        await eng.stop()


async def test_bridge_hop_off_by_default() -> None:
    eng = _engine()
    await eng.start()
    try:
        await _seed(eng, ["Ilse: I moved from my home country years ago", "Sven: hi"])
        with search_forensics() as stages:
            await eng.read("Where did Ilse move from?", namespace="a", mode="replay", top_k=2)
        assert "bridge_phrases" not in stages
        assert "bridge" not in dict(stages.get("extra_legs", []))
    finally:
        await eng.stop()


async def test_bridge_hop_fails_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(bridge_hop=True)
    await eng.start()
    try:
        await _seed(eng, ["Ilse: I moved from my home country years ago", "Sven: hi"])

        def boom(*_: Any, **__: Any) -> Any:
            raise RuntimeError("boom")

        monkeypatch.setattr("memspine.read.pipeline.legs.bridge_phrases", boom)
        out = await eng.read("Where did Ilse move from?", namespace="a", mode="replay")
        assert out.context.records
    finally:
        await eng.stop()


# -- R2-1b bridge hop gate ---------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "Where did Ilse move from 4 years ago?",
        "Would Ilse like to return to her native town this year?",
        "What did Tomasz do in his hometown?",
        "How does Mirel describe the studio that Noor has opened?",
        "When is Bram's daughter's birthday?",
        "What is the book that Tom recommended?",
        "What is the city where Sven grew up?",
    ],
)
def test_bridge_cue_fires(question: str) -> None:
    assert has_bridge_cue(question)


@pytest.mark.parametrize(
    "question",
    [
        "How many children does Bram have?",
        "What do Bram's family give her?",
        "What did Noor say about Mirel's progress with her store?",
        "How long did it take for Noor to open his studio?",
        "When did Ilse and Bram go to a pride festival together?",
        "What does Bram do to destress?",
    ],
)
def test_bridge_cue_does_not_fire(question: str) -> None:
    assert not has_bridge_cue(question)


def test_bridge_cue_on_dev_questions_is_rare() -> None:
    if not LOCOMO.exists():
        pytest.skip("LoCoMo data/locomo10.json not on disk")
    data = json.loads(LOCOMO.read_text(encoding="utf-8"))
    dev = [s for s in data if s["sample_id"] in {"conv-26", "conv-30", "conv-41", "conv-42"}]
    qs = [q["question"] for s in dev for q in s["qa"] if q["category"] in (1, 2, 3, 4)]
    fired = sum(has_bridge_cue(q) for q in qs)
    assert 0 < fired <= len(qs) * 0.03


def test_bridge_gate_modes() -> None:
    cue_q, plain_q = "Where did Ilse move from?", "How many children does Bram have?"

    def gate(mode: str, question: str, support: float | None) -> tuple[bool, str]:
        read = ReadConfig(bridge_hop=True, bridge_hop_gate=mode)
        token = _RERANK_SUPPORT.set(support)
        try:
            with search_forensics() as fx:
                return Engine._bridge_gate(question, read), fx["bridge_gate"]
        finally:
            _RERANK_SUPPORT.reset(token)

    assert gate("always", plain_q, 0.9) == (True, "always")
    assert gate("cue", cue_q, 0.9) == (True, "cue")
    assert gate("cue", plain_q, 0.1) == (False, "skipped")
    assert gate("weak", plain_q, 0.35) == (True, "weak")
    assert gate("weak", plain_q, 0.45) == (False, "skipped")
    assert gate("weak", plain_q, None) == (False, "skipped")  # no rerank signal: no hop
    assert gate("cue_or_weak", cue_q, 0.9) == (True, "cue")
    assert gate("cue_or_weak", plain_q, 0.1) == (True, "weak")
    assert gate("cue_or_weak", plain_q, 0.9) == (False, "skipped")


async def test_bridge_gate_skips_in_engine_and_records() -> None:
    eng = _engine(bridge_hop=True, bridge_hop_gate="cue")
    await eng.start()
    try:
        await _seed(eng, ["Ilse: I moved from my home country years ago", "Sven: hi"])
        with search_forensics() as stages:
            await eng.read("How many pets does Sven have?", namespace="a", mode="replay")
        assert stages["bridge_gate"] == "skipped"
        assert "bridge_phrases" not in stages
        with search_forensics() as stages:
            await eng.read("Where did Ilse move from?", namespace="a", mode="replay")
        assert stages["bridge_gate"] == "cue"
        assert "bridge_phrases" in stages
    finally:
        await eng.stop()


def test_bridge_gate_default_is_always() -> None:
    assert ReadConfig().bridge_hop_gate == "always"
