"""ADR-081 / RM-111 WP6 on a real Postgres: two Engines of one ``multi_node`` deployment.

Needs a scratch Postgres (``MEMSPINE_TEST_PG_URL``; skipped otherwise). Both Engines point at
the same database with the ``server`` template. Under ``deployment.locality: multi_node`` each
one is refused before it opens anything (no table appears in the database) because the
template's lexical slot (Tantivy) and graph slot (``auto``) are host-local or unpinned, and
LanceDB is refused the same way. The same configuration starts under ``single_process`` (the
refusal is the locality rule, not a broken config). With the ``pgvector`` package installed,
two Engines with only cluster slots (Postgres records, pgvector, ``sql_adjacency`` on
Postgres, process cache, hybrid off) both start on the shared database and read each other's
writes. Already on the ADR-081 shape (migrate_config_keys_adr081: skip).
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from memspine.exceptions import ConfigError

pytestmark = [pytest.mark.milestone]


def _pg_url() -> str | None:
    return os.environ.get("MEMSPINE_TEST_PG_URL") or os.environ.get("MEMSPINE_TEST_POSTGRES_URL")


def _tables(url: str) -> set[str]:
    import sqlalchemy as sa

    from memspine.clients.postgres import normalize_pg_url

    engine = sa.create_engine(normalize_pg_url(url))
    try:
        return set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _wipe(url: str) -> None:
    import sqlalchemy as sa

    from memspine.clients.postgres import normalize_pg_url

    engine = sa.create_engine(normalize_pg_url(url))
    try:
        meta = sa.MetaData()
        meta.reflect(bind=engine)
        meta.drop_all(bind=engine)
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
    finally:
        engine.dispose()


def _engine(url: str, deployment: dict[str, Any], **overrides: Any) -> Any:
    from memspine import Engine

    storage = {
        "records": {
            "provider": "postgres",
            "settings": {"url": url, "connect_timeout_s": 5.0, "statement_timeout_s": 30.0},
        },
        **overrides.pop("storage", {}),
    }
    return Engine(
        template="server",
        dotenv_path=None,
        deployment=deployment,
        storage=storage,
        model={"embedding": {"provider": "hash", "settings": {}}},
        **overrides,
    )


@pytest.fixture
def pg_url() -> Any:
    url = _pg_url()
    if not url:
        pytest.skip("multi_node locality e2e: set MEMSPINE_TEST_PG_URL to a scratch Postgres DSN")
    pytest.importorskip("psycopg")
    _wipe(url)
    yield url
    _wipe(url)


async def test_two_multi_node_engines_refuse_host_local_stores_before_opening(
    pg_url: str, tmp_path: Path
) -> None:
    deployment_id = f"wp6-{uuid.uuid4().hex[:8]}"
    node = {"id": deployment_id, "locality": "multi_node", "data_dir": str(tmp_path / "data")}
    lance = {"vector": {"provider": "lancedb"}}
    before = _tables(pg_url)
    for storage, expected in (({}, "storage.lexical provider 'tantivy'"), (lance, "'lancedb'")):
        eng = _engine(pg_url, node, storage=storage, memories={"associative": {"enabled": True}})
        with pytest.raises(ConfigError, match="deployment.locality=multi_node") as raised:
            await eng.start()
        await eng.stop()
        assert expected in str(raised.value)
        assert "storage.graph provider 'auto' must be explicit" in str(raised.value)
    assert _tables(pg_url) == before  # refused before the records store was opened
    assert not (tmp_path / "data").exists()  # and before any host-local file was created

    if importlib.util.find_spec("pgvector") is None:
        # Without pgvector the server template's vector slot is not buildable at all; the
        # single_process control uses LanceDB under data_dir instead.
        control = _engine(pg_url, {**node, "locality": "single_process"}, storage=lance)
    else:
        control = _engine(pg_url, {**node, "locality": "single_process"})
    await control.start()  # the same config is valid in one process: the refusal is locality
    await control.stop()
    assert "alembic_version" in _tables(pg_url)


async def test_two_multi_node_engines_share_one_postgres_with_cluster_slots(
    pg_url: str,
) -> None:
    if importlib.util.find_spec("pgvector") is None:
        pytest.skip("cluster-only vector slot needs the pgvector package (memspine[postgres])")
    deployment = {"id": f"wp6-{uuid.uuid4().hex[:8]}", "locality": "multi_node"}
    cluster = {"graph": {"provider": "sql_adjacency"}}
    options: dict[str, Any] = {
        "storage": cluster,
        "read": {"hybrid": False},
        "memories": {"associative": {"enabled": True}},
    }
    first = _engine(pg_url, deployment, **options)
    second = _engine(pg_url, deployment, **options)
    await first.start()
    try:
        await second.start()
        try:
            await first.write("Noor lives in Utrecht", namespace="ns/wp6", memory_type="semantic")
            found = await second.search("Where does Noor live?", namespace="ns/wp6")
            assert any("Utrecht" in record.content for record, _ in found)
        finally:
            await second.stop()
    finally:
        await first.stop()
