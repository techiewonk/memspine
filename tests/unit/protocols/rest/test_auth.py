"""#51 reference REST auth middleware, plus the #46/#47 routes (/export, /correct)."""

from __future__ import annotations

import sys
import types
from collections.abc import AsyncIterator
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")

import httpx  # noqa: E402

from memspine import Engine  # noqa: E402
from memspine.config.schema import RestConfig  # noqa: E402
from memspine.core.events import EventKind  # noqa: E402
from memspine.exceptions import ConfigError  # noqa: E402
from memspine.protocols.rest import create_app  # noqa: E402
from memspine.protocols.rest.auth import RateLimiter  # noqa: E402

ALICE_KEY = "alice-test-key-0001"
ADMIN_KEY = "admin-test-key-0002"


def _rest(**extra: Any) -> RestConfig:
    return RestConfig.model_validate(
        {
            "auth": {
                "mode": "api_key",
                "api_keys": [
                    {
                        "key_env": "MS_TEST_ALICE_KEY",
                        "principal": "alice",
                        "namespaces": ["alice*"],
                    },
                    {
                        "key_env": "MS_TEST_ADMIN_KEY",
                        "principal": "ops",
                        "namespaces": ["*"],
                        "admin": True,
                    },
                ],
            },
            **extra,
        }
    )


@pytest.fixture
async def engine() -> AsyncIterator[Engine]:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        read={"hybrid": False},
        memories={"semantic": {"enabled": True}},
        audit={"reads": True, "actions": True},
    )
    await eng.start()
    yield eng
    await eng.stop()


def _client(app: Any) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://memspine")


def _h(key: str | None, namespace: str = "alice") -> dict[str, str]:
    headers = {"X-Memspine-Namespace": namespace}
    if key is not None:
        headers["Authorization"] = f"Bearer {key}"
    return headers


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MS_TEST_ALICE_KEY", ALICE_KEY)
    monkeypatch.setenv("MS_TEST_ADMIN_KEY", ADMIN_KEY)


async def test_api_key_binds_principal_and_namespaces(engine: Engine, keys: None) -> None:
    async with _client(create_app(engine, _rest())) as http:
        missing = await http.post("/write", json={"content": "x"}, headers=_h(None))
        assert missing.status_code == 401
        wrong = await http.post("/write", json={"content": "x"}, headers=_h("nope"))
        assert wrong.status_code == 401
        assert "nope" not in wrong.text and ALICE_KEY not in wrong.text
        ok = await http.post("/write", json={"content": "Ana likes tea"}, headers=_h(ALICE_KEY))
        assert ok.status_code == 200
        x_key = await http.post(
            "/search",
            json={"query": "tea"},
            headers={"X-API-Key": ALICE_KEY, "X-Memspine-Namespace": "alice/sub"},
        )
        assert x_key.status_code == 200
        foreign = await http.post(
            "/search", json={"query": "tea"}, headers=_h(ALICE_KEY, namespace="bob")
        )
        assert foreign.status_code == 403
    reads = [
        e
        for e in await engine._require_started().read_events(0, 10_000)
        if e.kind is EventKind.READ_AUDIT
    ]
    assert reads and reads[-1].actor == "alice"  # the bound principal, not a claim


async def test_admin_routes_need_the_admin_role(engine: Engine, keys: None) -> None:
    async with _client(create_app(engine, _rest())) as http:
        for method, path in (("post", "/sleep"), ("post", "/rebuild"), ("get", "/export")):
            denied = await getattr(http, method)(path, headers=_h(ALICE_KEY))
            assert denied.status_code == 403, path
        assert (await http.get("/quarantine", headers=_h(ALICE_KEY))).status_code == 403
        allowed = await http.get("/export", headers=_h(ADMIN_KEY, namespace="alice"))
        assert allowed.status_code == 200
        assert allowed.headers["content-type"].startswith("application/x-ndjson")


async def test_rate_limit_returns_429(engine: Engine, keys: None) -> None:
    rest = _rest(rate_limit={"requests_per_second": 0.001, "burst": 2})
    async with _client(create_app(engine, rest)) as http:
        codes = [(await http.get("/describe", headers=_h(ALICE_KEY))).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_token_bucket_refills() -> None:
    now = [0.0]
    limiter = RateLimiter(
        RestConfig.model_validate(
            {"rate_limit": {"requests_per_second": 1, "burst": 1}}
        ).rate_limit,  # type: ignore[arg-type]
        clock=lambda: now[0],
    )
    assert limiter.allow("a") and not limiter.allow("a") and limiter.allow("b")
    now[0] = 1.0
    assert limiter.allow("a")


async def test_mode_none_is_unchanged(engine: Engine) -> None:
    async with _client(create_app(engine)) as http:
        ok = await http.post("/write", json={"content": "x"}, headers=_h(None, namespace="bob"))
        assert ok.status_code == 200
        assert (await http.post("/sleep")).status_code == 200


def test_missing_key_env_is_a_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from memspine.protocols.rest.auth import Authenticator

    monkeypatch.delenv("MS_TEST_ALICE_KEY", raising=False)
    with pytest.raises(ConfigError, match="MS_TEST_ALICE_KEY"):
        Authenticator(_rest())


def test_oidc_jwt_without_pyjwt_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from memspine.protocols.rest.auth import Authenticator

    monkeypatch.setitem(sys.modules, "jwt", None)  # import jwt -> ImportError
    rest = RestConfig.model_validate({"auth": {"mode": "oidc_jwt", "jwt": {"key_env": "K"}}})
    with pytest.raises(ConfigError, match="pyjwt"):
        Authenticator(rest, env={"K": "secret"})


async def test_oidc_jwt_claims_bind_principal(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = types.ModuleType("jwt")
    tokens = {
        "tok-ana": {"sub": "ana", "memspine_namespaces": "alice", "roles": []},
        "tok-ops": {"sub": "ops", "memspine_namespaces": ["*"], "roles": ["memspine-admin"]},
    }

    def decode(token: str, key: str, **_: Any) -> dict[str, Any]:
        assert key == "hs-secret"
        if token not in tokens:
            raise ValueError("bad signature")
        return tokens[token]

    fake.decode = decode  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "jwt", fake)
    monkeypatch.setenv("MS_TEST_JWT_KEY", "hs-secret")
    rest = RestConfig.model_validate(
        {
            "auth": {
                "mode": "oidc_jwt",
                "jwt": {"key_env": "MS_TEST_JWT_KEY", "algorithms": ["HS256"]},
            }
        }
    )
    async with _client(create_app(engine, rest)) as http:
        assert (await http.get("/describe", headers=_h("forged"))).status_code == 401
        assert (await http.get("/describe", headers=_h("tok-ana"))).status_code == 200
        assert (await http.post("/rebuild", headers=_h("tok-ana"))).status_code == 403
        assert (await http.post("/rebuild", headers=_h("tok-ops"))).status_code == 200


async def test_correct_route_supersedes_and_export_route(engine: Engine) -> None:
    async with _client(create_app(engine)) as http:
        written = await http.post(
            "/write",
            json={"content": "Ana lives in Lyon", "entity": "ana", "attribute": "city"},
            headers=_h(None),
        )
        old_id = written.json()["record_id"]
        fixed = await http.post(
            "/correct",
            json={"entity": "ana", "attribute": "city", "new_value": "Ana lives in Nice"},
            headers=_h(None),
        )
        assert fixed.status_code == 200
        assert fixed.json()["source"]["channel"] == "rest"
        bad = await http.post("/correct", json={"new_value": "x"}, headers=_h(None))
        assert bad.status_code == 400
        exported = await http.get("/export", params={"include_events": True}, headers=_h(None))
        assert exported.status_code == 200
        lines = exported.text.strip().splitlines()
        assert old_id in exported.text and fixed.json()["record_id"] in exported.text
        assert '"include_events":true' in lines[0] and '"type":"export"' in lines[0]
