# ruff: noqa: E501
"""W2-15: coverage check and ONE targeted re-extraction (``write.coverage_v2``, envelope-stage order
250). Model-free: envelopes are built through the real compiler from hand-written lines (invented
names, domains used nowhere else), then one end-to-end window runs with ``write.envelope_v2`` + the
flag and a stub window LLM that omits a line on the first call."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "evals"))
import coverage_gate as gate
from test_w2_06a_envelope import _engine
from test_window_write import _facts, _send

from memspine.config import constants
from memspine.config.schema import MemspineConfig
from memspine.derive.compile import WindowLine, compile_items
from memspine.derive.coverage_stage import COVERAGE_STAGE_ORDER, make_coverage_stage
from memspine.derive.envelope_compiler import EnvelopeCompiler
from memspine.derive.envelope_stages import EnvelopeStageContext, run_envelope_stages
from memspine.derive.grounding_stage import make_grounding_stage
from memspine.ingest.windows import _envelope_stages
from memspine.observability import tap
from memspine.prompts.structured import STRUCTURED_SYSTEM
from memspine.schemas.durable import OwnerScope

NOW = datetime(2024, 3, 9, 12, 0, tzinfo=UTC)
SCOPE = OwnerScope(tenant="default", owner="noor")


def _line(n: int, speaker: str, text: str, role: str = "user") -> WindowLine:
    return WindowLine(
        turn_id=f"t{n}",
        source_id=f"t{n}",
        source_version=1,
        seg_index=n,
        role=role,
        speaker=speaker,
        source_at=NOW,
        tz="UTC",
        content=f"{speaker}: {text}",
    )


def _item(n: int, speaker: str, text: str, span: str) -> dict[str, Any]:
    return {
        "s": speaker,
        "r": "states",
        "o": None,
        "text": text,
        "kind": "event",
        "mode": "asserted",
        "neg": False,
        "lines": [n],
        "span": span,
        "speaker": speaker,
    }


LINES = {
    1: _line(1, "Ilse", "I keep 6 hives behind the Kestrel Lane allotment."),
    2: _line(2, "Bram", "I lost two colonies to wasps in August."),
    3: _line(3, "Ilse", "haha yes"),
    4: _line(4, "Assistant", "Noted: Bram lost two colonies to wasps in August.", role="assistant"),
}
FACT1 = _item(
    1,
    "Ilse",
    "Ilse keeps 6 hives behind the Kestrel Lane allotment",
    "keep 6 hives behind the Kestrel Lane allotment",
)
FACT2 = _item(
    2, "Bram", "Bram lost two colonies to wasps in August", "lost two colonies to wasps in August"
)


class Stub:
    """Returns ``reply`` to every call and records the messages."""

    provider_id = "fake:coverage"

    def __init__(self, reply: list[dict[str, Any]] | str = ()) -> None:
        self.reply = reply
        self.messages: list[list[dict[str, str]]] = []

    async def chat(self, messages: list[dict[str, str]], **_: Any) -> str:
        self.messages.append([dict(m) for m in messages])
        return (
            self.reply if isinstance(self.reply, str) else json.dumps({"facts": list(self.reply)})
        )


def _setup(
    first: list[dict[str, Any]], echo_line: int | None = 4
) -> tuple[list[Any], EnvelopeStageContext]:
    compiler = EnvelopeCompiler(clock=lambda: NOW)
    ctx = compiler.context(SCOPE, "w1", LINES, source_time=NOW)
    envs = list(compile_items(first, ctx).envelopes)
    records = [SimpleNamespace(tags=[constants.ECHO_TAG] if n == echo_line else []) for n in LINES]
    sctx = EnvelopeStageContext(
        namespace="a",
        window_id="w1",
        lines=LINES,
        records=records,
        compile_ctx=ctx,
        notes=Counter(),
    )
    return envs, sctx


def _run(
    llm: Stub, envs: list[Any], sctx: EnvelopeStageContext, **kw: Any
) -> tuple[list[Any], Any]:
    grounding = make_grounding_stage()
    cov = make_coverage_stage(llm, earlier=[grounding], **kw)
    out = asyncio.run(run_envelope_stages([grounding, cov.stage()], envs, sctx))
    return out, cov


# --- config and registration --------------------------------------------------------------------
def test_flag_off_registers_nothing_and_on_registers_one_stage_at_250_after_grounding() -> None:
    off = MemspineConfig()
    assert off.write.coverage_v2 is False and off.write.coverage_threshold == 0.8
    assert _envelope_stages(SimpleNamespace(), off) == ()
    on = MemspineConfig(write={"window_extract": "facts", "envelope_v2": True, "grounding_v2": True, "coverage_v2": True})
    eng = SimpleNamespace(
        _llm=SimpleNamespace(for_role=lambda r: Stub()), _prompts=SimpleNamespace(partial=None)
    )
    stages = _envelope_stages(eng, on)
    assert [(s.name, s.order) for s in stages] == [
        ("grounding", 200),
        ("coverage", COVERAGE_STAGE_ORDER),
    ]
    assert COVERAGE_STAGE_ORDER == 250 and eng._coverage_stage.stats.windows == []


# --- the stage ----------------------------------------------------------------------------------
def test_high_coverage_makes_no_call() -> None:
    llm = Stub([FACT2])
    envs, sctx = _setup([FACT1, FACT2])
    out, cov = _run(llm, envs, sctx)
    assert len(out) == 2 and llm.messages == []
    assert (
        sctx.notes["coverage.reextract_calls"] == 0
        and cov.stats.as_dict()["coverage_before"] == 1.0
    )


def test_low_coverage_makes_exactly_one_targeted_call_and_adds_the_grounded_item() -> None:
    llm = Stub([FACT2])
    envs, sctx = _setup([FACT1])
    out, cov = _run(llm, envs, sctx)
    assert len(llm.messages) == 1  # exactly one
    assert [e.text for e in out] == [FACT1["text"], FACT2["text"]]
    stats = cov.stats.as_dict()
    assert stats["coverage_before"] == 0.5 and stats["coverage_after"] == 1.0
    assert (stats["reextract_calls"], stats["added"], stats["added_unsupported_dropped"]) == (
        1,
        1,
        0,
    )
    assert sctx.notes["coverage.reextract_calls"] == 1 and sctx.notes["coverage.added"] == 1
    user = llm.messages[0][-1][
        "content"
    ]  # the W2-06b window prompt: only the uncited line, KNOWN present
    assert "NEW:\n[2]" in user and "[1]" not in user.split("CONTEXT:")[0] and "KNOWN:\nIlse" in user
    assert "Sections: facts" in user


def test_the_call_is_never_repeated_by_a_second_run_of_the_same_window_with_nothing_left() -> None:
    llm = Stub([FACT2])
    envs, sctx = _setup([FACT1, FACT2])
    _run(llm, envs, sctx)
    assert llm.messages == []


def test_an_ungrounded_addition_is_dropped_and_counted() -> None:
    bad = {**FACT2, "text": "Bram lost 9 colonies to wasps in August"}
    llm = Stub([bad])
    envs, sctx = _setup([FACT1])
    out, cov = _run(llm, envs, sctx)
    assert [e.text for e in out] == [FACT1["text"]]
    stats = cov.stats.as_dict()
    assert (stats["added"], stats["added_unsupported_dropped"]) == (0, 1)
    assert (
        sctx.notes["coverage.added_unsupported_dropped"] == 1
        and sctx.notes["coverage.reextract_calls"] == 1
    )
    assert any(k.startswith("coverage.reground.ground.dropped") for k in sctx.notes)
    assert (
        stats["coverage_after"] == stats["coverage_before"] == 0.5
    )  # nothing counts as covered by it


def test_a_true_item_next_to_a_hallucinated_one_is_still_added() -> None:
    bad = {**FACT2, "text": "Bram lost 9 colonies to wasps in August"}
    llm = Stub([bad, FACT2])
    envs, sctx = _setup([FACT1])
    out, cov = _run(llm, envs, sctx)
    assert [e.text for e in out] == [FACT1["text"], FACT2["text"]]
    assert cov.stats.as_dict()["added_unsupported_dropped"] == 1 and cov.stats.added == 1


def test_an_item_citing_only_context_or_a_covered_line_is_dropped_offtarget() -> None:
    off = {**FACT1, "text": "Ilse keeps 6 hives behind the Kestrel Lane allotment today"}
    llm = Stub([off])
    envs, sctx = _setup([FACT1])
    out, _ = _run(llm, envs, sctx)
    assert len(out) == 1 and sctx.notes["coverage.added_offtarget_dropped"] == 1


def test_filler_and_echo_lines_are_not_in_the_denominator() -> None:
    # line 3 is filler, line 4 an echo (tagged): neither may count as uncited-informative
    llm = Stub([FACT2])
    envs, sctx = _setup([FACT1, FACT2])
    _, cov = _run(llm, envs, sctx)
    assert llm.messages == [] and cov.stats.as_dict()["coverage_before"] == 1.0
    # with the tag removed, the assistant echo line is a salient uncited line and is owed coverage
    llm2 = Stub([])
    envs2, sctx2 = _setup([FACT1, FACT2], echo_line=None)
    _run(llm2, envs2, sctx2)
    assert len(llm2.messages) == 1 and "NEW:\n[4]" in llm2.messages[0][-1]["content"]


def test_a_failed_or_unreadable_call_keeps_what_the_window_had() -> None:
    envs, sctx = _setup([FACT1])
    out, _ = _run(Stub("not json at all"), envs, sctx)
    assert (
        len(out) == 1
        and sctx.notes["coverage.call_failed"] == 1
        and sctx.notes["coverage.reextract_calls"] == 1
    )

    class Boom(Stub):
        async def chat(self, messages: list[dict[str, str]], **_: Any) -> str:
            raise RuntimeError("down")

    envs, sctx = _setup([FACT1])
    out, _ = _run(Boom(), envs, sctx)
    assert len(out) == 1 and sctx.notes["coverage.call_failed"] == 1


def test_threshold_and_stage_work_without_a_grounding_stage_configured() -> None:
    llm = Stub([FACT2])
    envs, sctx = _setup([FACT1])
    cov = make_coverage_stage(llm, threshold=0.4)  # coverage 0.5 >= 0.4: no call
    out = asyncio.run(run_envelope_stages([cov.stage()], envs, sctx))
    assert len(out) == 1 and llm.messages == []
    # below the threshold a grounding stage is added by the factory: the hallucination is still stopped
    bad = Stub([{**FACT2, "text": "Bram lost 9 colonies to wasps in August"}])
    envs, sctx = _setup([FACT1])
    out = asyncio.run(run_envelope_stages([make_coverage_stage(bad).stage()], envs, sctx))
    assert len(out) == 1 and sctx.notes["coverage.added_unsupported_dropped"] == 1


# --- the synthetic unseen-domain gate -----------------------------------------------------------
def test_unseen_domain_gate_recall_gain_and_unsupported_additions_are_measured_apart() -> None:
    r = asyncio.run(gate.run())
    assert len(r["domains"]) >= 3 and r["windows"] >= 12
    assert r["recall_after"] > r["recall_before"] and r["omitted_recovered"] >= 8
    assert (
        r["unsupported_added"] == 0 and r["unsupported_stopped"] == r["hallucinated_extras_offered"]
    )
    assert r["false_triggers"] == 0
    assert r["calls_per_window_baseline"] == 1.0 and r["calls_per_window_coverage"] <= 1.6
    assert r["extra_calls"] <= r["windows"]  # at most one extra call per window


# --- through the engine -------------------------------------------------------------------------
LINE = re.compile(r"^\[(\d+)\] \[[\d-]+\] (.+)$", re.M)


class ScriptLLM:
    """First call (structured prompt): items for ``first`` lines only. Targeted call (window prompt):
    items for the lines it is asked about, plus an optional hallucination on the first of them."""

    provider_id = "fake:script"

    def __init__(self, first: int, hallucinate: bool = False) -> None:
        self.first = first
        self.hallucinate = hallucinate
        self.extractions = 0
        self.targeted = 0

    async def chat(self, messages: list[dict[str, str]], **_: Any) -> str:
        user = messages[-1]["content"]
        targeted = messages[0]["content"] != STRUCTURED_SYSTEM
        if targeted:
            self.targeted += 1
            user = user.split("NEW:", 1)[1].split("CONTEXT:", 1)[0]
        else:
            self.extractions += 1
        rows = [(int(n), t) for n, t in LINE.findall(user)]
        if not targeted:
            rows = rows[: self.first]
        items = []
        for n, text in rows:
            who, said = text.split(": ", 1)
            items.append(_item(n, who, f"{who} said {said}", said))
        if targeted and self.hallucinate and rows:
            n, text = rows[0]
            who, said = text.split(": ", 1)
            items.append(_item(n, who, f"{who} said {said} and paid 90 euros", said))
        return json.dumps({"facts": items} if targeted else items)


def _turns() -> list[dict[str, str]]:
    base = datetime(2024, 3, 9, 9, 0, tzinfo=UTC)
    said = [
        "I keep 6 hives behind the Kestrel Lane allotment",
        "I lost two colonies to wasps in August",
        "I harvested 18 kilos of heather honey on the Grindle ridge",
        "I teach the Thursday beginners class at Wren Meadow",
    ]
    return [
        {
            "role": "user",
            "content": f"Ilse: {t}",
            "timestamp": (base + timedelta(minutes=i)).isoformat(),
        }
        for i, t in enumerate(said)
    ]


async def test_end_to_end_one_extra_call_adds_the_omitted_facts_and_the_ledger_counts_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(tap, "event", lambda stage, /, **f: seen.append((stage, f)))
    llm = ScriptLLM(first=2, hallucinate=True)
    eng = await _engine(
        monkeypatch, llm, grounding_v2=True, coverage_v2=True, economy={"ledger_v2": True}
    )
    try:
        await _send(eng, "a", "s1", _turns())
        await eng.flush_windows()
        facts = await _facts(eng)
        assert llm.extractions == 1 and llm.targeted == 1
        assert len(facts) == 4, [f.content for f in facts]
        assert not any("90 euros" in f.content for f in facts)
        win = [f for s, f in seen if s == "write.coverage.window"]
        assert len(win) == 1 and win[0]["calls"] == 1 and win[0]["added"] == 2
        assert win[0]["unsupported_dropped"] == 1 and win[0]["after"] > win[0]["before"]
        report = eng.cost_report()
        assert report["by_stage"]["window_extract"]["model_calls"] == 1
        assert report["by_stage"]["coverage_reextract"]["model_calls"] == 1
        assert report["model_calls"] == 2
        stage = eng._coverage_stage
        assert stage.stats.reextract_calls == 1 and stage.stats.added == 2
    finally:
        await eng.stop()


async def test_end_to_end_complete_extraction_makes_no_extra_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = ScriptLLM(first=4)
    eng = await _engine(monkeypatch, llm, grounding_v2=True, coverage_v2=True)
    try:
        await _send(eng, "a", "s1", _turns())
        await eng.flush_windows()
        assert llm.targeted == 0 and len(await _facts(eng)) == 4
    finally:
        await eng.stop()


async def test_end_to_end_flag_off_makes_no_extra_call_and_deposits_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = ScriptLLM(first=2)
    eng = await _engine(monkeypatch, llm, grounding_v2=True)
    try:
        await _send(eng, "a", "s1", _turns())
        await eng.flush_windows()
        assert llm.targeted == 0 and len(await _facts(eng)) == 2
        assert getattr(eng, "_coverage_stage", None) is None
    finally:
        await eng.stop()
