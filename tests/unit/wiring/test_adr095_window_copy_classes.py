"""ADR-095 erasure gate (RM-135), the window and derivation copy classes.

Copied excerpts (fact evidence quotes), unresolved annotations (RM-42), open-window drafts,
the known-entity cache and the pre-pass mention cache (in-process), and a forget that lands
while a window call is in flight. The same rules as ``test_adr095_erasure_gate``: after the
erasure the log and every SQL table hold none of the erased turn's words or content hashes,
and the in-process caches hold none of its names. Model-free: fake window LLM and finder.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import orjson
import pytest
import zstandard
from sqlalchemy import MetaData, Table, inspect, select

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.derive.gliner_mentions import MentionSpan
from memspine.derive.prepass import Prepass
from memspine.ingest import windows as ingest_windows
from memspine.services.llm.base import LLMRouter

pytestmark = pytest.mark.erasure_gate

NS = "a"
LINE = re.compile(r"^\[(\d+)\] \[[\d-]+\] (.+)$", re.M)


class CiteAllLLM:
    """A window LLM that turns every transcript line into one fact citing it, verbatim span.
    ``block`` holds the call open (an in-flight window) until it is set."""

    def __init__(self) -> None:
        self.calls = 0
        self.block: asyncio.Event | None = None
        self.entered = asyncio.Event()

    @property
    def provider_id(self) -> str:
        return "fake:window"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        user = messages[-1]["content"]
        if re.search(r"sections: (.+)\.$", user.strip(), re.M | re.I) is None:
            return "[]"
        self.calls += 1
        self.entered.set()
        if self.block is not None:
            await self.block.wait()
        facts = []
        for n, text in LINE.findall(user):
            speaker, _, said = text.partition(": ")
            facts.append(
                {
                    "s": speaker,
                    "r": "event",
                    "o": None,
                    "text": f"{speaker} said {said}",
                    "kind": "event",
                    "mode": "asserted",
                    "neg": False,
                    "lines": [int(n)],
                    "span": said,
                }
            )
        return json.dumps({"facts": facts})


class Finder:
    """A fake GLiNER pre-pass finder: the gate's names are entities."""

    producer_id = "fake-gliner:1"

    async def aentities(self, text: str) -> tuple[MentionSpan, ...]:
        return tuple(
            MentionSpan(
                text=m.group(0), label="person", start=m.start(), end=m.end(), confidence=0.9
            )
            for m in re.finditer(r"\b(Zebulon|Quixotelle|Dana)\b", text)
        )


def _router(monkeypatch: pytest.MonkeyPatch, roles: dict[str, Any]) -> None:
    async def router(
        config: MemspineConfig, hooks: object = None, wiring: object = None
    ) -> LLMRouter:
        return LLMRouter(roles)

    monkeypatch.setattr("memspine.runtime.container.build_llm_router", router)


def _config(path: Path, *, llm: bool, associative: bool = False, **write: Any) -> dict[str, Any]:
    model: dict[str, Any] = {"embedding": {"provider": "hash", "settings": {}}}
    if llm:
        model["llm"] = {
            "window_extract": {
                "provider": "openai_compatible",
                "timeout_s": 60.0,
                "settings": {"api_base": "http://test.invalid/v1", "model": "fake/window"},
            }
        }
    memories: dict[str, Any] = {"episodic": {"enabled": True}, "semantic": {"enabled": True}}
    if associative:
        memories["associative"] = {"enabled": True, "policies": {"entity_nodes": True}}
    return {
        "template": "core",
        "dotenv_path": None,
        "storage": {
            "records": {"provider": "sqlite", "settings": {"path": str(path), "busy_timeout_s": 5}}
        },
        "model": model,
        "memories": memories,
        "write": {
            "window_extract": "facts",
            "window_async": False,
            "window_idle_s": 0.0,
            "segment_gap_s": 0,
            "batch_max_windows": 1,
            **write,
        },
        "workers": {"outbox": True},
    }


def _message(i: int, content: str, sender: str = "Mira") -> dict[str, Any]:
    return {
        "message_id": f"m-{i}",
        "role": "user",
        "sender": {"id": sender},
        "content": content,
        "created_at": f"2026-10-24T10:0{i}:00+00:00",
    }


def _text(value: Any) -> str:
    if isinstance(value, bytes | bytearray | memoryview):
        raw = bytes(value)
        try:
            raw = zstandard.ZstdDecompressor().decompress(raw)
        except zstandard.ZstdError:
            pass
        return raw.decode("utf-8", errors="replace")
    return str(value)


async def _sql_scan(eng: Engine) -> str:
    """The decoded event log plus every row of every table, lower-cased."""
    assert eng._storage is not None and eng._sql is not None
    parts = [orjson.dumps(e.payload).decode() for e in await eng._storage.read_events(0, 10**6)]
    async with eng._sql.engine.connect() as conn:
        names = await conn.run_sync(lambda sync: inspect(sync).get_table_names())
        meta = MetaData()
        tables = await conn.run_sync(
            lambda sync: {n: Table(n, meta, autoload_with=sync) for n in names}
        )
        for name, table in tables.items():
            for row in (await conn.execute(select(table))).all():
                parts.append(name + ": " + " | ".join(_text(v) for v in row))
    return "\n".join(parts).lower()


def _present(haystack: str, needles: list[str]) -> list[str]:
    return [n for n in needles if n and n.lower() in haystack]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ---------------------------------------------------------------- copied excerpts


@pytest.mark.parametrize(
    ("hard", "redact_hidden"),
    [
        # ADR-095 amendment 2026-11-02: a sole-source fact is redacted by default.
        pytest.param(True, False, id="hard-default"),
        pytest.param(True, True, id="hard-redact_hidden"),
        pytest.param(False, False, id="soft"),
    ],
)
async def test_copied_excerpts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, hard: bool, redact_hidden: bool
) -> None:
    """A window fact copies a verbatim excerpt (its evidence quote) of its source turn. Erasing
    the turn erases every copy: the fact row and its WRITE event keep no quote."""
    _router(monkeypatch, {"window_extract": CiteAllLLM()})
    eng = Engine(
        **_config(tmp_path / "m.db", llm=True, window_max_turns=2, segment_max_turns=2),
        privacy={"erasure_redact_hidden": redact_hidden},
    )
    await eng.start()
    try:
        turns = await eng.ingest_messages(
            [
                _message(1, "I adopted a parrot called Quixotelle."),
                _message(2, "Dana bakes bread on Sundays."),
            ],
            namespace=NS,
            session_id="s1",
            conversation_id="s1",
        )
        await eng.flush_windows(NS)
        assert eng._storage is not None
        facts = [
            r for r in await eng._storage.list_records(NS, "semantic") if "window_fact" in r.tags
        ]
        assert any("quixotelle" in r.content.lower() for r in facts), [r.content for r in facts]
        await eng.forget(turns[0].record_id, namespace=NS, hard=hard)
        served = await eng.search("Quixotelle parrot", namespace=NS)
        assert not any("quixotelle" in r.content.lower() for r, _ in served)
        if hard:
            assert _present(await _sql_scan(eng), ["quixotelle", "parrot"]) == []
            assert "dana" in await _sql_scan(eng)
    finally:
        await eng.stop()


# ---------------------------------------------------------------- unresolved annotations


@pytest.mark.parametrize("hard", [True, False])
async def test_unresolved_annotations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, hard: bool
) -> None:
    """RM-42: an unmapped fact candidate persists as an unresolved annotation holding the value
    read from its turn; forgetting the turn (soft or hard) removes it from every table."""
    _router(monkeypatch, {})
    eng = Engine(**_config(tmp_path / "m.db", llm=False, online_extractor=True, window_max_turns=2))
    await eng.start()
    try:
        turns = await eng.ingest_messages(
            [_message(1, "I work for Acme."), _message(2, "I grew up in Vrbnikovo.")],
            namespace=NS,
            session_id="s1",
            conversation_id="s1",
        )
        await eng.flush_windows(NS)
        [item] = ingest_windows.unresolved_annotations(eng, NS)
        assert "vrbnikovo" in str(item).lower()
        await eng.forget(turns[1].record_id, namespace=NS, hard=hard)
        assert ingest_windows.unresolved_annotations(eng, NS) == []
        assert eng._sql is not None
        haystack = await _sql_scan(eng)
        effects = [line for line in haystack.splitlines() if line.startswith("outbox_")]
        assert _present("\n".join(effects), ["vrbnikovo"]) == []
        if hard:
            assert _present(haystack, ["vrbnikovo", "grew up"]) == []
    finally:
        await eng.stop()


# ---------------------------------------------------------------- window drafts


def _draft_needles(turn: Any) -> list[str]:
    envelope = turn.source.message or {}
    return [turn.record_id, *map(str, (envelope.get("input_fingerprints") or {}).values())]


@pytest.mark.parametrize("restart", [False, True])
async def test_window_drafts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, restart: bool
) -> None:
    """An open window's draft (``outbox_draft``) holds each turn's id, content hash and vector.
    Hard-erasing a turn removes it from the draft, also when the forget is the first thing a
    restarted engine does in the namespace."""
    db = tmp_path / "m.db"
    _router(monkeypatch, {"window_extract": CiteAllLLM()})
    eng = Engine(**_config(db, llm=True, window_max_turns=6, segment_max_turns=6))
    await eng.start()
    try:
        turns = await eng.ingest_messages(
            [_message(i, f"Zebulon note number {i} about Quixotelle") for i in range(1, 4)],
            namespace=NS,
            session_id="s1",
            conversation_id="s1",
        )
        assert "outbox_draft" in await _sql_scan(eng)
        if restart:
            await eng.stop()
            eng = Engine(**_config(db, llm=True, window_max_turns=6, segment_max_turns=6))
            await eng.start()
        gone = turns[0]
        await eng.forget(gone.record_id, namespace=NS, hard=True)
        haystack = await _sql_scan(eng)
        drafts = "\n".join(
            line
            for line in haystack.splitlines()
            if line.startswith(("outbox_draft", "outbox_item"))
        )
        assert turns[1].record_id.lower() in drafts  # the scan sees the open window
        assert _present(drafts, _draft_needles(gone)) == []
        assert _present(haystack, ["number 1 about"]) == []
    finally:
        await eng.stop()


# ---------------------------------------------------------------- in-process name caches


def _known_cursors(eng: Engine) -> list[Any]:
    from memspine.memories.associative.known_entities import KnownEntityCursor

    found: list[Any] = []
    pipeline: Any = eng._windows
    roots = [
        getattr(getattr(pipeline, "_ctx", None), "store", None),
        eng.__dict__.get("_coverage_stage"),
    ]
    seen: set[int] = set()
    stack = [r for r in roots if r is not None]
    while stack:
        obj = stack.pop()
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        if isinstance(obj, KnownEntityCursor):
            found.append(obj)
            continue
        for cell in getattr(obj, "__closure__", None) or ():
            try:
                stack.append(cell.cell_contents)
            except ValueError:
                continue
    return found


def _cursor_names(cursors: list[Any]) -> str:
    names = [
        text
        for cursor in cursors
        for state in cursor._state.values()
        for entry in state.entries.values()
        for text, _ in entry.names.values()
    ]
    return " ".join(names).lower()


async def _known_engine(monkeypatch: pytest.MonkeyPatch, path: Path) -> Engine:
    _router(monkeypatch, {"window_extract": CiteAllLLM()})
    eng = Engine(
        **_config(path, llm=True, associative=True, window_max_turns=2, segment_max_turns=2)
    )
    await eng.start()
    return eng


async def _feed_known(eng: Engine) -> Any:
    gone = await eng.write("Zebulon Quartz lives in Vrbnik", namespace=NS, entity="Zebulon Quartz")
    await eng.write("Dana bakes bread", namespace=NS, entity="Dana")
    await eng.ingest_messages(
        [_message(1, "Dana met Zebulon Quartz."), _message(2, "Dana waved.")],
        namespace=NS,
        session_id="s1",
        conversation_id="s1",
    )
    await eng.flush_windows(NS)
    return gone


@pytest.mark.parametrize("scope", ["record", "namespace"])
async def test_known_entity_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scope: str
) -> None:
    """The window prompt's KNOWN cursor keeps the entity names it read in process memory. After
    the record naming an entity is hard-erased (or its namespace is erased), no cursor holds
    the name."""
    eng = await _known_engine(monkeypatch, tmp_path / "m.db")
    try:
        gone = await _feed_known(eng)
        cursors = _known_cursors(eng)
        assert cursors, "no KNOWN cursor reachable from the window pipeline"
        assert "zebulon quartz" in _cursor_names(cursors)
        if scope == "record":
            await eng.forget(gone.record_id, namespace=NS, hard=True)
        else:
            await eng.erase_namespace(NS)
        assert "zebulon" not in _cursor_names(cursors)
    finally:
        await eng.stop()


async def test_prepass_mention_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The GLiNER pre-pass caches its hints (mention text) by window text. Hard-erasing the turn
    drops every cached hint holding its mentions."""
    _router(monkeypatch, {"window_extract": CiteAllLLM()})
    eng = Engine(
        **_config(
            tmp_path / "m.db", llm=True, window_max_turns=2, segment_max_turns=2, prepass="rules"
        )
    )
    eng._window_prepass = Prepass("gliner", gliner=Finder())
    await eng.start()
    try:
        turns = await eng.ingest_messages(
            [_message(1, "I visited Quixotelle with Zebulon."), _message(2, "Dana waved.")],
            namespace=NS,
            session_id="s1",
            conversation_id="s1",
        )
        await eng.flush_windows(NS)
        pre: Any = eng._window_prepass
        assert pre is not None and "quixotelle" in repr(pre._cache).lower()
        await eng.forget(turns[0].record_id, namespace=NS, hard=True)
        assert _present(repr(pre._cache).lower(), ["quixotelle", "zebulon"]) == []
    finally:
        await eng.stop()


# ---------------------------------------------------------------- in-flight background work


async def test_forget_during_in_flight_window_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A hard forget that lands while the window call is in flight: the effect commit is fenced
    (the erasure generation moved), so no fact, annotation or effect of the erased turn is
    written, and no copy of it remains after the call returns."""
    llm = CiteAllLLM()
    llm.block = asyncio.Event()
    _router(monkeypatch, {"window_extract": llm})
    eng = Engine(**_config(tmp_path / "m.db", llm=True, window_max_turns=2, segment_max_turns=2))
    await eng.start()
    try:
        turns = await eng.ingest_messages(
            [_message(1, "Zebulon owns the Quixotelle boat."), _message(2, "Dana bakes bread.")],
            namespace=NS,
            session_id="s1",
            conversation_id="s1",
        )
        flushing = asyncio.create_task(eng.flush_windows(NS))
        await asyncio.wait_for(llm.entered.wait(), 10)
        await eng.forget(turns[0].record_id, namespace=NS, hard=True)
        llm.block.set()
        await asyncio.wait_for(flushing, 30)
        await eng.flush_windows(NS)
        assert eng._storage is not None
        live = [
            r
            for r in await eng._storage.list_records(NS)
            if r.status.value != "deleted" and turns[0].record_id in map(str, r.source.parents)
        ]
        assert live == []
        assert _present(await _sql_scan(eng), ["zebulon", "quixotelle"]) == []
    finally:
        llm.block.set()
        await eng.stop()
