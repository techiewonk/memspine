"""W2-08: grounding stage (``write.grounding_v2``, envelope-stage order 200). Model-free: envelopes are
built through the real compiler from hand-written lines (invented names), then one end-to-end window
runs with ``write.envelope_v2`` + the flag and the stub window LLM."""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "evals"))
from grounding_gate import run as gate_run
from test_w2_06a_envelope import ItemLLM, _engine
from test_window_write import _facts, _send

from memspine.config.schema import MemspineConfig
from memspine.derive.compile import WindowLine
from memspine.derive.envelope_compiler import EnvelopeCompiler
from memspine.derive.envelope_stages import EnvelopeStageContext, run_envelope_stages
from memspine.derive.grounding_stage import (
    GROUND_STAGE_ORDER,
    Adjudicator,
    build_grounding_stage,
    lexical_support,
    make_grounding_stage,
)
from memspine.derive.items import ItemValidator
from memspine.ingest.windows import _envelope_stages
from memspine.observability import tap
from memspine.schemas.durable import OwnerScope

NOW = datetime(2023, 6, 1, 12, 0, tzinfo=UTC)
SCOPE = OwnerScope(tenant="default", owner="ilse")


def _line(n: int, role: str, speaker: str, text: str) -> WindowLine:
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


LINES = {
    1: _line(1, "user", "Ilse", "I paid forty euros for the Utrecht ticket last week."),
    2: _line(2, "user", "Bram", "I don't eat meat any more."),
    3: _line(3, "assistant", "Assistant", "I suggest we book the canal tour for Saturday."),
    4: _line(4, "user", "Ilse", "Yes please, go ahead."),
    5: _line(5, "assistant", "Assistant", "I suggest we move the meeting to Tuesday."),
    6: _line(6, "user", "Ilse", "ok, thanks"),
}


def _item(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "s": "Ilse",
        "r": "paid",
        "o": "the ticket",
        "text": "Ilse paid forty euros for the Utrecht ticket",
        "kind": "event",
        "mode": "asserted",
        "neg": False,
        "lines": [1],
        "span": "paid forty euros",
        "speaker": "Ilse",
    }
    base.update(over)
    return base


def _compile(items: list[dict[str, Any]]) -> tuple[list[Any], EnvelopeStageContext]:
    compiler = EnvelopeCompiler(clock=lambda: NOW)
    ctx = compiler.context(SCOPE, "w1", LINES, source_time=NOW)
    out = ItemValidator(compiler).validate(items, ctx)
    assert not out.invalid, out.invalid
    sctx = EnvelopeStageContext(
        namespace="a",
        window_id="w1",
        lines=LINES,
        records=[],
        compile_ctx=ctx,
        services={},
        notes=Counter(),
    )
    return list(out.envelopes), sctx


def _run(stage: Any, envs: list[Any], ctx: EnvelopeStageContext) -> list[Any]:
    return asyncio.run(run_envelope_stages([stage], envs, ctx))


# --- config and registration --------------------------------------------------------------------
def test_flag_off_registers_nothing_and_on_registers_one_stage_at_200() -> None:
    off = MemspineConfig(
        runtime={
            "provider_services": {
                "health_timeout_s": 1.0,
                "close_timeout_s": 1.0,
                "startup_timeout_s": None,
            }
        }
    )
    assert off.write.grounding_v2 is False and off.write.ground_action == "drop"
    assert off.write.ground_adjudicator == "lexical"
    assert _envelope_stages(SimpleNamespace(), off) == ()
    on = MemspineConfig(
        write={"window_extract": "facts", "envelope_v2": True, "grounding_v2": True},
        runtime={
            "provider_services": {
                "health_timeout_s": 1.0,
                "close_timeout_s": 1.0,
                "startup_timeout_s": None,
            }
        },
    )
    stages = _envelope_stages(SimpleNamespace(), on)
    assert [(s.name, s.order) for s in stages] == [
        ("grounding", GROUND_STAGE_ORDER)
    ] and GROUND_STAGE_ORDER == 200


# --- rule checks and spans ----------------------------------------------------------------------
def test_a_grounded_item_is_kept_and_gets_span_offsets_on_its_evidence() -> None:
    envs, ctx = _compile([_item()])
    out = _run(make_grounding_stage(), envs, ctx)
    assert len(out) == 1 and out[0].grounding == "grounded" and out[0].status == "active"
    span = out[0].evidence[0].span
    assert span is not None
    content = LINES[1].content or ""
    assert content[span.start : span.end] == span.quote and "forty euros" in (span.quote or "")
    assert ctx.notes["ground.grounded"] == 1 and out[0].checks is not None


def test_a_wrong_number_is_dropped_with_a_reason_code() -> None:
    envs, ctx = _compile([_item(text="Ilse paid 90 euros for the Utrecht ticket")])
    out = _run(make_grounding_stage(), envs, ctx)
    assert out == [] and ctx.notes["ground.dropped"] == 1
    assert any(k.startswith("ground.dropped.numbers") for k in ctx.notes)


def test_flipped_negation_is_dropped() -> None:
    envs, ctx = _compile(
        [
            _item(
                s="Bram",
                text="Bram eats meat",
                lines=[2],
                speaker="Bram",
                span="eat meat",
                r="eats",
                o="meat",
            )
        ]
    )
    out = _run(make_grounding_stage(), envs, ctx)
    assert out == [] and any(k.startswith("ground.dropped.") and "polarity" in k for k in ctx.notes)


def test_wrong_speaker_is_dropped_by_the_asserter_check() -> None:
    envs, ctx = _compile([_item(reported_speaker="Bram")])  # line 1 is Ilse's
    out = _run(make_grounding_stage(), envs, ctx)
    assert out == [] and any("asserter" in k for k in ctx.notes)


def test_tentative_action_keeps_the_item_marked() -> None:
    envs, ctx = _compile([_item(text="Ilse paid 90 euros for the Utrecht ticket")])
    out = _run(make_grounding_stage(action="tentative"), envs, ctx)
    assert len(out) == 1 and out[0].status == "tentative" and out[0].grounding == "rejected"
    assert ctx.notes["ground.tentative"] == 1 and "ground.dropped" not in ctx.notes


def test_siblings_survive_a_dropped_item() -> None:
    envs, ctx = _compile([_item(), _item(text="Ilse paid 90 euros for the Utrecht ticket")])
    out = _run(make_grounding_stage(), envs, ctx)
    assert len(out) == 1 and out[0].text.startswith("Ilse paid forty")


# --- assent ---------------------------------------------------------------------------------------
def _proposal(line_no: int, text: str) -> dict[str, Any]:
    return _item(
        s="Assistant",
        mode="planned",
        r="offered",
        o="the canal tour",
        text=text,
        lines=[line_no],
        span="book the canal tour" if line_no == 3 else "move the meeting",
        speaker="Assistant",
    )


def test_a_real_user_yes_promotes_an_assistant_proposal() -> None:
    envs, ctx = _compile([_proposal(3, "The assistant proposed to book the canal tour")])
    assert envs[0].src_class == "assistant_proposed"
    out = _run(make_grounding_stage(), envs, ctx)
    assert len(out) == 1 and out[0].src_class == "user_confirmed"
    assert [e.role for e in out[0].evidence][-1] == "binding" and out[0].evidence[
        -1
    ].turn.seg_index == 4
    assert out[0].assessment.source_trust >= 0.8 and ctx.notes["ground.assent.confirmed"] == 1


def test_ok_thanks_does_not_promote() -> None:
    envs, ctx = _compile([_proposal(5, "The assistant proposed to move the meeting to Tuesday")])
    out = _run(make_grounding_stage(), envs, ctx)
    assert len(out) == 1 and out[0].src_class == "assistant_proposed"
    assert "ground.assent.confirmed" not in ctx.notes


def test_assent_off_leaves_the_proposal_alone() -> None:
    envs, ctx = _compile([_proposal(3, "The assistant proposed to book the canal tour")])
    out = _run(make_grounding_stage(check_assent=False), envs, ctx)
    assert out[0].src_class == "assistant_proposed"


# --- adjudicator ----------------------------------------------------------------------------------
class _D:
    def __init__(self, label: str | None, conf: float | None = 0.9, boom: bool = False) -> None:
        self.label, self.conf, self.boom, self.calls = label, conf, boom, 0

    async def decide(self, task: str, question: str, context: str | None = None) -> Any:
        self.calls += 1
        if self.boom:
            raise KeyError(task)
        return SimpleNamespace(label=self.label, confidence=self.conf)


def test_lexical_support_and_fallback_rules() -> None:
    assert lexical_support("Noor visited Utrecht", "Noor: I visited Utrecht yesterday") == 1.0
    a = Adjudicator(None)
    assert (
        asyncio.run(a.adjudicate("Noor visited Utrecht", "Noor visited Utrecht")).label
        == "supported"
    )
    # other-language evidence is never rejected by overlap
    assert (
        asyncio.run(
            a.adjudicate("Noor visited Utrecht", "Noor hat gestern die Stadt besucht")
        ).label
        == "undecided"
    )
    # same-language evidence with almost no overlap is
    assert (
        asyncio.run(
            a.adjudicate("Noor visited Utrecht", "the weather is mild and the park is open")
        ).label
        == "unsupported"
    )


def test_decider_verdict_is_used_when_confident_and_lexical_otherwise() -> None:
    claim, ev = "Noor visited Utrecht", "Noor hat gestern die Stadt besucht"
    yes = Adjudicator(_D("grounded"))
    assert asyncio.run(yes.adjudicate(claim, ev)).label == "supported"
    no = Adjudicator(_D("ungrounded"))
    assert asyncio.run(no.adjudicate(claim, ev)).label == "unsupported"
    unsure = Adjudicator(_D("ungrounded", 0.52))
    assert asyncio.run(unsure.adjudicate(claim, ev)).label == "undecided"
    broken = Adjudicator(_D(None, boom=True))
    assert (
        asyncio.run(broken.adjudicate(claim, ev)).label == "undecided"
        and broken.decider_failures == 1
    )


def test_undecided_items_are_adjudicated_and_counted() -> None:
    # a paraphrase the rules cannot place: they say undecided, the adjudicator accepts it
    envs, ctx = _compile(
        [_item(text="Ilse bought the Utrecht ticket", r="bought", span="paid forty euros")]
    )
    out = _run(make_grounding_stage(decider=_D("grounded")), envs, ctx)
    assert len(out) == 1 and out[0].grounding in ("grounded", "uncertain")
    off = _run(
        make_grounding_stage(adjudicate=False),
        *_compile([_item(text="Ilse bought the Utrecht ticket", r="bought")]),
    )
    assert len(off) == 1  # undecided is kept, never dropped, without an adjudicator


def test_build_from_config_wires_the_decider_only_when_asked() -> None:
    cfg = MemspineConfig(
        write={
            "window_extract": "facts",
            "envelope_v2": True,
            "grounding_v2": True,
            "ground_adjudicator": "off",
        },
        runtime={
            "provider_services": {
                "health_timeout_s": 1.0,
                "close_timeout_s": 1.0,
                "startup_timeout_s": None,
            }
        },
    )
    assert build_grounding_stage(SimpleNamespace(), cfg).order == 200


# --- the gate on the X-05 labelled set ------------------------------------------------------------
def test_labelled_set_rules_plus_lexical_precision_floor() -> None:
    r = asyncio.run(gate_run("lexical"))
    assert r["n"] == 62
    # a dropped assertion is (almost) never a supported one; what the CPU path cannot place stays kept
    assert r["ungrounded_precision"] >= 0.95
    assert r["supported_precision"] >= 0.95
    assert r["grounding_rate"] >= 0.84


# --- through the engine ---------------------------------------------------------------------------
async def test_end_to_end_window_drops_the_ungrounded_item_and_keeps_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(tap, "event", lambda stage, /, **f: seen.append((stage, f)))
    llm = ItemLLM()
    llm.mutations[1] = lambda item: {**item, "text": "Mira said she repotted 99 orchids"}
    eng = await _engine(monkeypatch, llm, grounding_v2=True)
    try:
        await _send(eng, "a", "s1", _w2_turns())
        stats = await eng.flush_windows()
        facts = await _facts(eng)
        checked = [f for s, f in seen if s == "write.ground.checked"]
        assert stats["items_valid"] == 4 and len(checked) == 4, seen
        assert len(facts) == 3, [(f["label"], f["reasons"]) for f in checked]
        assert "99" not in " ".join(f.content for f in facts)
        dropped = [f for s, f in seen if s == "write.ground.dropped"]
        assert len(dropped) == 1 and any("numbers" in r for r in dropped[0]["reasons"])
        summary = [f for s, f in seen if s == "write.ground.summary"]
        assert summary and summary[0]["before"] == 4 and summary[0]["after"] == 3
    finally:
        await eng.stop()


async def test_end_to_end_flag_off_deposits_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = ItemLLM()
    llm.mutations[1] = lambda item: {**item, "text": "Mira said she repotted 99 orchids"}
    eng = await _engine(monkeypatch, llm)
    try:
        await _send(eng, "a", "s1", _w2_turns())
        await eng.flush_windows()
        assert len(await _facts(eng)) == 4
    finally:
        await eng.stop()


def _w2_turns() -> list[dict[str, str]]:
    base = datetime(2023, 6, 1, 9, 0, tzinfo=UTC)
    said = [
        "I repotted the orchid by the window this morning",
        "I repotted two orchids in the hallway after lunch",
        "I watered the ferns on the balcony before dinner",
        "I trimmed the basil and the mint in the kitchen",
    ]
    from datetime import timedelta

    return [
        {
            "role": "user",
            "content": f"Mira: {t}",
            "timestamp": (base + timedelta(minutes=i)).isoformat(),
        }
        for i, t in enumerate(said)
    ]
