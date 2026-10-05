"""#37 over REST: ``POST /search`` takes the date-filter fields."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

fastapi = pytest.importorskip("fastapi")

import httpx  # noqa: E402

from memspine import Engine  # noqa: E402
from memspine.protocols.rest import create_app  # noqa: E402


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
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
