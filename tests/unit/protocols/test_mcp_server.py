"""I66: the stdio MCP server, driven over real pipes with an in-process engine."""

from __future__ import annotations

import asyncio
import io
import json
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest

from memspine import Engine
from memspine.protocols.mcp import McpServer, serve_stdio
from memspine.protocols.tools import AgentMemoryTools, ToolContext


@pytest.fixture
async def server() -> AsyncIterator[McpServer]:
    engine = await Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        read={"hybrid": False},
        memories={"semantic": {"enabled": True}, "episodic": {"enabled": True}},
    ).start()
    try:
        yield McpServer(AgentMemoryTools(engine, ToolContext(namespace="mcp-t")))
    finally:
        await engine.stop()


def _req(i: int, method: str, **params: Any) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}) + "\n"


async def test_stdio_roundtrip_initialize_list_write_search(server: McpServer) -> None:
    note = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
    script = (
        _req(1, "initialize", protocolVersion="2025-03-26", capabilities={})
        + note
        + _req(2, "tools/list")
        + _req(3, "tools/call", name="memory_write", arguments={"content": "Likes jazz music"})
        + _req(4, "tools/call", name="memory_search", arguments={"query": "jazz"})
        + _req(5, "ping")
    )
    out = io.StringIO()
    await serve_stdio(server, io.StringIO(script), out)
    frames = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [f["id"] for f in frames] == [1, 2, 3, 4, 5]  # the notification got no reply
    init, tools, wrote, found, ping = frames
    assert init["result"]["protocolVersion"] == "2025-03-26"
    assert "tools" in init["result"]["capabilities"]
    listed = {t["name"]: t for t in tools["result"]["tools"]}
    assert set(listed) == {
        "memory_search",
        "memory_write",
        "memory_update",
        "memory_forget",
        "memory_confirm",
    }
    assert listed["memory_search"]["annotations"]["readOnlyHint"] is True
    assert listed["memory_forget"]["annotations"]["destructiveHint"] is True
    assert listed["memory_write"]["inputSchema"]["required"] == ["content"]
    body = json.loads(wrote["result"]["content"][0]["text"])
    assert body["verdict"] == "stored" and wrote["result"]["isError"] is False
    rows = json.loads(found["result"]["content"][0]["text"])["results"]
    assert any(r["id"] == body["id"] for r in rows)
    assert ping["result"] == {}


async def test_errors_and_namespace_injection(server: McpServer) -> None:
    bad_json = await server.handle_line("{not json")
    assert json.loads(bad_json or "")["error"]["code"] == -32700
    unknown = await server.handle({"jsonrpc": "2.0", "id": 1, "method": "nope"})
    assert unknown and unknown["error"]["code"] == -32601
    inj = await server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "memory_write", "arguments": {"content": "a b", "namespace": "x"}},
        }
    )
    assert inj
    assert inj["result"]["isError"] is True
    assert json.loads(inj["result"]["content"][0]["text"])["error"] == "unknown_argument"
    notif = await server.handle({"jsonrpc": "2.0", "method": "tools/call", "params": {}})
    assert notif is None


async def test_real_subprocess_stdout_is_protocol_only(tmp_path: Any) -> None:
    """Spawn `memspine mcp` as a client would; stdout must hold only JSON frames."""
    import sys

    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "embedding:\n  provider: hash\nread:\n  hybrid: false\n"
        "memories:\n  semantic:\n    enabled: true\n",
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "memspine.cli",
        "mcp",
        "-n",
        "sub-t",
        "-t",
        "core",
        "-c",
        str(cfg),
        "--db",
        str(tmp_path / "m.db"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    script = _req(1, "initialize", protocolVersion="2024-11-05") + _req(2, "tools/list")
    stdout, _ = await asyncio.wait_for(proc.communicate(script.encode()), timeout=120)
    frames = [json.loads(line) for line in stdout.decode().splitlines() if line.strip()]
    assert [f["id"] for f in frames] == [1, 2]
    assert len(frames[1]["result"]["tools"]) == 5
