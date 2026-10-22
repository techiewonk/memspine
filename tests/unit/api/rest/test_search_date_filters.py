"""#37 over REST: ``POST /search`` takes the date-filter fields."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

fastapi = pytest.importorskip("fastapi")

import httpx

from memspine import Engine
from memspine.api.rest import create_app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        embedding={"provider": "hash", "settings": {}},
        memories={"semantic": {"enabled": True}},
    )
    await eng.start()
    await eng.write(
        "pottery class in April", namespace="a", valid_from=datetime(2023, 4, 3, tzinfo=UTC)
    )
    await eng.write(
        "charity race in May", namespace="a", valid_from=datetime(2023, 5, 9, tzinfo=UTC)
    )
    transport = httpx.ASGITransport(app=create_app(eng), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://memspine") as http:
        yield http
    await eng.stop()


async def test_search_date_fields(client: httpx.AsyncClient) -> None:
    headers = {"X-Memspine-Namespace": "a"}
    body = {"query": "pottery class", "top_k": 5, "valid_from_after": "2023-05-01T00:00:00Z"}
    found = await client.post("/search", json=body, headers=headers)
    assert found.status_code == 200
    assert [hit["record"]["content"] for hit in found.json()] == ["charity race in May"]
    both = await client.post(
        "/search",
        json={
            "query": "pottery class",
            "valid_from_after": "2023-05-01T00:00:00Z",
            "valid_from_before": "2023-04-04T00:00:00Z",
            "date_filter_mode": "or",
        },
        headers=headers,
    )
    assert len(both.json()) == 2
    bad = await client.post(
        "/search", json={"query": "x", "date_filter_mode": "xor"}, headers=headers
    )
    assert bad.status_code == 422


async def test_search_graph_filter_field(client: httpx.AsyncClient) -> None:
    # RM-93: the typed graph filter rides on /search; naive times are rejected (RM-82)
    headers = {"X-Memspine-Namespace": "a"}
    body = {
        "query": "charity race",
        "graph_filter": {"assertion_modalities": ["asserted"], "topical_domains": ["sport"]},
    }
    ok = await client.post("/search", json=body, headers=headers)
    assert ok.status_code == 200
    assert [hit["record"]["content"] for hit in ok.json()][0] == "charity race in May"
    naive = {"query": "x", "graph_filter": {"valid_at": "2023-05-01T00:00:00"}}
    assert (await client.post("/search", json=naive, headers=headers)).status_code == 422
    unknown = {"query": "x", "graph_filter": {"no_such_field": 1}}
    assert (await client.post("/search", json=unknown, headers=headers)).status_code == 422
