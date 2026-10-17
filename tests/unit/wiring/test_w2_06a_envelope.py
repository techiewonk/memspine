"""W2-06a: ItemV1 -> AssertionEnvelope compile, per-item validation, one repair re-ask, dead letters
(``write.envelope_v2``). Model-free: a stub window LLM that answers the ``structured`` prompt and the
``structured.repair`` re-ask, hash embedder."""

from __future__ import annotations

import copy
import json
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_window_write import FakeWindowLLM, _facts, _send, _turns

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.derive.compile import RejectionCode, WindowLine
from memspine.derive.envelope_compiler import KIND_CAP, EnvelopeCompiler, KindRegistry, storage_kind
from memspine.derive.failure_ledger import FailureLedger
from memspine.derive.items import ItemValidator, RepairAsk, process_items
from memspine.exceptions import LLMError
from memspine.models.providers.llm.base import LLMRouter
from memspine.observability import tap
from memspine.prompts.structured import (
    STRUCTURED_SYSTEM,
    RepairItem,
    extraction_messages,
    repair_messages,
)
from memspine.schemas.durable import OwnerScope

LINE = re.compile(r"^\[(\d+)\] \[[\d-]+\] (.+)$", re.M)
NOW = datetime(2023, 6, 1, 12, 0, tzinfo=UTC)
SCOPE = OwnerScope(tenant="default", owner="a")


# --- the stub model ----------------------------------------------------------------------------
class ItemLLM:
    """Answers the ``structured`` extraction (one ItemV1 per transcript line) and the repair re-ask."""

    provider_id = "fake:items"

    def __init__(self) -> None:
        self.extractions = 0
        self.repairs: list[list[dict[str, str]]] = []
        self.mutations: dict[int, Callable[[dict[str, Any]], dict[str, Any] | None]] = {}
        self.repair_mode = "fix"  # fix | noop | garbage | extra | raise
        self.legacy = FakeWindowLLM()

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        if messages[0]["content"] != STRUCTURED_SYSTEM:
            return await self.legacy.chat(messages, **options)
        if messages[-1]["content"].find("of your items failed validation") >= 0:
            return self._repair(messages)
        self.extractions += 1
        lines = [(int(n), text) for n, text in LINE.findall(messages[-1]["content"])]
        items: list[dict[str, Any] | None] = []
        for pos, (n, text) in enumerate(lines):
            who, said = text.split(": ", 1)
            item: dict[str, Any] | None = {
                "s": who,
                "r": "said",
                "o": None,
                "text": f"{who} said {said}",
                "kind": "event",
                "mode": "asserted",
                "neg": False,
                "when": None,
                "until": None,
                "lines": [n],
                "span": said,
                "speaker": who,
            }
            if pos in self.mutations:
                item = self.mutations[pos](item)
            if item is not None:
                items.append(item)
        return json.dumps(items)

    def _repair(self, messages: list[dict[str, str]]) -> str:
        self.repairs.append([dict(m) for m in messages])
        if self.repair_mode == "raise":
            raise LLMError("stub: repair failed")
        if self.repair_mode == "garbage":
            return "I cannot do that"
        shown = json.loads(messages[-2]["content"])
        if self.repair_mode == "noop":
            return json.dumps(shown)
        lines = {
            text.split(": ", 1)[1]: int(n) for n, text in LINE.findall(messages[-3]["content"])
        }
        out = []
        for item in shown:
            fixed = copy.deepcopy(item)
            for key in [k for k in fixed if k in ("trust", "user_id", "status")]:
                del fixed[key]
            if not fixed.get("lines") or any(n not in lines.values() for n in fixed["lines"]):
                fixed["lines"] = [lines[fixed["span"]]]
            out.append(fixed)
        if self.repair_mode == "extra":
            out.append({**out[0], "text": "A brand new item the model invented"})
        return json.dumps(out)


async def _engine(
    monkeypatch: pytest.MonkeyPatch,
    llm: Any,
    path: Any = ":memory:",
    *,
    economy: dict[str, Any] | None = None,
    **write: Any,
) -> Engine:
    async def router(config: MemspineConfig, hooks: object = None, wiring: object = None) -> LLMRouter:
        return LLMRouter({"window_extract": llm})

    monkeypatch.setattr("memspine.runtime.container.build_llm_router", router)
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={'provider': "sqlite", 'settings': {'path': str(path), 'busy_timeout_s': 5.0}},
        embedding={'provider': "hash", 'settings': {}},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        write={
            "window_extract": "facts",
            "window_async": False,
            "window_idle_s": 0.0,
            "envelope_v2": True,
            **write,
        },
        llm={"roles": {"window_extract": {"provider": "litellm", "settings": {"model": "fake/window", "timeout_seconds": 60.0}}}},
        economy=economy or {},
    )
    await eng.start()
    return eng


def _lines(*roles: str) -> dict[int, WindowLine]:
    return {
        n: WindowLine(
            turn_id=f"t{n}",
            source_id=f"t{n}",
            source_version=1,
            seg_index=n,
            role=role,
            speaker="Mira",
            source_at=NOW,
            tz="UTC",
            content=f"Mira: I repotted orchid number {n}",
        )
        for n, role in enumerate(roles, 1)
    }


def _item(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "s": "Mira",
        "r": "repotted",
        "o": "the orchid",
        "text": "Mira repotted the orchid",
        "kind": "event",
        "mode": "asserted",
        "neg": False,
        "lines": [1],
        "span": "repotted orchid",
    }
    base.update(over)
    return base


BASE = extraction_messages(
    "[1] [2023-06-01] Mira: repotted orchid\n[2] [2023-06-01] Mira: orchid number 2", "Mira", 2
)


def _setup(*roles: str) -> tuple[EnvelopeCompiler, ItemValidator, Any]:
    compiler = EnvelopeCompiler(clock=lambda: NOW)
    return compiler, ItemValidator(compiler), compiler.context(SCOPE, "w1", _lines(*roles))


# --- config and flag off ----------------------------------------------------------------------
def test_the_flag_is_off_by_default() -> None:
    assert MemspineConfig(runtime={"provider_services": {"health_timeout_s": 1.0, "close_timeout_s": 1.0, "startup_timeout_s": None}}).write.envelope_v2 is False


async def test_flag_off_is_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    llm = FakeWindowLLM()
    eng = await _engine(monkeypatch, llm, tmp_path / "off.db", envelope_v2=False)
    try:
        await _send(eng, "a", "s1", _turns("trip", 4))
        stats = await eng.flush_windows()
        assert "envelope_v2" not in stats and "items_valid" not in stats
        assert eng._windows.failure_ledger is None
        facts = await _facts(eng)
        assert len(facts) == 4 and not any("envelope_v2" in f.tags for f in facts)
        assert not (tmp_path / "off.db.deadletters.sqlite").exists()
    finally:
        await eng.stop()


# --- compile: axes, provenance, evidence, trust (unit) ----------------------------------------
def test_valid_items_compile_with_axes_provenance_and_evidence() -> None:
    _, validator, ctx = _setup("user", "assistant")
    out = validator.validate(
        [_item(), _item(text="The assistant offered a pot", lines=[2], span="orchid number 2")], ctx
    )
    assert not out.invalid and len(out.envelopes) == 2
    user, bot = out.envelopes
    assert user.src_class == "user_stated" and user.assessment.source_trust == pytest.approx(0.8)
    assert bot.src_class == "assistant_proposed" and bot.assessment.source_trust == pytest.approx(
        0.4
    )
    assert user.time.source_time == NOW and user.time.recorded_at == NOW
    assert [e.role for e in user.evidence] == ["support"]
    assert user.evidence[0].turn.source_id == "t1"
    assert user.scope == SCOPE and user.kind == "event"
    assert user.provenance.source_id == "w1"


def test_context_lines_are_binding_evidence_never_support() -> None:
    compiler = EnvelopeCompiler(clock=lambda: NOW)
    lines = _lines("user", "user")
    lines[2] = WindowLine(**{**lines[2].__dict__, "is_context": True})
    ctx = compiler.context(SCOPE, "w1", lines)
    out = ItemValidator(compiler).validate([_item(lines=[1, 2]), _item(lines=[2])], ctx)
    assert [e.role for e in out.envelopes[0].evidence] == ["support", "binding"]
    assert out.invalid[0].rejection.code is RejectionCode.CONTEXT_ONLY


# --- invalid items: citation, reserved fields, kinds ------------------------------------------
def test_missing_and_out_of_range_citations_are_rejected_alone() -> None:
    _, validator, ctx = _setup("user", "user")
    out = validator.validate(
        [_item(lines=[]), _item(lines=[9]), _item(lines=[2], text="ok sibling")], ctx
    )
    codes = [x.rejection.code for x in out.invalid]
    assert codes == [RejectionCode.UNCITED, RejectionCode.CITED_TURN_NOT_IN_WINDOW]
    assert [e.text for e in out.envelopes] == ["ok sibling"]


def test_a_reserved_field_invalidates_the_item_and_never_reaches_the_envelope() -> None:
    _, validator, ctx = _setup("user")
    out = validator.validate(
        [_item(trust=0.99), _item(user_id="u1"), _item(extra_note="kept")], ctx
    )
    assert [x.rejection.code for x in out.invalid] == [RejectionCode.RESERVED_FIELD] * 2
    (env,) = out.envelopes
    assert env.assessment.source_trust == pytest.approx(0.8)  # system-set, not the model's
    assert env.extras["extra_note"] == "kept"  # an unknown, non-reserved field is allowed


def test_new_kinds_are_accepted_with_safe_defaults_and_capped_at_64() -> None:
    compiler, validator, ctx = _setup("user")
    assert KIND_CAP == 64
    out = validator.validate([_item(kind="Hobbies", text=f"t{i}") for i in range(2)], ctx)
    assert out.envelopes[0].kind == "hobby"  # normalised and accepted
    assert storage_kind("hobby") == "event" and storage_kind("state") == "state"
    items = [_item(kind=f"kind_{i}", text=f"statement {i}") for i in range(100)]
    out = validator.validate(items, ctx)
    assert len(compiler.kinds) == KIND_CAP
    kinds = {e.kind for e in out.envelopes}
    assert "other" in kinds and len(kinds) <= KIND_CAP
    assert compiler.kinds.capped > 0
    small = KindRegistry(cap=len(KindRegistry()) + 1)
    assert (small("x"), small("y")) == ("x", "other")


# --- repair (unit) ------------------------------------------------------------------------------
async def test_repair_carries_only_invalid_items_and_forbids_new_ones() -> None:
    llm = ItemLLM()
    llm.repair_mode = "extra"
    _, validator, ctx = _setup("user", "user")
    ledger = FailureLedger()
    raw = [_item(lines=[1]), _item(lines=[], text="needs a citation", span="orchid number 2")]
    out = await process_items(
        raw, ctx, validator=validator, repair=RepairAsk(llm), ledger=ledger,
        base_messages=BASE, ns="a", window_id="w1",
    )  # fmt: skip
    assert out.repair_calls == 1 and out.repaired == 1
    sent = llm.repairs[0]
    assert out.dead == 0
    shown = json.loads(sent[-2]["content"])
    assert (
        len(shown) == 1 and shown[0]["text"] == "needs a citation"
    )  # the valid sibling is not resent
    assert "Mira repotted the orchid" not in sent[-1]["content"]  # errors name codes, not text
    assert (
        len(out.envelopes) == 2
    )  # the sibling plus the repaired one; the invented item is dropped


async def test_repair_failure_modes_dead_letter_the_item_and_keep_siblings() -> None:
    for mode in ("noop", "garbage", "raise"):
        llm = ItemLLM()
        llm.repair_mode = mode
        _, validator, ctx = _setup("user")
        ledger = FailureLedger()
        out = await process_items(
            [_item(), _item(lines=[], text="bad")], ctx, validator=validator,
            repair=RepairAsk(llm), ledger=ledger, base_messages=BASE, ns="a", window_id="w1",
        )  # fmt: skip
        assert len(out.envelopes) == 1 and out.dead == 1 and out.repair_calls == 1, mode
        assert len(llm.repairs) == 1  # at most one re-ask
        assert ledger.count("a") == 1


async def test_taps_report_valid_repaired_and_dead_with_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(tap, "event", lambda stage, /, **f: seen.append((stage, f)))
    llm = ItemLLM()
    _, validator, ctx = _setup("user")
    raw = [
        _item(),
        _item(lines=[], text="fixable", span="orchid number 1"),
        _item(trust=1, text="x"),
    ]
    llm.repair_mode = "fix"
    await process_items(
        raw, ctx, validator=validator, repair=RepairAsk(llm), ledger=None,
        base_messages=extraction_messages(
            "[1] [2023-06-01] Mira: orchid number 1\n[2] [2023-06-01] Mira: repotted orchid",
            "Mira",
            2,
        ),
        ns="a", window_id="w1",
    )  # fmt: skip
    stages = [s for s, _ in seen]
    assert stages.count("write.item.valid") == 1 and stages.count("write.item.repaired") == 2
    assert "write.item.dead" not in stages
    llm.repair_mode = "noop"
    seen.clear()
    await process_items(
        [_item(lines=[])], ctx, validator=validator, repair=RepairAsk(llm), ledger=None,
        base_messages=BASE, ns="a", window_id="w2",
    )  # fmt: skip
    dead = [f for s, f in seen if s == "write.item.dead"]
    assert dead and dead[0]["reason"] == "uncited"


def test_repair_prompt_is_content_free_and_layout_stable() -> None:
    a = extraction_messages("[1] [2023-06-01] Mira: a", "Mira", 1)
    b = extraction_messages("[1] [2023-06-01] Mira: totally different", "Noor, Ilse", 7)
    assert a[0] == b[0]  # the system part is byte-identical for every call (W2-07)
    msgs = repair_messages(a, [RepairItem(0, {"text": "t"}, ("uncited: cite a line",))])
    assert msgs[: len(a)] == list(a) and msgs[-2]["role"] == "assistant"


# --- the dead-letter list ---------------------------------------------------------------------
def test_dead_letters_persist_without_content(tmp_path: Path) -> None:
    path = str(tmp_path / "dl.sqlite")
    ledger = FailureLedger(path)
    ledger.record("a", "w1", 3, "uncited", detail="uncited", attempts=2, item={"text": "SECRET"})
    ledger.close()
    again = FailureLedger(path)
    (letter,) = again.list("a")
    assert (letter.code, letter.item_index, letter.attempts) == ("uncited", 3, 2)
    assert letter.item_hash and letter.content is None  # a hash, never the text
    assert "SECRET" not in json.dumps(letter.__dict__)
    again.close()


# --- through the engine -------------------------------------------------------------------------
async def test_valid_items_become_facts_with_envelope_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = ItemLLM()
    eng = await _engine(monkeypatch, llm)
    try:
        await _send(eng, "a", "s1", _turns("trip", 4))
        stats = await eng.flush_windows()
        facts = await _facts(eng)
        assert len(facts) == 4 and llm.extractions == 1 and not llm.repairs
        assert all({"envelope_v2", "src:user_stated"} <= set(f.tags) for f in facts)
        assert all(len(f.source.parents) == 1 for f in facts)
        assert stats["envelope_v2"] and stats["items_valid"] == 4 and stats["items_dropped"] == 0
        assert stats["repair_calls"] == 0 and stats["facts_deposited"] == 4
        assert stats["dead_letters"] == 0
    finally:
        await eng.stop()


async def test_a_bad_item_is_dropped_counted_and_its_siblings_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    llm = ItemLLM()
    llm.mutations = {1: lambda i: {**i, "lines": []}, 2: lambda i: {**i, "lines": [99]}}
    llm.repair_mode = "noop"
    eng = await _engine(monkeypatch, llm, tmp_path / "d.db")
    try:
        await _send(eng, "a", "s1", _turns("trip", 4))
        stats = await eng.flush_windows()
        assert len(await _facts(eng)) == 2  # the two valid siblings
        assert (
            stats["items_valid"] == 2 and stats["items_dead"] == 2 and stats["items_dropped"] == 2
        )
        assert stats["items_dead_by_code"] == {"uncited": 1, "cited_turn_not_in_window": 1}
        assert stats["repair_calls"] == 1 and len(llm.repairs) == 1  # ONE re-ask for both
        assert stats["sections_dropped"] == 0
    finally:
        await eng.stop()


async def test_a_reserved_field_is_repaired_and_a_new_kind_is_stored_as_an_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = ItemLLM()
    llm.mutations = {0: lambda i: {**i, "trust": 1.0}, 1: lambda i: {**i, "kind": "hobbies"}}
    eng = await _engine(monkeypatch, llm)
    try:
        await _send(eng, "a", "s1", _turns("trip", 3))
        stats = await eng.flush_windows()
        facts = await _facts(eng)
        assert len(facts) == 3 and stats["items_repaired"] == 1 and stats["items_dead"] == 0
        assert all(f.trust <= 0.8 + 1e-9 for f in facts)  # never the model's 1.0
        assert any("kind:event" in f.tags for f in facts)
        assert "kind:hobbies" not in [t for f in facts for t in f.tags]
        assert stats["kinds"] >= 9
    finally:
        await eng.stop()


async def test_repair_fixes_one_item_with_one_extra_call(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = ItemLLM()
    llm.mutations = {2: lambda i: {**i, "lines": []}}
    eng = await _engine(monkeypatch, llm)
    try:
        await _send(eng, "a", "s1", _turns("trip", 4))
        stats = await eng.flush_windows()
        assert len(await _facts(eng)) == 4
        assert stats["items_valid"] == 3 and stats["items_repaired"] == 1
        assert stats["items_dead"] == 0 and stats["dead_letters"] == 0
        assert llm.extractions == 1 and len(llm.repairs) == 1
    finally:
        await eng.stop()


async def test_a_still_invalid_item_survives_a_restart_in_the_dead_letters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    llm = ItemLLM()
    llm.mutations = {0: lambda i: {**i, "lines": []}}
    llm.repair_mode = "noop"
    db = tmp_path / "dead.db"
    eng = await _engine(monkeypatch, llm, db)
    try:
        await _send(eng, "a", "s1", _turns("trip", 3))
        await eng.flush_windows()
        assert len(await _facts(eng)) == 2  # siblings kept
        (letter,) = eng._windows.failure_ledger.list("a")
        assert letter.code == "uncited" and letter.attempts == 2 and letter.item_hash
        assert letter.content is None
    finally:
        await eng.stop()
    eng2 = await _engine(monkeypatch, ItemLLM(), db)
    try:
        (again,) = eng2._windows.failure_ledger.list("a")
        assert (again.id, again.code, again.window_id) == (letter.id, "uncited", letter.window_id)
        assert eng2._windows.stats()["dead_letters"] == 1
    finally:
        await eng2.stop()


async def test_a_failed_facts_call_is_a_window_level_dead_letter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Garbage(ItemLLM):
        async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
            return "not json at all {{{"

    eng = await _engine(monkeypatch, Garbage())
    try:
        await _send(eng, "a", "s1", _turns("trip", 3))
        stats = await eng.flush_windows()
        assert stats["parse_failures"] == 1 and stats["sections_dropped"] == 1
        (letter,) = eng._windows.failure_ledger.list("a")
        assert letter.kind == "window" and letter.code == "parse_failures" and letter.retry_allowed
    finally:
        await eng.stop()


async def test_repair_calls_are_counted_by_the_cost_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = ItemLLM()
    llm.mutations = {0: lambda i: {**i, "lines": []}}
    eng = await _engine(monkeypatch, llm, economy={"ledger_v2": True})
    try:
        await _send(eng, "a", "s1", _turns("trip", 3))
        await eng.flush_windows()
        report = eng.cost_report()
        assert llm.extractions == 1 and len(llm.repairs) == 1
        assert report["model_calls"] == 2  # the extraction and the repair re-ask
        assert report["by_stage"]["window_extract"]["model_calls"] == 2
        assert report["by_layer"]["facts"]["model_calls"] == 2
    finally:
        await eng.stop()


async def test_batched_windows_keep_citations_per_window(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = ItemLLM()
    eng = await _engine(monkeypatch, llm, segmentation_v2=True, segment_max_turns=6)
    try:
        await _send(eng, "a", "s1", _turns("trip", 18))
        stats = await eng.flush_windows()
        assert stats["batches"] >= 1 and stats["batched_windows"] >= 2
        assert llm.extractions < stats["windows"]  # fewer calls than windows
        facts = await _facts(eng)
        assert len(facts) == 18 and stats["items_dead"] == 0 and stats["batch_cross_window"] == 0
        storage = eng._require_started()
        for fact in facts:  # each fact is parented on the very turn it was said in
            (pid,) = fact.source.parents
            turn = await storage.get_record(pid)
            assert turn is not None and turn.content.split(": ", 1)[1] in fact.content
    finally:
        await eng.stop()
