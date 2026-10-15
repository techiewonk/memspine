"""TS-LOCAL: the Tuning Studio local HTTP server (stdlib), in-thread, offline demo data."""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

pytest.importorskip("memspine")

from memspine_evals.studio.api import StudioAPI
from memspine_evals.studio.http_server import make_server
from memspine_evals.studio.server import tool_list

TOKEN = "test-token-123"


@pytest.fixture(scope="module")
def srv(tmp_path_factory: pytest.TempPathFactory):
    tmp: Path = tmp_path_factory.mktemp("studio_http")
    api = StudioAPI("demo", tmp / "out", arms_dir=tmp / "arms", logs_dir=tmp / "logs")
    s = make_server(api, "127.0.0.1", 0, TOKEN)
    t = threading.Thread(target=s.serve_forever, daemon=True)
    t.start()
    yield s
    s.shutdown()
    s.server_close()
    api.close()


def req(
    srv,
    method: str,
    path: str,
    body: dict | None = None,
    token: str | None = TOKEN,
    host: str | None = None,
    origin: str | None = None,
):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=120)
    headers = {}
    if token is not None:
        headers["X-Studio-Token"] = token
    if host:
        headers["Host"] = host
    if origin:
        headers["Origin"] = origin
    data = None
    if body is not None:
        data = json.dumps(body)
        headers["Content-Type"] = "application/json"
    c.request(method, path, body=data, headers=headers)
    r = c.getresponse()
    raw = r.read()
    c.close()
    return r.status, raw


def call(srv, tool: str, **args):
    status, raw = req(srv, "POST", "/api/call", {"tool": tool, "args": args})
    return status, json.loads(raw)


def test_page_served_with_mode_detection(srv) -> None:
    status, raw = req(srv, "GET", "/", token=None)
    html = raw.decode("utf-8")
    assert status == 200 and "<html" in html.lower()
    assert (
        "Live (local)" in html
        and "/api/call" in html
        and "replaceState" in html
        and "X-Studio-Token" in html
    )
    assert "claude.use" in html or "cl.use" in html  # artifact mode still present


def test_auth_and_host_guards(srv) -> None:
    assert (
        req(srv, "POST", "/api/call", {"tool": "studio_status", "args": {}}, token=None)[0] == 401
    )
    assert (
        req(srv, "POST", "/api/call", {"tool": "studio_status", "args": {}}, token="wrong")[0]
        == 401
    )
    assert req(srv, "GET", "/api/tools", token=None)[0] == 401
    assert req(srv, "GET", "/", host="evil.example.com", token=None)[0] == 403
    assert (
        req(
            srv, "POST", "/api/call", {"tool": "studio_status", "args": {}}, host="evil.example.com"
        )[0]
        == 403
    )
    assert (
        req(
            srv,
            "POST",
            "/api/call",
            {"tool": "studio_status", "args": {}},
            origin="http://evil.example.com",
        )[0]
        == 403
    )
    assert (
        req(
            srv,
            "POST",
            "/api/call",
            {"tool": "studio_status", "args": {}},
            origin="http://localhost:8765",
        )[0]
        == 200
    )
    assert req(srv, "GET", "/nope", token=None)[0] == 404


def test_wildcard_and_remote_host_rules(tmp_path: Path) -> None:
    api = StudioAPI("demo", tmp_path / "o")
    with pytest.raises(SystemExit):
        make_server(api, "0.0.0.0", 0, "t")
    with pytest.raises(SystemExit):
        make_server(api, "100.64.0.1", 0, None)


def test_tools_match_mcp(srv) -> None:
    status, raw = req(srv, "GET", "/api/tools")
    got = json.loads(raw)
    assert status == 200 and got["ok"] and got["payload"] == json.loads(json.dumps(tool_list()))
    assert {t["name"]: t["annotations"]["readOnlyHint"] for t in got["payload"]}[
        "studio_ask"
    ] is False


def test_loop_over_http(srv) -> None:
    status, body = call(srv, "studio_open", data="demo", mode="offline")
    assert status == 200 and body["ok"] and body["payload"]["open"]
    status, body = call(
        srv,
        "studio_write",
        mode="say",
        messages=[{"speaker": "Bram", "text": "Last week I moved to Zwolle."}],
    )
    assert status == 200 and body["payload"]["turns"][0]["custom"] is True
    call(srv, "studio_write", mode="session", n=3)
    status, body = call(srv, "studio_ask", questions=["q1"], detail="brief")
    assert status == 200 and body["payload"]["results"][0]["qid"] == "q1"
    status, body = call(srv, "studio_set", key="read.assembly.relative_floor", value=0.2)
    assert status == 200 and body["payload"]["applied"] == "live_verified"
    status, body = call(srv, "studio_history", limit=50)
    assert status == 200
    rows = body["payload"]["result"] if isinstance(body["payload"], dict) else body["payload"]
    assert {r["tool"] for r in rows} >= {"studio_open", "studio_write", "studio_ask", "studio_set"}


def test_errors_are_structured(srv) -> None:
    status, body = call(srv, "studio_nothing")
    assert status == 400 and body["ok"] is False and body["message"]
    status, raw = req(srv, "POST", "/api/call", {"nope": 1})
    assert status == 400 and json.loads(raw)["code"] == "bad_request"
