"""EX-1a on the server profile (Postgres): the same five steps as ``test_ex1a_embedded``.

The embedded module's step functions are re-used unchanged. An autouse fixture swaps the two
profile-specific helpers (``_overrides`` -> ``storage.profile: server``, ``_sql`` -> the lineage /
outbox probes over the Postgres DSN) and wipes the scratch database before each test.

Needs a scratch Postgres with pgvector: set ``MEMSPINE_TEST_PG_URL`` (skipped otherwise); see
``docs/v2/MILESTONES.md`` for the throwaway container. No GPU and no model: hash embedder, stub
roles. Run: ``pytest tests/e2e/test_ex1a_server.py -m milestone -o addopts=""``.

Differences from the embedded run, by design:

* step 5 (``migrate_sqlite`` replay) skips: it replays a SQLite source. The server replay
  (``Engine.rebuild`` then read) is the F-16f matrix cell ``test_rebuild_then_read``.
* step 4 with ``redact_on`` cannot byte-scan a database file; the on-disk check is vacuous here
  (the erased text is gone from every record and event via ``verify_forget``, which step 4 asserts).
"""

# ruff: noqa: F811
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import test_ex1a_embedded as ex1a
from test_ex1a_embedded import (
    Report,
    stubs,  # noqa: F401  (fixture)
    test_step1_workload_written,
    test_step2_sleep_and_read,
    test_step3_restart_state,
    test_step4_hard_erase,
    world,  # noqa: F401  (fixture)
)

pytestmark = [
    pytest.mark.milestone,
    pytest.mark.filterwarnings("ignore:optimize:UserWarning"),  # lancedb purge, by design
]

_EMBEDDED_OVERRIDES = ex1a._overrides


def _pg_url() -> str | None:
    return os.environ.get("MEMSPINE_TEST_PG_URL") or os.environ.get("MEMSPINE_TEST_POSTGRES_URL")


def _server_overrides(db: Path, redact: bool = False, rederive: bool = False) -> dict[str, Any]:
    o = _EMBEDDED_OVERRIDES(db, redact, rederive)
    o["storage"] = {
        "profile": "server",
        "provider": "postgres",
        "settings": {
            "url": _pg_url(),
            "data_dir": str(db.parent / "derived"),
            "connect_timeout_s": 5.0,
            "statement_timeout_s": 30.0,
        },
    }
    o["runtime"] = {**o.get("runtime", {}), "deployment_id": "e2e-ex1a"}  # shared Postgres
    return o


def _pg_sql(db: Path, query: str) -> list[tuple[Any, ...]]:
    import sqlalchemy as sa

    from memspine.clients.postgres import normalize_pg_url

    engine = sa.create_engine(normalize_pg_url(_pg_url() or ""))
    try:
        with engine.connect() as conn:
            return [tuple(r) for r in conn.execute(sa.text(query)).fetchall()]
    finally:
        engine.dispose()


def _wipe(url: str) -> None:
    import sqlalchemy as sa

    from memspine.clients.postgres import normalize_pg_url

    engine = sa.create_engine(normalize_pg_url(url))
    meta = sa.MetaData()
    meta.reflect(bind=engine)
    meta.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture(autouse=True)
def _server_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    if not _pg_url():
        pytest.skip("server profile: set MEMSPINE_TEST_PG_URL to a scratch Postgres DSN")
    pytest.importorskip("psycopg")
    _wipe(_pg_url() or "")
    monkeypatch.setattr(ex1a, "_overrides", _server_overrides)
    monkeypatch.setattr(ex1a, "_sql", _pg_sql)


@pytest.fixture(scope="module")
def report() -> Report:
    return Report()  # never written: the server run has its own record (ex1_<date>.json)


async def test_server_profile_is_live(world: Any) -> None:
    """The steps above really ran on Postgres (not a silent SQLite fallback)."""
    assert world.eng.config.storage.profile == "server"
    assert not world.db.exists(), "a SQLite file was created"
    assert _pg_sql(world.db, "SELECT COUNT(*) FROM memory_records")[0][0] > 0
    assert _pg_sql(world.db, "SELECT COUNT(*) FROM lineage")[0][0] >= 0


async def test_step5_replay_migrate(*_: Any, **__: Any) -> None:
    pytest.skip("replay: migrate_sqlite replays a SQLite source; the server replay is rebuild")


__all__ = [
    "test_step1_workload_written",
    "test_step2_sleep_and_read",
    "test_step3_restart_state",
    "test_step4_hard_erase",
    "test_step5_replay_migrate",
]
