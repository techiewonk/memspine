"""F-13b: REST and the agent tools call the scoped ``Memory`` handle (G-16 D, G-17 C, ER-29).

The namespace comes from the checked header, the viewer from the authenticated principal; a
payload cannot override either. A multi-party scope without a viewer is rejected like the handle.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any

import pytest

pytest.importorskip("fastapi")

import httpx

from memspine import Engine
from memspine.api.rest import create_app
from memspine.api.tools import AgentMemoryTools, ToolContext
from memspine.config.schema import RestConfig
from memspine.observability import tap

ALICE_KEY = "alice-test-key-0001"


def _rest() -> RestConfig:
    return RestConfig.model_validate(
        {
            "auth": {
                "mode": "api_key",
                "api_keys": [
                    {
                        "key_env": "MS_TEST_ALICE_KEY",
                        "principal": "alice",
                        "namespaces": ["alice*"],
                    }
                ],
            }
        }
    )


@pytest.fixture
async def engine() -> AsyncIterator[Engine]:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"provider": "sqlite", "settings": {"path": ":memory:", "busy_timeout_s": 5.0}},
        embedding={"provider": "hash", "settings": {}},
        read={"hybrid": False},
        memories={"semantic": {"enabled": True}},
    )
    await eng.start()
    yield eng
    await eng.stop()


def _client(app: Any) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://memspine")


def _auth(namespace: str = "alice", **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {ALICE_KEY}", "X-Memspine-Namespace": namespace, **extra}


@pytest.fixture
def spy(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    real_search = engine.search

    async def search(query: str, **kwargs: Any) -> Any:
        calls.append(("search", kwargs))
        return await real_search(query, **kwargs)

    monkeypatch.setattr(engine, "search", search)
    return calls


async def test_viewer_is_the_authenticated_principal(
    engine: Engine, spy: list[tuple[str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MS_TEST_ALICE_KEY", ALICE_KEY)
    async with _client(create_app(engine, _rest())) as http:
        ok = await http.post("/search", json={"query": "tea", "purpose": "p"}, headers=_auth())
        assert ok.status_code == 200
    (_, kwargs) = spy[0]
    assert kwargs["namespace"] == "alice"
    assert kwargs["viewer"] == "alice"
    assert kwargs["purpose"] == "p"


async def test_payload_cannot_override_namespace_or_viewer(
    engine: Engine, spy: list[tuple[str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MS_TEST_ALICE_KEY", ALICE_KEY)
    async with _client(create_app(engine, _rest())) as http:
        for route, body in (
            ("/search", {"query": "tea", "viewer": "bob"}),
            ("/search", {"query": "tea", "namespace": "bob"}),
            ("/assemble", {"query": "tea", "viewer": "bob"}),
            ("/write", {"content": "x", "namespace": "bob"}),
            ("/write", {"content": "x", "viewer": "bob"}),
        ):
            resp = await http.post(route, json=body, headers=_auth())
            assert resp.status_code == 422, (route, body)
        # the header is the only namespace carrier, and it is checked against the principal
        foreign = await http.post("/search", json={"query": "t"}, headers=_auth("bob"))
        assert foreign.status_code == 403
    assert spy == []  # nothing reached the engine


async def test_multi_party_without_viewer_is_rejected(
    engine: Engine, spy: list[tuple[str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    flag = {"X-Memspine-Multi-Party": "true"}
    async with _client(create_app(engine)) as http:  # auth off: no principal, no viewer
        bad = await http.post(
            "/search", json={"query": "tea"}, headers={"X-Memspine-Namespace": "t", **flag}
        )
        assert bad.status_code == 400
        assert "viewer" in bad.json()["detail"]
    assert spy == []
    monkeypatch.setenv("MS_TEST_ALICE_KEY", ALICE_KEY)
    async with _client(create_app(engine, _rest())) as http:  # auth on: viewer = principal
        ok = await http.post("/search", json={"query": "tea"}, headers=_auth(**flag))
        assert ok.status_code == 200
    assert spy[0][1]["viewer"] == "alice"


async def test_auth_off_binds_no_viewer(
    engine: Engine, spy: list[tuple[str, dict[str, Any]]]
) -> None:
    async with _client(create_app(engine)) as http:
        ok = await http.post(
            "/search", json={"query": "tea"}, headers={"X-Memspine-Namespace": "t"}
        )
        assert ok.status_code == 200
    assert "viewer" not in spy[0][1]


async def test_rest_calls_open_a_request_trace(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    @contextmanager
    def fake(kind: str, /, **fields: Any) -> Iterator[None]:
        seen.append((kind, fields))
        yield

    monkeypatch.setattr(tap, "request", fake)
    async with _client(create_app(engine)) as http:
        h = {"X-Memspine-Namespace": "t"}
        await http.post("/write", json={"content": "tea is nice"}, headers=h)
        await http.post("/search", json={"query": "tea"}, headers=h)
        await http.get("/quarantine", headers=h)
    kinds = [(k, f["route"]) for k, f in seen if f.get("via") == "rest"]
    assert ("write", "/write") in kinds
    assert ("read", "/search") in kinds
    assert all(f["scope"] == "t" for _, f in seen if f.get("via") == "rest")


async def test_tools_use_a_principal_bound_handle(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    @contextmanager
    def fake(kind: str, /, **fields: Any) -> Iterator[None]:
        seen.append((kind, fields))
        yield

    monkeypatch.setattr(tap, "request", fake)
    tools = AgentMemoryTools(engine, ToolContext(namespace="agent-ns", principal="bot"))
    mem = tools._mem()
    assert (mem.namespace, mem.principal) == ("agent-ns", "bot")
    written = await tools.call("memory_write", {"content": "Ana likes tea"})
    assert written["verdict"] == "stored"
    await tools.call("memory_search", {"query": "tea"})
    refused = await tools.call("memory_search", {"query": "tea", "namespace": "other"})
    assert refused["error"] == "unknown_argument"
    kinds = [k for k, f in seen if f.get("via") == "tool"]
    assert kinds == ["write", "read"]
