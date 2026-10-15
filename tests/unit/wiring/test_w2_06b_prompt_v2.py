# ruff: noqa: E501
"""W2-06b: prompt v2 and schema-constrained decoding (``write.prompt_v2``). Model-free: a stub LLM that
records every request (messages and kwargs) and answers the one-call window prompt, the W2-06a
``structured`` prompt (flag off) and the repair re-ask. Hash embedder."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_w2_06a_envelope import ItemLLM, _engine
from test_window_write import _facts, _send, _turns

from memspine.config.schema import MemspineConfig
from memspine.derive.prompt_context import KnownEntity, PromptContextBuilder
from memspine.exceptions import LLMError
from memspine.models.providers.llm.capabilities import schema_options
from memspine.observability import tap
from memspine.prompts.layout_aliases import render_known
from memspine.prompts.structured import default_partial, window_messages, window_schema
from memspine.workers.windows import split_batch_episodes

LINE = re.compile(r"^\[(\d+)\] \[[\d-]+\] (.+)$", re.M)


class WindowLLM(ItemLLM):
    """ItemLLM plus the one-call window prompt; ``requests`` holds every call's (messages, options)."""

    supports_json_schema: bool | None = None

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[tuple[list[dict[str, str]], dict[str, Any]]] = []
        self.unified = 0
        self.reject_schema = False

    @property
    def calls(self) -> int:
        return len(self.requests)

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        self.requests.append(([dict(m) for m in messages], dict(options)))
        user = messages[-1]["content"]
        if "NEW:\n" not in user:
            return await super().chat(messages, **options)
        if self.reject_schema and "response_format" in options:
            raise LLMError("backend does not support response_format")
        self.unified += 1
        sections = re.search(r"^Sections: (.+)\.$", user, re.M).group(1).split(", ")  # type: ignore[union-attr]
        new = user.split("NEW:\n", 1)[1].split("\n\nCONTEXT:", 1)[0]
        lines = [(int(n), text) for n, text in LINE.findall(new)]
        out: dict[str, Any] = {}
        if "facts" in sections:
            items: list[dict[str, Any]] = []
            for pos, (n, text) in enumerate(lines):
                who, said = text.split(": ", 1)
                item: dict[str, Any] | None = {
                    "s": who, "r": "said", "o": None, "text": f"{who} said {said}", "kind": "event",
                    "mode": "asserted", "neg": False, "when": None, "until": None, "lines": [n],
                    "span": said, "speaker": who,
                }
                if pos in self.mutations:
                    item = self.mutations[pos](item)
                if item is not None:
                    items.append(item)
            out["facts"] = items
        if "edges" in sections:
            out["edges"] = [
                {
                    "src_entity": text.split(":")[0], "rel": "mentioned", "dst_entity": f"item{n}",
                    "fact": f"{text.split(':')[0]} mentioned item{n}", "turns": [n],
                }
                for n, text in lines
            ]
        if "episode" in sections:
            blocks = re.split(r"^=== window \d+ of \d+ ===$", new, flags=re.M)
            blocks = [b for b in blocks if b.strip()]
            eps = []
            for k, block in enumerate(blocks, 1):
                got = [(int(n), t) for n, t in LINE.findall(block)]
                eps.append(
                    {
                        "window": k, "title": "A chat", "summary": "People chat.",
                        "narrative": " ".join(t for _, t in got), "turns": [n for n, _ in got],
                    }
                )
            batched = "=== window" in new
            out["episode"] = eps if batched else eps[0]
        return json.dumps(out)


async def _run(
    monkeypatch: pytest.MonkeyPatch, llm: WindowLLM, n: int = 4, **write: Any
) -> tuple[Any, dict[str, Any]]:
    eng = await _engine(monkeypatch, llm, **write)
    await _send(eng, "a", "s1", _turns("trip", n))
    stats = await eng.flush_windows()
    return eng, stats


# --- config / flag off --------------------------------------------------------------------------
def test_defaults_are_off() -> None:
    w = MemspineConfig().write
    assert w.prompt_v2 is False and w.prompt_schema == "auto"
    assert (w.prompt_known_cap, w.prompt_context_turns) == (40, 3)


@pytest.mark.parametrize("mode", ["facts_graph", "all"])
async def test_flag_off_unchanged_two_calls_per_window(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    llm = WindowLLM()
    eng, stats = await _run(monkeypatch, llm, n=14, window_extract=mode)
    try:
        assert llm.unified == 0
        assert not any("response_format" in o for _, o in llm.requests)
        assert llm.calls == 2 * stats["windows"]  # the W2-06a state: items call + edges call
        print(f"flag off, {mode}: {llm.calls} model calls / {stats['windows']} windows")
        assert len(await _facts(eng)) == 14
    finally:
        await eng.stop()


async def test_prompt_v2_needs_envelope_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(Exception, match="envelope_v2"):
        await _engine(monkeypatch, WindowLLM(), envelope_v2=False, prompt_v2=True)


# --- one call per window / batch ------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["facts", "facts_graph", "all"])
async def test_one_call_per_window(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    llm = WindowLLM()
    eng, stats = await _run(monkeypatch, llm, n=14, window_extract=mode, prompt_v2=True)
    try:
        assert stats["windows"] >= 2
        assert stats["calls"] == stats["windows"] == llm.unified == llm.calls
        assert len(await _facts(eng)) == 14
        print(f"\nprompt_v2, {mode}: {stats['calls']} calls / {stats['windows']} windows")
    finally:
        await eng.stop()


async def test_one_call_per_batch_with_episodes(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = WindowLLM()
    eng = await _engine(
        monkeypatch, llm, window_extract="all", prompt_v2=True, segmentation_v2=True,
        segment_max_turns=4, batch_max_windows=3,
    )
    try:
        await _send(eng, "a", "s1", _turns("trip", 12))
        stats = await eng.flush_windows()
        windows = stats["windows"]
        assert windows >= 2 and stats["batches"] == 1
        assert llm.unified == 1 == llm.calls  # facts + edges + every window's episode in ONE call
        assert stats["episodes_deposited"] == windows
        print(f"prompt_v2 batch, all: {llm.calls} model call / {windows} windows")
        assert len(await _facts(eng)) == 12
        user = llm.requests[0][0][-1]["content"]
        assert user.count("=== window") == windows
        assert "episode: a LIST" in llm.requests[0][0][0]["content"]
    finally:
        await eng.stop()


def test_split_batch_episodes_renumbers_and_fails_one_window_alone() -> None:
    spans = [(0, 2), (2, 3)]
    out = split_batch_episodes(
        [{"window": 2, "narrative": "b", "turns": [3, 5]}, {"window": 1, "narrative": "a", "turns": [1]}],
        spans,
    )
    assert out[0] == {"narrative": "a", "turns": [1]} and out[1] == {"narrative": "b", "turns": [1, 3]}
    only_first = split_batch_episodes([{"window": 1, "narrative": "a", "turns": [2]}], spans)
    assert only_first[0]["turns"] == [2] and only_first[1] is not only_first[0]
    positional = split_batch_episodes([{"narrative": "a"}, {"narrative": "b"}], spans)
    assert [p["narrative"] for p in positional] == ["a", "b"]


# --- sections parse back ----------------------------------------------------------------------------
async def test_sections_parse_into_facts_edges_and_episode(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = WindowLLM()
    eng, stats = await _run(monkeypatch, llm, n=4, window_extract="all", prompt_v2=True)
    try:
        facts = await _facts(eng)
        assert len(facts) == 4 and all("envelope_v2" in f.tags for f in facts)
        assert stats["episodes_deposited"] == stats["windows"] >= 1
        assert stats["edges_returned"] == 4 if "edges_returned" in stats else True
        edges = eng._windows.take_edges("a")
        assert sum(len(b.edges) for b in edges) == 4
        assert stats["sections_dropped"] == 0
    finally:
        await eng.stop()


async def test_malformed_items_dropped_no_worse_than_w2_06a(monkeypatch: pytest.MonkeyPatch) -> None:
    def break_items(llm: WindowLLM) -> None:
        llm.mutations[0] = lambda item: {**item, "lines": []}
        llm.mutations[1] = lambda item: {**item, "trust": 0.99}
        llm.mutations[2] = lambda item: {**item, "lines": [99]}
        llm.repair_mode = "fix"

    old = WindowLLM()
    break_items(old)
    eng, base = await _run(monkeypatch, old, window_extract="facts")
    await eng.stop()
    new = WindowLLM()
    break_items(new)
    eng2, got = await _run(monkeypatch, new, window_extract="facts", prompt_v2=True)
    try:
        assert got["items_dead"] <= base["items_dead"]
        assert got["items_dropped"] <= base["items_dropped"]
        assert got["items_valid"] + got["items_repaired"] >= base["items_valid"] + base["items_repaired"]
        assert len(new.repairs) <= len(old.repairs) <= 1  # at most one repair re-ask per window
    finally:
        await eng2.stop()


async def test_unparseable_reply_fails_closed_and_is_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    class Bad(WindowLLM):
        async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
            if "NEW:\n" in messages[-1]["content"] or "not a valid JSON object" in messages[-1]["content"]:
                self.requests.append((messages, options))
                return "I cannot do that"
            return await super().chat(messages, **options)

    llm = Bad()
    eng, stats = await _run(monkeypatch, llm, n=3, window_extract="all", prompt_v2=True)
    try:
        assert stats["parse_failures"] >= 1 and len(await _facts(eng)) == 0
    finally:
        await eng.stop()


# --- schema-constrained decoding -----------------------------------------------------------------
def _formats(llm: WindowLLM) -> list[Any]:
    return [o.get("response_format") for m, o in llm.requests if "NEW:\n" in m[-1]["content"]]


async def test_schema_passed_to_a_supporting_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = WindowLLM()
    llm.supports_json_schema = True
    eng, _ = await _run(monkeypatch, llm, window_extract="all", prompt_v2=True)
    try:
        (fmt, *_rest) = _formats(llm)
        assert fmt["type"] == "json_schema"
        schema = fmt["json_schema"]["schema"]
        assert set(schema["properties"]) == {"facts", "edges", "episode"}
        assert schema["properties"]["facts"]["items"]["required"][:2] == ["text", "kind"]
    finally:
        await eng.stop()


@pytest.mark.parametrize("declared", [None, False])
async def test_schema_omitted_for_other_providers(
    monkeypatch: pytest.MonkeyPatch, declared: bool | None
) -> None:
    llm = WindowLLM()
    llm.supports_json_schema = declared
    eng, _ = await _run(monkeypatch, llm, window_extract="all", prompt_v2=True)
    try:
        assert _formats(llm) and all(f is None for f in _formats(llm))
    finally:
        await eng.stop()


async def test_schema_off_by_config(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = WindowLLM()
    llm.supports_json_schema = True
    eng, _ = await _run(monkeypatch, llm, window_extract="all", prompt_v2=True, prompt_schema="off")
    try:
        assert all(f is None for f in _formats(llm))
    finally:
        await eng.stop()


async def test_a_rejected_schema_falls_back_once_and_is_remembered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = WindowLLM()
    llm.supports_json_schema = True
    llm.reject_schema = True
    eng, stats = await _run(monkeypatch, llm, n=14, window_extract="all", prompt_v2=True)
    try:
        sent = _formats(llm)
        assert sent[0] is not None and all(f is None for f in sent[1:])
        assert llm.unified == stats["windows"] and len(await _facts(eng)) == 14
    finally:
        await eng.stop()


def test_provider_capability_detection() -> None:
    class P:
        def __init__(self, pid: str, **kw: Any) -> None:
            self.provider_id = pid
            self.__dict__.update(kw)

    s = window_schema(("facts",))
    assert schema_options(P("litellm:ollama_chat/qwen3"), s, "x")[0] == "json_schema"
    assert schema_options(P("litellm:openai/gpt"), s, "x")[0] == "json_schema"
    assert schema_options(P("litellm:bedrock/claude"), s, "x") == ("none", {})
    mode, opts = schema_options(P("llama_cpp:/m.gguf"), s, "x")
    assert mode == "grammar" and opts["response_format"]["schema"] == s
    assert schema_options(P("litellm:ollama/x", supports_json_schema=False), s, "x") == ("none", {})
    assert schema_options(P("fake:x"), s, "x") == ("none", {})
    assert schema_options(P("fake:x", supports_json_schema=True), s, "x")[0] == "json_schema"


# --- KNOWN, CONTEXT, layout ------------------------------------------------------------------------------
async def test_known_is_capped_and_deduplicated() -> None:
    async def store(_ns: str, _text: str) -> Sequence[KnownEntity]:
        return [KnownEntity(f"Person{i}", (f"P{i}",)) for i in range(10)] + [
            KnownEntity("ilse", ("Ilse B",)),
        ]

    async def prepass(_ns: str, _text: str) -> Sequence[KnownEntity]:
        return [KnownEntity("Ilse", ("Ilsa", "Person3")), KnownEntity("Ilse", ("Ilse B",))]

    builder = PromptContextBuilder(store, cap=5)
    builder.prepass = prepass
    built = await builder.build("a", "Noor met Person7 and Ilsa", ["[1] earlier"])
    lines = built.known.splitlines()
    assert built.known_count == len(lines) == 5
    names = [ln.split(" = ")[0] for ln in lines]
    assert len(names) == len({n.casefold() for n in names})  # deduplicated
    assert names[0] == "Ilse" and "Person7" in names  # the entities named in NEW come first
    assert "Person3" not in lines[0]  # an alias that is another entity's name is not an alias
    assert lines[0].count("Ilse B") == 1  # merged alias listed once
    assert built.by_source == {"prepass": 1, "store": 10}
    assert built.context == "[1] earlier"


async def test_known_hooks_default_to_none_and_a_failing_source_is_skipped() -> None:
    async def boom(_ns: str, _text: str) -> Sequence[KnownEntity]:
        raise RuntimeError("down")

    b = PromptContextBuilder(boom)
    assert b.prepass is None and b.resolver is None
    assert (await b.build("a", "x", [])).known == ""
    zero = await PromptContextBuilder(None, cap=0).build("a", "x", ["c"])
    assert zero.known == "" and zero.known_count == 0


def test_aliases_render_compactly() -> None:
    text = render_known([("Ilse", ["Ilsa", "I.", "Ilse B", "extra"]), ("Bram", [])], max_aliases=2)
    assert text == "Ilse = Ilsa | I.\nBram"
    assert render_known([("A" * 50, []), ("B", [])], max_chars=40) == ""


def test_layout_new_context_known_and_stable_system() -> None:
    a = window_messages(("facts", "edges"), "[1] [2023-06-01] Mira: hi", "Mira", 1, known="Ilse = Ilsa")
    b = window_messages(("facts", "edges"), "[1] [2023-06-02] Bram: yo", "Bram", 1, context="[x] y")
    assert a[0]["content"] == b[0]["content"]  # the system part is constant per section set
    assert a[0]["content"] != window_messages(("facts",), "", "", 1)[0]["content"]
    user = a[-1]["content"]
    assert user.index("NEW:") < user.index("CONTEXT:") < user.index("KNOWN:")
    assert "Ilse = Ilsa" in user and "CONTEXT:\n(none)" in user
    assert "Example" not in a[0]["content"] and "for example" not in a[0]["content"].lower()


def test_partials_still_apply() -> None:
    sysmsg = window_messages(("facts",), "[1] [2023-06-01] Mira: hi")[0]["content"]
    assert default_partial("coarse_date") in sysmsg and default_partial("no_injection") in sysmsg
    en = window_messages(("facts",), "x", language="en")[-1]["content"]
    fr = window_messages(("facts",), "x", language="fr")[-1]["content"]
    assert default_partial("same_language") not in en
    assert fr.startswith(default_partial("same_language"))
    off = window_messages(("facts",), "x", language="fr", partial=lambda n: "")  # layout partial switched off
    assert "language of the source" not in off[-1]["content"]


async def test_context_is_the_previous_windows_tail_and_the_tap_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    real = tap.event
    monkeypatch.setattr(tap, "event", lambda stage, /, **f: (events.append((stage, f)), real(stage, **f))[1])
    llm = WindowLLM()
    llm.supports_json_schema = True
    eng, stats = await _run(monkeypatch, llm, n=14, window_extract="facts_graph", prompt_v2=True)
    try:
        users = [m[-1]["content"] for m, _ in llm.requests if "NEW:\n" in m[-1]["content"]]
        assert "CONTEXT:\n(none)" in users[0]
        ctx = users[1].split("CONTEXT:\n", 1)[1].split("\n\nKNOWN:", 1)[0]
        assert 1 <= len(ctx.splitlines()) <= 3 and ctx.splitlines()[-1] in users[0]
        taps = [f for s, f in events if s == "write.prompt.v2" and "known_count" in f]
        assert len(taps) == stats["windows"]
        assert taps[0]["sections"] == ["facts", "edges"] and taps[0]["schema_mode"] == "json_schema"
        assert isinstance(taps[0]["known_count"], int)
    finally:
        await eng.stop()
