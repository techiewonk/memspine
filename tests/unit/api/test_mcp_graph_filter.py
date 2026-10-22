"""RM-101: the MCP ``memory_search`` tool takes graph-filter presets and an advanced filter.

Driven through :class:`~memspine.api.mcp.McpServer` (JSON-RPC ``tools/call``). Presets:
``asserted_only``, ``as_of`` (valid time), ``known_by`` (known time); ``exclude_sensitivity``
is offered only once ``QualifierFilter`` carries ``exclude_sensitivities`` (RM-86). The
advanced ``graph_filter`` object is laid over the presets (advanced wins on shared fields).
"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from memspine import Engine
from memspine.api.mcp import McpServer
from memspine.api.tools import EXCLUDE_SENSITIVITY_SUPPORTED, AgentMemoryTools, ToolContext
from memspine.services.graph.qualifiers import AssertionModality, QualifierFilter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "wiring"))
from test_rm93_graph_filter import NS, QUERY, _build, _Spy
from test_rm93_graph_filter import _engine as _rm93_engine


@pytest.fixture
async def engine() -> AsyncIterator[Engine]:
    eng = await Engine(
        template="core",
        dotenv_path=None,
        storage={"provider": "sqlite", "settings": {"path": ":memory:", "busy_timeout_s": 5.0}},
        embedding={"provider": "hash", "settings": {}},
        read={"hybrid": False},
        memories={"semantic": {"enabled": True}, "episodic": {"enabled": True}},
    ).start()
    try:
        yield eng
    finally:
        await eng.stop()


class _Capture:
    """Stands in for ``Engine.search`` on the instance and records its ``graph_filter``."""

    def __init__(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        self.filters: list[QualifierFilter | None] = []
        real = engine.search

        async def search(query: str, **kwargs: Any) -> Any:
            self.filters.append(kwargs.get("graph_filter"))
            return await real(query, **kwargs)

        monkeypatch.setattr(engine, "search", search)


async def _call(server: McpServer, arguments: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    reply = await server.handle(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "memory_search", "arguments": arguments},
        }
    )
    assert reply is not None and "result" in reply, reply
    body = json.loads(reply["result"]["content"][0]["text"])
    return bool(reply["result"]["isError"]), body


def _server(engine: Engine) -> McpServer:
    return McpServer(AgentMemoryTools(engine, ToolContext(namespace="mcp-g")))


async def test_tools_list_offers_presets_and_advanced_filter(engine: Engine) -> None:
    reply = await _server(engine).handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert reply is not None
    search = next(t for t in reply["result"]["tools"] if t["name"] == "memory_search")
    props = search["inputSchema"]["properties"]
    assert {"asserted_only", "as_of", "known_by", "graph_filter"} <= set(props)
    assert props["asserted_only"]["type"] == "boolean"
    assert props["graph_filter"]["type"] == "object"
    assert ("exclude_sensitivity" in props) is EXCLUDE_SENSITIVITY_SUPPORTED
    assert search["inputSchema"]["required"] == ["query"]


async def test_presets_expand_to_a_qualifier_filter(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cap = _Capture(engine, monkeypatch)
    server = _server(engine)
    err, body = await _call(
        server,
        {
            "query": "jazz",
            "asserted_only": True,
            "as_of": "2026-03-01T00:00:00Z",
            "known_by": "2026-04-01T12:00:00+02:00",
        },
    )
    assert not err and body["ok"] is True
    flt = cap.filters[-1]
    assert flt == QualifierFilter(
        assertion_modalities=frozenset({AssertionModality.ASSERTED}),
        valid_at=datetime(2026, 3, 1, tzinfo=UTC),
        known_as_of=datetime(2026, 4, 1, 10, 0, tzinfo=UTC),
    )
    assert body["graph_filter"]["assertion_modalities"] == ["asserted"]
    # No filter arguments: the search is unchanged (no graph_filter passed at all).
    err, body = await _call(server, {"query": "jazz", "asserted_only": False})
    assert not err and cap.filters[-1] is None and "graph_filter" not in body


async def test_advanced_filter_combines_and_wins_on_conflict(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cap = _Capture(engine, monkeypatch)
    err, body = await _call(
        _server(engine),
        {
            "query": "jazz",
            "asserted_only": True,
            "known_by": "2026-04-01T00:00:00Z",
            "graph_filter": {
                "assertion_modalities": ["asserted", "reported"],  # overrides the preset
                "topical_domains": ["work"],
                "valid_window": ["2026-01-01T00:00:00Z", "2026-06-01T00:00:00Z"],
                "min_extraction_confidence": 0.7,
            },
        },
    )
    assert not err and body["ok"] is True
    flt = cap.filters[-1]
    assert flt is not None
    assert flt.assertion_modalities == frozenset(
        {AssertionModality.ASSERTED, AssertionModality.REPORTED}
    )
    assert flt.known_as_of == datetime(2026, 4, 1, tzinfo=UTC)  # the preset survives
    assert flt.topical_domains == frozenset({"work"})
    assert flt.valid_window == (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC))
    assert flt.min_extraction_confidence == 0.7


@pytest.mark.parametrize(
    ("arguments", "code", "fragment"),
    [
        ({"as_of": "2026-03-01T00:00:00"}, "bad_graph_filter", "timezone offset"),
        ({"known_by": "last tuesday"}, "bad_graph_filter", "known_by must be an ISO 8601"),
        ({"asserted_only": "yes"}, "bad_argument", "asserted_only must be true or false"),
        ({"graph_filter": "domain=work"}, "bad_argument", "graph_filter must be an object"),
        ({"graph_filter": {"topical_domain": ["work"]}}, "bad_graph_filter", "topical_domain"),
        (
            {"graph_filter": {"assertion_modalities": ["certain"]}},
            "bad_graph_filter",
            "assertion_modalities",
        ),
        (
            {"graph_filter": {"valid_at": "2026-03-01T00:00:00"}},
            "bad_graph_filter",
            "timezone-aware",
        ),
        (
            {"graph_filter": {"min_extraction_confidence": 2}},
            "bad_graph_filter",
            "min_extraction_confidence",
        ),
        ({"graph_filter": {"topical_domains": ["x" * 5000]}}, "bad_graph_filter", "exceeds"),
    ],
    ids=[
        "naive_as_of",
        "unparseable_known_by",
        "preset_not_bool",
        "advanced_not_object",
        "unknown_field",
        "bad_enum",
        "naive_advanced_time",
        "out_of_range",
        "too_large",
    ],
)
async def test_invalid_filters_are_clear_tool_errors(
    engine: Engine, arguments: dict[str, Any], code: str, fragment: str
) -> None:
    err, body = await _call(_server(engine), {"query": "jazz", **arguments})
    assert err is True
    assert body["ok"] is False and body["error"] == code
    assert fragment in body["message"]


async def test_exclude_sensitivity_preset_maps_to_the_rm86_deny_set(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert EXCLUDE_SENSITIVITY_SUPPORTED  # RM-86 merged: the preset is offered
    cap = _Capture(engine, monkeypatch)
    err, body = await _call(
        _server(engine),
        {
            "query": "jazz",
            "exclude_sensitivity": ["health", "location"],
            "graph_filter": {"unchecked_sensitivity": "pass"},
        },
    )
    assert not err and body["ok"] is True
    flt = cap.filters[-1]
    assert flt is not None
    assert flt.exclude_sensitivities == frozenset({"health", "location"})
    assert flt.unchecked_sensitivity == "pass"
    err, body = await _call(_server(engine), {"query": "jazz", "exclude_sensitivity": ["secret"]})
    assert err is True and body["error"] == "bad_graph_filter"


async def test_filter_reaches_the_graph_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end over the RM-93 world: the presets gate the compiled-edge graph leg."""
    eng = await _rm93_engine()
    try:
        await _build(eng)
        server = McpServer(AgentMemoryTools(eng, ToolContext(namespace=NS)))
        spy = _Spy(monkeypatch)
        err, body = await _call(
            server, {"query": QUERY, "known_by": "2000-01-01T00:00:00Z", "top_k": 4}
        )
        assert not err and body["ok"] is True and body["results"]  # non-graph legs still serve
        before = spy.of("read.graph_filter")
        err, _ = await _call(server, {"query": QUERY, "asserted_only": True, "top_k": 4})
        assert not err
        asserted = spy.of("read.graph_filter")[len(before) :]
    finally:
        await eng.stop()
    assert before and before[0]["admitted"] == 0
    assert asserted and 0 < asserted[0]["admitted"] < asserted[0]["edges"]
