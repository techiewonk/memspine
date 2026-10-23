"""RW-D: R3-11 gate_v2, R3-12 deep_v2, R3-16 obligations (model-free: hash embedder, stub model)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from memspine import Engine
from memspine.compose.pack import AssembledContext
from memspine.read.contract_rules import build_contract
from memspine.read.pipeline import deep as pdeep
from memspine.read.pipeline.gate import _gate_contract
from memspine.read.pipeline.state import ReadResult
from memspine.store.records import MemoryRecord


def _engine(tmp_path: Any, **read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": str(tmp_path / "t.db"), "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        read={"rerank": "off", "record_access": False, **read},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
    )


async def _store(eng: Engine) -> None:
    await eng.ingest_messages(
        [
            {"role": "user", "content": "Ilse lives in Rotterdam and works as a nurse."},
            {"role": "user", "content": "Ilse's dentist is Bram and his clinic is in Delft."},
            {"role": "user", "content": "Ilse bought a red bicycle last spring."},
        ],
        namespace="n",
        session_id="s1",
        valid_from=datetime(2024, 5, 1, tzinfo=UTC),
    )


def test_gate_contract_maps_typed_fields() -> None:
    plain = _gate_contract("What is the capital of France?", False, False)
    assert plain.personal is False and plain.request_kind == "lookup"
    named = _gate_contract("Where does Ilse live?", True, True)
    assert named.named_reference is True


@pytest.mark.parametrize(
    ("query", "typed_kind", "typed_view", "unsupported_kind", "supported_view"),
    [
        ("Where did Ilse live as of 1998?", "as_of", "as_of", "lookup", "as_of"),
        # A range retains the as-of request kind, but GateContract has no range view.
        ("Where did Ilse live in 1998?", "as_of", "range", "lookup", "current"),
        # Enumeration remains an independent bypass even without temporal support.
        (
            "What is the history of Ilse home cities?",
            "enumeration",
            "history",
            "enumeration",
            "history",
        ),
    ],
)
def test_gate_contract_time_view_needs_support(
    query: str,
    typed_kind: str,
    typed_view: str,
    unsupported_kind: str,
    supported_view: str,
) -> None:
    typed = build_contract(query)
    assert (typed.request_kind, typed.time_view.kind) == (typed_kind, typed_view)

    unsupported = _gate_contract(query, False, False)
    assert (unsupported.request_kind, unsupported.time_view) == (unsupported_kind, "current")
    supported = _gate_contract(query, False, True)
    assert (supported.request_kind, supported.time_view) == (typed_kind, supported_view)


@pytest.mark.parametrize("supported", [False, True])
def test_gate_contract_non_temporal_input_stays_current(supported: bool) -> None:
    query = "Where does Ilse live?"
    typed = build_contract(query)
    assert (typed.request_kind, typed.time_view.kind) == ("lookup", "unspecified")
    contract = _gate_contract(query, False, supported)
    assert (contract.request_kind, contract.time_view) == ("lookup", "current")


async def test_gate_v2_off_is_untouched(tmp_path: Any) -> None:
    eng = _engine(tmp_path)
    await eng.start()
    try:
        await _store(eng)
        res = await eng.read("What is the capital of France?", namespace="n", mode="compose")
        assert res.context.records  # no gate: today's behaviour
    finally:
        await eng.stop()


async def test_gate_v2_generic_question_pulls_no_memory(tmp_path: Any) -> None:
    eng = _engine(tmp_path, gate_v2=True)
    await eng.start()
    try:
        await _store(eng)
        generic = await eng.read("What is the capital of France?", namespace="n", mode="compose")
        assert generic.context.abstained and not generic.context.records
        personal = await eng.read("Where does Ilse live?", namespace="n", mode="compose")
        assert personal.context.records and not personal.context.abstained
    finally:
        await eng.stop()


async def test_gate_v2_unknown_name_does_not_bypass(tmp_path: Any) -> None:
    eng = _engine(tmp_path, gate_v2=True)
    await eng.start()
    try:
        await _store(eng)
        res = await eng.read("Where does Zorbulon live?", namespace="n", mode="compose")
        assert not res.context.records
    finally:
        await eng.stop()


async def test_gate_v2_weak_abstain_option(tmp_path: Any) -> None:
    eng = _engine(tmp_path, gate_v2=True, gate_v2_weak="abstain", gate_v2_named="scored")
    await eng.start()
    try:
        await _store(eng)
        res = await eng.read("What is the capital of France?", namespace="n", mode="compose")
        assert res.context.abstained
    finally:
        await eng.stop()


# ----------------------------------------------------------------------------- obligations / deep


def _fact(entity: str, attribute: str, value: str, *, day: int = 1) -> MemoryRecord:
    return MemoryRecord(
        namespace="n",
        memory_type="semantic",
        content=value,
        entity=entity,
        attribute=attribute,
        valid_from=datetime(2024, 1, day, tzinfo=UTC),
    )


def test_query_description_maps_contract() -> None:
    desc = pdeep._query_description(build_contract("How long did Ilse live in Rotterdam?"))
    assert desc.kind in ("duration", "single")
    desc = pdeep._query_description(build_contract("List all the cities Ilse lived in."))
    assert desc.kind in ("list", "count", "single")


def test_evidence_bundle_only_keyed_semantic_facts() -> None:
    raw = MemoryRecord(namespace="n", memory_type="episodic", content="hello there")
    bundle = pdeep._evidence_bundle([_fact("Ilse", "city", "Rotterdam"), raw])
    assert [f.entity for f in bundle.facts] == ["Ilse"]
    assert bundle.exhaustive is False


class _Role:
    pass


def _fake_engine(cfg: Any, search_hits: list[MemoryRecord]) -> Any:
    calls: list[str] = []

    async def search(text: str, ns: str, k: int, **kw: Any) -> list[tuple[MemoryRecord, float]]:
        calls.append(text)
        return [(r, 1.0) for r in search_hits]

    async def decorate(ns: str, pairs: list[Any], hide: Any = None) -> list[Any]:
        return pairs

    eng = SimpleNamespace(
        _config=lambda: SimpleNamespace(read=cfg),
        _llm=SimpleNamespace(roles=["sufficiency"], for_role=lambda r: object()),
        _prompts=SimpleNamespace(select=lambda *a, **k: object()),
        _search=search,
        _decorate=decorate,
        searches=calls,
    )

    def assemble(
        query: str, result: ReadResult, added: Any, displaced: Any, info: Any
    ) -> ReadResult:
        info["assembled"] = [r.record_id for r in added]
        recs = [*result.context.records, *added]
        return ReadResult(result.mode, AssembledContext(records=recs))

    eng._agentic_assemble = assemble
    return eng


def _cfg(**kw: Any) -> Any:
    base = {
        "deep_v2": True,
        "obligations": True,
        "agentic_max_new": 4,
        "agentic_top_k": 5,
        "agentic_first_share": 0.7,
    }
    return SimpleNamespace(**{**base, **kw})


async def test_deep_rules_met_makes_no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []

    async def boom(*a: Any, **k: Any) -> Any:
        called.append(1)
        raise AssertionError("model must not be called")

    monkeypatch.setattr(pdeep, "structured_call", boom)
    head = _fact("Ilse", "city", "Rotterdam")
    eng = _fake_engine(_cfg(), [])
    result = ReadResult("compose", AssembledContext(records=[head]))
    out = await pdeep._deep_extend(
        eng, "Where does Ilse live?", "n", result, 500, hide=None, session_id=None
    )
    assert out is result and not called
    assert not eng.searches


async def test_deep_open_hole_runs_one_judge_one_rewrite_one_retrieve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from memspine.prompts.models import MissingInfoOut, SufficiencyOut

    seen: list[str] = []

    async def fake(llm: Any, prompt: Any, values: Any, model: Any) -> Any:
        seen.append(model.__name__)
        if model is SufficiencyOut:
            return SufficiencyOut(complete=False)
        return MissingInfoOut(queries=["Ilse city Utrecht"])

    monkeypatch.setattr(pdeep, "structured_call", fake)
    extra = _fact("Ilse", "city", "Utrecht", day=5)
    eng = _fake_engine(_cfg(), [extra])
    # a raw turn only: the join obligation of "Ilse's dentist's city" stays open
    raw = MemoryRecord(namespace="n", memory_type="episodic", content="Ilse sees a dentist")
    result = ReadResult("compose", AssembledContext(records=[raw]))
    out = await pdeep._deep_extend(
        eng, "Where does Ilse live?", "n", result, 500, hide=None, session_id=None
    )
    assert seen == ["SufficiencyOut", "MissingInfoOut"]  # one judge, one rewrite, never more
    assert eng.searches  # the one retrieval ran its queries
    assert extra.record_id in [r.record_id for r in out.context.records]


async def test_deep_unbound_role_makes_zero_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("no model call without a bound role")

    monkeypatch.setattr(pdeep, "structured_call", boom)
    eng = _fake_engine(_cfg(), [])
    eng._llm = SimpleNamespace(roles=[], for_role=lambda r: object())
    raw = MemoryRecord(namespace="n", memory_type="episodic", content="Ilse sees a dentist")
    result = ReadResult("compose", AssembledContext(records=[raw]))
    out = await pdeep._deep_extend(
        eng, "Where does Ilse live?", "n", result, 500, hide=None, session_id=None
    )
    assert out is result and not eng.searches


async def test_obligations_only_runs_no_deep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("obligations alone never call a model")

    monkeypatch.setattr(pdeep, "structured_call", boom)
    eng = _fake_engine(_cfg(deep_v2=False), [])
    raw = MemoryRecord(namespace="n", memory_type="episodic", content="Ilse sees a dentist")
    result = ReadResult("compose", AssembledContext(records=[raw]))
    out = await pdeep._deep_extend(
        eng, "Where does Ilse live?", "n", result, 500, hide=None, session_id=None
    )
    assert out is result and not eng.searches
