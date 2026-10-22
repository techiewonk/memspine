"""ADR-081: the ``server`` storage template boots on Postgres with pgvector and Alembic.

Needs a scratch Postgres with the pgvector extension available: set ``MEMSPINE_TEST_PG_URL``
(skipped otherwise). The template's placeholder URL, deployment id and data_dir are overridden;
everything else (records postgres, vector pgvector with provisioning, lexical tantivy under
``deployment.data_dir``) comes from the template. Already on the ADR-081 shape
(migrate_config_keys_adr081: skip).
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

pytestmark = [pytest.mark.milestone]


def _pg_url() -> str | None:
    return os.environ.get("MEMSPINE_TEST_PG_URL") or os.environ.get("MEMSPINE_TEST_POSTGRES_URL")


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


async def test_server_template_boots_on_postgres(tmp_path: Path) -> None:
    url = _pg_url()
    if not url:
        pytest.skip("server template: set MEMSPINE_TEST_PG_URL to a scratch Postgres DSN")
    pytest.importorskip("psycopg")
    import importlib.util

    # Without the pgvector package the vector slot falls back to LanceDB under data_dir; the
    # records slot, Alembic and data_dir placement are still exercised.
    has_pgvector = importlib.util.find_spec("pgvector") is not None
    vector = {} if has_pgvector else {"vector": {"provider": "lancedb"}}
    import sqlalchemy as sa

    from memspine import Engine
    from memspine.clients.postgres import normalize_pg_url

    _wipe(url)
    data_dir = tmp_path / "data"
    eng = Engine(
        template="server",
        dotenv_path=None,
        deployment={"id": f"server-template-{uuid.uuid4().hex[:8]}", "data_dir": str(data_dir)},
        storage={
            "records": {
                "provider": "postgres",
                "settings": {"url": url, "connect_timeout_s": 5.0, "statement_timeout_s": 30.0},
            },
            **vector,
        },
        embedding={"provider": "hash", "settings": {}},
    )
    await eng.start()
    try:
        config = eng.config
        assert config.storage.records.provider == "postgres"
        assert config.storage.vector.provider == ("pgvector" if has_pgvector else "lancedb")
        assert config.storage.lexical.provider == "tantivy"
        await eng.write("Noor lives in Utrecht", namespace="ns/server", memory_type="semantic")
        found = await eng.search("Where does Noor live?", namespace="ns/server")
        assert any("Utrecht" in record.content for record, _ in found)
    finally:
        await eng.stop()
    engine = sa.create_engine(normalize_pg_url(url))
    try:
        tables = set(sa.inspect(engine).get_table_names())
        assert "alembic_version" in tables  # ADR-081: always Alembic
    finally:
        engine.dispose()
    # The host-local lexical index lives under deployment.data_dir.
    assert any(path.name.startswith("memspine") for path in data_dir.iterdir())
    _wipe(url)
