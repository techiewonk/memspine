"""I28: the task-level decider port, its adapters and the read-path decision points."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import ReadConfig
from memspine.engine import search_forensics
from memspine.services.decision import decider as decider_mod
from memspine.services.decision.decider import (
    TASKS,
    Decision,
    HeuristicDecider,
    OpenDeciderDecider,
    build_decider,
)

T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)


class FakeDecider:
    """Answers from a table; records every call (task, question, context)."""

    decider_id = "fake"

    def __init__(self, answers: dict[str, tuple[str, float | None]] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[tuple[str, str, str | None]] = []

    async def decide(self, task: str, question: str, context: str | None = None) -> Decision:
        self.calls.append((task, question, context))
        label, conf = self.answers[task]
        return Decision(label, conf, {"fake": True}, task, self.decider_id)


class BoomDecider:
    decider_id = "boom"

    async def decide(self, task: str, question: str, context: str | None = None) -> Decision:
        raise RuntimeError("model unavailable")


def _engine(**read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"record_access": False, **read},
    )


async def _seed(eng: Engine) -> None:
    for i, text in enumerate(
        ["Caroline: I moved from my home country years ago", "Tim: football on Sunday"]
    ):
        await eng.write(
            text, namespace="a", memory_type="episodic", valid_from=T0 + timedelta(minutes=i)
        )


# -- defaults ------------------------------------------------------------------------------


def test_defaults_are_heuristic() -> None:
    read = ReadConfig()
    assert read.decider == "heuristic"
    assert read.decider_device == "cpu"
    assert read.decider_tasks == ["list_mode", "bridge_hop"]
    assert read.relevance_gate == "off"
    assert read.decider_min_confidence == 0.5  # the calibrated probability, no tuned cut


def test_unknown_decider_task_is_rejected() -> None:
    with pytest.raises(ValueError, match="decider_tasks"):
        ReadConfig(decider_tasks=["nonsense"])  # type: ignore[list-item]


async def test_default_engine_never_consults_a_decider() -> None:
    fake = FakeDecider({"relevance": ("irrelevant", 0.99)})
    eng = _engine()
    eng.set_decider(fake)
    await eng.start()
    try:
        await _seed(eng)
        with search_forensics() as stages:
            out = await eng.read("Where did Caroline move from?", namespace="a", mode="replay")
        assert fake.calls == []
        assert "decisions" not in stages
        assert out.context.records and not out.context.abstained
    finally:
        await eng.stop()


# -- heuristic adapter ---------------------------------------------------------------------


async def test_heuristic_adapter_wraps_existing_rules() -> None:
    h = HeuristicDecider()
    assert (await h.decide("list_mode", "What activities does Melanie do?")).label == "set"
    assert (await h.decide("list_mode", "When did Melanie go camping?")).label == "single"
    assert (
        await h.decide("bridge_hop", "Where did Caroline move from 4 years ago?")
    ).label == "hop"
    assert (await h.decide("temporal_intent", "When did she travel?")).label == "temporal"
    assert (await h.decide("relevance", "hi", "- x")).label == "relevant"
    assert (await h.decide("abstention", "q", "ctx")).label == "answerable"
    with pytest.raises(KeyError):
        await h.decide("refusal", "q", "a")  # the regex lives in the evals harness


def test_build_decider() -> None:
    assert build_decider("heuristic").decider_id == "heuristic"
    assert build_decider("opendecider").decider_id == "opendecider"
    with pytest.raises(ValueError):
        build_decider("nope")


# -- decision points honour the decider ----------------------------------------------------


async def test_list_mode_follows_the_decider() -> None:
    eng = _engine(
        list_mode=True,
        decider="opendecider",
        decider_tasks=["list_mode"],
        decider_min_confidence=0.6,
    )
    fake = FakeDecider({"list_mode": ("set", 0.9)})
    eng.set_decider(fake)
    await eng.start()
    try:
        # a regex-negative question the decider calls a set
        assert not eng._list_mode_fires("Tell me about the weekend")
        assert await eng._list_fires("Tell me about the weekend") is True
        fake.answers["list_mode"] = ("single", 0.9)
        assert await eng._list_fires("What activities does Melanie do?") is False
        # unsure: the regex answer stands
        fake.answers["list_mode"] = ("single", 0.55)
        assert await eng._list_fires("What activities does Melanie do?") is True
    finally:
        await eng.stop()


async def test_list_mode_off_never_asks_the_decider() -> None:
    eng = _engine(list_mode=False, decider="opendecider", decider_tasks=["list_mode"])
    fake = FakeDecider({"list_mode": ("set", 0.99)})
    eng.set_decider(fake)
    await eng.start()
    try:
        assert await eng._list_fires("What activities does Melanie do?") is False
        assert fake.calls == []
    finally:
        await eng.stop()


async def test_bridge_gate_follows_the_decider_and_records_it() -> None:
    eng = _engine(bridge_hop=True, decider="opendecider", decider_tasks=["bridge_hop"])
    fake = FakeDecider({"bridge_hop": ("no_hop", 0.95)})
    eng.set_decider(fake)
    await eng.start()
    try:
        await _seed(eng)
        with search_forensics() as stages:
            await eng.read("Where did Caroline move from?", namespace="a", mode="replay", top_k=2)
        assert stages["bridge_gate"] == "skipped"
        assert "bridge_phrases" not in stages
        entry = stages["decisions"][0]
        assert entry["task"] == "bridge_hop" and entry["adapter"] == "fake"
        assert entry["label"] == "no_hop" and entry["used"] is True
        assert entry["heuristic"] == "hop"  # the cue regex would have fired
        fake.answers["bridge_hop"] = ("hop", 0.95)
        with search_forensics() as stages:
            await eng.read("Where did Caroline move from?", namespace="a", mode="replay", top_k=2)
        assert stages["bridge_gate"] == "decider"
        assert "home country" in stages["bridge_phrases"]
    finally:
        await eng.stop()


async def test_relevance_gate_injects_nothing_when_sure() -> None:
    eng = _engine(decider="opendecider", relevance_gate="decider", decider_min_confidence=0.6)
    fake = FakeDecider({"relevance": ("irrelevant", 0.97)})
    eng.set_decider(fake)
    await eng.start()
    try:
        await _seed(eng)
        with search_forensics() as stages:
            out = await eng.read("Write me a poem about autumn", namespace="a", mode="replay")
        assert out.context.abstained and not out.context.records
        assert stages["decisions"][0]["final"] == "irrelevant"
        task, question, context = fake.calls[0]
        assert task == "relevance" and "poem" in question and "Caroline" in (context or "")
        # not sure: memories are still injected
        fake.answers["relevance"] = ("irrelevant", 0.55)
        out = await eng.read("Write me a poem about autumn", namespace="a", mode="replay")
        assert out.context.records
        # relevant: injected
        fake.answers["relevance"] = ("relevant", 0.99)
        out = await eng.read("Where did Caroline move from?", namespace="a", mode="replay")
        assert out.context.records
    finally:
        await eng.stop()


async def test_a_failing_decider_falls_back_to_the_heuristic() -> None:
    eng = _engine(
        list_mode=True, decider="opendecider", decider_tasks=["list_mode"], relevance_gate="decider"
    )
    eng.set_decider(BoomDecider())
    await eng.start()
    try:
        await _seed(eng)
        assert await eng._list_fires("What activities does Melanie do?") is True
        with search_forensics() as stages:
            out = await eng.read("Where did Caroline move from?", namespace="a", mode="replay")
        assert out.context.records
        assert stages["decisions"][0]["error"] == "model unavailable"
        assert stages["decisions"][0]["used"] is False
    finally:
        await eng.stop()


async def test_the_decider_sees_no_gold_or_category() -> None:
    """Decision points receive only the question and retrieved/answer text."""
    eng = _engine(
        list_mode=True,
        bridge_hop=True,
        decider="opendecider",
        decider_tasks=["list_mode", "bridge_hop"],
        relevance_gate="decider",
        relevance_gate_bypass="none",
    )
    fake = FakeDecider(
        {"list_mode": ("single", 0.9), "bridge_hop": ("hop", 0.9), "relevance": ("relevant", 0.9)}
    )
    eng.set_decider(fake)
    await eng.start()
    try:
        await _seed(eng)
        await eng.read("Where did Caroline move from?", namespace="a", mode="replay")
        assert {c[0] for c in fake.calls} == {"list_mode", "bridge_hop", "relevance"}
        texts = " ".join(f"{q} {c or ''}" for _, q, c in fake.calls).lower()
        assert "gold" not in texts and "category" not in texts
    finally:
        await eng.stop()


# -- OpenDecider adapter (fake model: calling convention) ------------------------------------


class _FakeNano:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str]] = []

    def yes_probability(self, states: list[str], instructions: str, criteria: Any = None) -> list:
        self.calls.append((list(states), instructions))
        return [0.9 if "poem" not in s else 0.1 for s in states]


@pytest.fixture
def fake_nano(monkeypatch: pytest.MonkeyPatch) -> tuple[_FakeNano, list[tuple[str, str]]]:
    loads: list[tuple[str, str]] = []
    model = _FakeNano()

    def load(name: str, device: str = "cpu", dtype: str = "float32", **_: Any) -> _FakeNano:
        loads.append((name, device))
        return model

    monkeypatch.setattr(decider_mod, "load_nano", load)
    return model, loads


async def test_opendecider_adapter_convention(
    fake_nano: tuple[_FakeNano, list[tuple[str, str]]],
) -> None:
    model, loads = fake_nano
    a = OpenDeciderDecider("m", "cpu")
    d1 = await a.decide("relevance", "Write me a poem", "- Caroline moved")
    d2 = await a.decide("list_mode", "What hobbies does Tim have?")
    assert (d1.label, round(d1.confidence or 0, 2)) == ("irrelevant", 0.9)
    assert d2.label == "set" and d2.adapter == "opendecider" and d2.task == "list_mode"
    assert loads == [("m", "cpu"), ("m", "cpu")]  # the real loader caches per process
    states, instructions = model.calls[0]
    assert states[0].startswith("Message: Write me a poem" + chr(10) + "Memories:")
    assert instructions == TASKS["relevance"].instructions
    batch = await a.decide_batch("refusal", [("q1", "I do not know"), ("q2", "Paris")])
    assert [x.label for x in batch] == ["refusal", "refusal"]  # the fake answers 0.9 for both
    assert len(model.calls[-1][0]) == 2  # one batched call


async def test_opendecider_missing_dependencies_is_a_missing_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from memspine.exceptions import MissingServiceError
    from memspine.services.decision import opendecider_nano as nano

    monkeypatch.setitem(sys.modules, "torch", None)  # import raises ImportError
    monkeypatch.setattr(nano, "_MODELS", {})
    monkeypatch.setattr(nano, "_ERRORS", {})
    with pytest.raises(MissingServiceError):
        await OpenDeciderDecider("m", "cpu").decide("relevance", "q", "c")
    with pytest.raises(MissingServiceError):  # cached, not retried
        await OpenDeciderDecider("m", "cpu").decide("relevance", "q", "c")


def test_nano_input_layout_matches_the_published_format() -> None:
    """[CLS] question: ... [SEP] [MASK] yes: Yes [MASK] no: No [SEP] input: ... [SEP]"""
    from memspine.services.decision.opendecider_nano import nano_ids, noul_options

    class Tok:
        cls_token_id, sep_token_id, mask_token_id = 1, 2, 3

        def encode(self, text: str, add_special_tokens: bool, split_special_tokens: bool) -> list:
            assert not add_special_tokens and split_special_tokens
            return [100 + len(text)]  # one token per text, identifiable by its length

    ids, truncated = nano_ids(Tok(), "abc", "Q?", noul_options(), 2048)
    assert ids == [
        1,
        100 + len("question: Q?"),
        2,
        3,
        100 + len(" yes: Yes"),
        3,
        100 + len(" no: No"),
        2,
        100 + len("input: abc"),
        2,
    ]
    assert not truncated
    ids, truncated = nano_ids(Tok(), "x" * 10, "Q?", noul_options(), 5)
    assert truncated and ids.count(3) == 2  # the state goes first, every option survives


def test_decide_many_buckets_by_length_and_keeps_order() -> None:
    """I38: sorted by length, micro-batches of max_batch, results in input order."""
    from memspine.services.decision.opendecider_nano import _Base, noul_options

    class Tok:
        cls_token_id, sep_token_id, mask_token_id, pad_token_id = 1, 2, 3, 0

        def encode(self, text: str, add_special_tokens: bool, split_special_tokens: bool) -> list:
            return [7] * len(text.split())

    class Fake(_Base):
        tok, max_len, mask_id, max_batch = Tok(), 2048, 3, 2

        def __init__(self) -> None:
            self.batches: list[list[int]] = []

        def _forward(self, rows: list[list[int]]) -> list[list[float]]:
            self.batches.append([len(r) for r in rows])
            # yes-logit grows with the row length, so the answer identifies the input
            return [[float(len(r)), 0.0] for r in rows]

    m = Fake()
    texts = ["a b c d e f", "a", "a b c", "a b c d e f g h", "a b"]
    out = m.decide_many([(t, "Q", noul_options()) for t in texts])
    lengths = [b for batch in m.batches for b in batch]
    assert lengths == sorted(lengths) and all(len(b) <= 2 for b in m.batches)
    yes = [p["yes"] for p in out]
    assert abs(sum(out[0].values()) - 1.0) < 1e-9
    assert yes[1] < yes[4] < yes[2] < yes[0] < yes[3]  # input order kept: lengths 1,5,3,6,8 words


async def test_decide_many_mixes_tasks_in_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    class Model:
        def __init__(self) -> None:
            self.calls: list[list[Any]] = []

        def decide_many(self, batch: list[Any]) -> list[dict[str, float]]:
            self.calls.append(batch)
            return [{"yes": 0.8, "no": 0.2} for _ in batch]

    model = Model()
    monkeypatch.setattr(decider_mod, "load_nano", lambda *a, **k: model)
    out = await OpenDeciderDecider("m", "cpu").decide_many(
        [("relevance", "q", "- m"), ("list_mode", "What hobbies?", None), ("refusal", "q", "no")]
    )
    assert [d.label for d in out] == ["relevant", "set", "refusal"]
    assert len(model.calls) == 1 and len(model.calls[0]) == 3  # one batched call


# -- optional smoke test: the real model, skipped unless it is downloaded --------------------


def _real_model_cached() -> bool:
    try:
        import safetensors  # noqa: F401
        import torch  # noqa: F401
        import transformers  # noqa: F401
        from huggingface_hub import try_to_load_from_cache

        for f in ("opendecider.json", "model.safetensors", "head.safetensors"):
            if not isinstance(try_to_load_from_cache(decider_mod.DEFAULT_MODEL, f), str):
                return False
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _real_model_cached(), reason="opendecider-nano is not downloaded")
async def test_real_opendecider_nano_on_cpu_matches_the_model_card() -> None:
    from memspine.services.decision.opendecider_nano import load_nano

    # model card example: churn_risk noul = 0.922 (parity with the package: max diff 6e-8)
    state = (
        "Hi, we were billed twice for March. Please refund the duplicate today or we will "
        "cancel our plan."
    )
    p = load_nano(decider_mod.DEFAULT_MODEL, "cpu").yes_probability(
        [state], "Does the user threaten to cancel or leave?"
    )[0]
    assert abs(p - 0.922) < 2e-3
    d = OpenDeciderDecider(decider_mod.DEFAULT_MODEL, "cpu")
    refusal = await d.decide("refusal", "Where did Tim live?", "That is not mentioned.")
    answer = await d.decide("refusal", "Where did Tim live?", "Tim lived in Paris.")
    assert refusal.label == "refusal" and answer.label == "answer"
