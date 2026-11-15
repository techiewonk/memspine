"""W2-06c: the W2-13 pre-pass and W2-09 resolver as KNOWN sources of the W2-06b prompt. Model-free."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_w2_06a_envelope import _engine
from test_w2_06b_prompt import WindowLLM
from test_window_write import _send

from memspine.derive.entity_registry import EntityRegistry
from memspine.derive.known_sources import prepass_known, resolver_known
from memspine.derive.prepass import Prepass
from memspine.derive.prompt_context import KnownEntity, KnownVisibility, PromptContextBuilder

T0 = datetime(2023, 6, 1, 9, 0, tzinfo=UTC)
TEXT = (
    "[1] [2023-06-01] Mira: Bram booked a trip to Kelmark.\n[2] [2023-06-01] Mira: Ilsa said hello."
)


class _Stage:
    def __init__(self) -> None:
        self.registry = EntityRegistry()


async def test_prepass_source_reports_entity_and_alias_spans() -> None:
    pre = Prepass("rules")
    pre.learn(["Ilsa"], namespace="a")
    found = await prepass_known(pre)("a", TEXT)
    names = {e.name for e in found}
    assert {"Bram", "Kelmark", "Ilsa"} <= names
    assert not any(n.startswith("[") for n in names)  # the line prefix is stripped


async def test_resolver_source_reads_the_registry_aliases_and_namespace() -> None:
    stage = _Stage()
    stage.registry.create(("a",), "e1", "Ilse Verhoeven")
    stage.registry.add_alias(("a",), "e1", "Ilsa")
    stage.registry.create(("b",), "e2", "Noor")
    found = await resolver_known(stage)("a", TEXT)
    assert [(e.name, e.aliases) for e in found] == [("Ilse Verhoeven", ("Ilsa",))]


async def test_known_merges_both_sources_capped_and_deduplicated() -> None:
    pre = Prepass("rules")
    stage = _Stage()
    stage.registry.create(("a",), "e1", "Bram Tomasz", context="")
    stage.registry.add_alias(("a",), "e1", "Bram")  # alias equals the pre-pass surface
    for i in range(10):
        stage.registry.create(("a",), f"x{i}", f"Person{i}")

    async def store(_ns: str, _text: str) -> Sequence[KnownEntity]:
        return [KnownEntity("Kelmark", ("Kelm",))]

    builder = PromptContextBuilder(
        store, cap=6, prepass=prepass_known(pre), resolver=resolver_known(stage)
    )
    built = await builder.build("a", TEXT, [])
    lines = built.known.splitlines()
    names = [ln.split(" = ")[0] for ln in lines]
    assert built.known_count == len(lines) == 6  # capped
    assert len(names) == len({n.casefold() for n in names})  # deduplicated
    assert "Kelmark" in names  # named in the NEW lines: kept ahead of the cap
    assert any("Bram" in ln for ln in lines)
    assert set(built.by_source) == {"prepass", "resolver", "store"}


async def test_historical_builder_passes_cutoff_and_keeps_proven_store_alias() -> None:
    seen: list[int | None] = []
    resolver_calls = 0

    async def store(_ns: str, _text: str, known_seq: int | None) -> Sequence[KnownEntity]:
        seen.append(known_seq)
        return [
            KnownEntity(
                "Alex",
                ("Lexi",),
                source="entity_index",
                entity_id="ent:a:alex",
                version=12,
                known_seq=4,
                visibility=KnownVisibility(namespace="a"),
            )
        ]

    async def resolver(_ns: str, _text: str) -> Sequence[KnownEntity]:
        nonlocal resolver_calls
        resolver_calls += 1
        return [KnownEntity("Future Name", ("Future Alias",), source="resolver")]

    built = await PromptContextBuilder(store, resolver=resolver).build(
        "a", "[1] Alex called Lexi", [], known_seq=10
    )
    assert seen == [10]
    assert "Alex = Lexi" in built.known
    assert "Future Name" not in built.known and "Future Alias" not in built.known
    assert resolver_calls == 0


async def test_end_to_end_prompt_carries_prepass_names_and_resolver_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = WindowLLM()
    eng = await _engine(monkeypatch, llm, prepass="rules", resolver=True)
    try:
        reg = eng._entity_resolve_stage.registry
        reg.create(("a",), "e1", "Ilse Verhoeven")
        reg.add_alias(("a",), "e1", "Ilsa V")
        msgs = [
            {
                "role": "user",
                "content": "Mira: Bram booked a trip to Kelmark.",
                "created_at": T0.isoformat(),
            },
            {
                "role": "user",
                "content": "Mira: Ilse Verhoeven joined him.",
                "created_at": (T0 + timedelta(minutes=1)).isoformat(),
            },
        ]
        await _send(eng, "a", "s1", msgs)
        await eng.flush_windows()
        users = [m[-1]["content"] for m, _ in llm.requests if "NEW:\n" in m[-1]["content"]]
        assert users
        known = users[0].split("KNOWN:", 1)[1]
        assert "Kelmark" in known and "Bram" in known  # pre-pass
        # The window is historical: live resolver entries have no canonical known-time
        # provenance, so they cannot add names that may have been resolved later.
        assert "Ilsa V" not in known
    finally:
        await eng.stop()


async def test_flags_off_leave_the_builder_hooks_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = await _engine(
        monkeypatch,
        WindowLLM(),
    )
    try:
        builder = eng._windows._ctx
        assert builder.prepass is None and builder.resolver is None
    finally:
        await eng.stop()
