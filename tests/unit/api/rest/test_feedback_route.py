"""#54: ``POST /feedback`` over the REST app."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

fastapi = pytest.importorskip("fastapi")

import httpx

from memspine import Engine
from memspine.api.rest import create_app


@pytest.fixture
async def client() -> AsyncIterator[tuple[Engine, httpx.AsyncClient]]:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        read={"hybrid": False},
    )
    await eng.start()
    transport = httpx.ASGITransport(app=create_app(eng), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://memspine") as http:
        yield eng, http
    await eng.stop()


async def test_feedback_route_counts_and_scopes(
    client: tuple[Engine, httpx.AsyncClient],
) -> None:
    eng, http = client
    record = await eng.write("Ana likes green tea", namespace="a")
    headers = {"X-Memspine-Namespace": "a"}
    resp = await http.post(
        "/feedback", json={"record_id": record.record_id, "signal": "like"}, headers=headers
    )
    assert resp.status_code == 200 and resp.json()["scoring"]["likes"] == 1
    resp = await http.post(
        "/feedback",
        json={"record_id": record.record_id, "signal": "note", "note": "keep"},
        headers=headers,
    )
    assert resp.status_code == 200 and resp.json()["scoring"]["notes"] == 1
    foreign = await http.post(
        "/feedback",
        json={"record_id": record.record_id, "signal": "like"},
        headers={"X-Memspine-Namespace": "b"},
    )
    assert foreign.status_code == 409
    bad = await http.post(
        "/feedback", json={"record_id": record.record_id, "signal": "love"}, headers=headers
    )
    assert bad.status_code == 422
