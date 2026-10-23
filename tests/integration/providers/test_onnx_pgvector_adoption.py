"""Run against a disposable pgvector database via MEMSPINE_TEST_POSTGRES_URL."""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

import pytest
from sqlalchemy import select

from memspine.clients.postgres import PostgresClient, PostgresSpec
from memspine.services.vector.pgvector_store import PgVectorProjection, PgVectorStore


@pytest.fixture(scope="session")
def event_loop_policy() -> asyncio.AbstractEventLoopPolicy:
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


async def test_certified_pgvector_reads_old_rows_without_reembedding() -> None:
    url = os.environ.get("MEMSPINE_TEST_POSTGRES_URL")
    if url is None:
        pytest.skip("set MEMSPINE_TEST_POSTGRES_URL to a disposable pgvector database")
    client = PostgresClient(PostgresSpec(url))
    await client.open()
    projection = PgVectorProjection(
        "adoption_" + uuid.uuid4().hex[:16],
        3,
        index="none",
        provision_projection=True,
        create_extension=True,
    )
    before = PgVectorStore(client, projection, embedder_id="fastembed:model")
    try:
        await before.start()
        await before.upsert("r1", "tenant", "fastembed:model", [1.0, 0.0, 0.0])
        await before.upsert("private", "other-tenant", "fastembed:model", [1.0, 0.0, 0.0])
        expected = await before.query("tenant", [1.0, 0.0, 0.0], "fastembed:model")
        async with client.engine.connect() as connection:
            original = (await connection.execute(select(before._table))).all()
        after = PgVectorStore(
            client, projection, embedder_id="embedder:model", legacy_embedder_id="fastembed:model"
        )
        await after.start()
        assert await after.query("tenant", [1.0, 0.0, 0.0], "embedder:model") == expected
        assert await after.scores_for("tenant", [1.0, 0.0, 0.0], "embedder:model", ["r1"]) == {
            "r1": 1.0
        }
        async with client.engine.connect() as connection:
            unchanged = (await connection.execute(select(after._table))).all()
        assert [(r.record_id, r.embedder_id, list(r.embedding)) for r in unchanged] == [
            (r.record_id, r.embedder_id, list(r.embedding)) for r in original
        ]
        await after.upsert("r2", "tenant", "embedder:model", [0.0, 1.0, 0.0])
        assert len(await after.query("tenant", [0.0, 1.0, 0.0], "embedder:model")) == 2
    finally:
        async with client.engine.begin() as connection:
            await connection.run_sync(before._table.drop, checkfirst=True)
        await client.close()
