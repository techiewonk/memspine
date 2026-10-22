"""Global test hygiene: the suite must be hermetic against the developer env.

Engine.start() feeds ``os.environ`` into the config layering, so any exported
``MEMSPINE_*`` variable (e.g. someone experimenting with
``MEMSPINE_EVENT_LOG__MODE=ephemeral``) would silently reconfigure every test
engine. Scrub them for the duration of each test.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Callable

import pytest

from memspine.clients.sqlite import SQLiteClient

_PG_MODULES = (
    "test_f16d_storage_profile",
    "test_storage_backends",
    "test_f16f_matrix",
    "test_ex1a_server",
    "test_pgvector_contract",
    "test_qualifiers_contract",  # sql_adjacency on Postgres (ADR-081)
    "test_records_conformance",  # records port on Postgres (ADR-081)
    "test_graph_auto_pin",
)
_PG_LIVE_ONLY_TESTS = {
    "test_provider_live_contracts": frozenset({"test_postgres_storage_contract"}),
    "test_side_tables": frozenset({"test_postgres_engine_keeps_side_tables_in_the_database"}),
}


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> dict[str, Callable[[], asyncio.AbstractEventLoop]]:
    """Select a selector loop only for opt-in live-Postgres tests on Windows.

    Psycopg's async mode is incompatible with Windows' default Proactor loop. The
    pytest-asyncio 1.4 loop-factory hook scopes the alternative to those test items,
    so unrelated asyncio tests retain the platform's normal ``new_event_loop``.
    """
    del config  # The selection depends on the item and explicit live-server opt-in.
    module_name = item.module.__name__.rsplit(".", 1)[-1]
    live_postgres_test = module_name in _PG_MODULES or item.name in _PG_LIVE_ONLY_TESTS.get(
        module_name, ()
    )
    live_postgres = (
        sys.platform == "win32"
        and live_postgres_test
        and (
            os.environ.get("MEMSPINE_TEST_PG_URL")
            or os.environ.get("MEMSPINE_TEST_POSTGRES_URL")
            or os.environ.get("MEMSPINE_TEST_LIVE_POSTGRES_URL")
        )
    )
    if live_postgres:
        return {"windows_selector": asyncio.SelectorEventLoop}
    return {"default": asyncio.new_event_loop}


@pytest.fixture(autouse=True)
def _pin_pre_adr032_default(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mechanics tests select the explicit slim core template.

    Core preserves the bare memory defaults and states required provider deadlines.
    Tests marked ``shipped_default`` still exercise the real assistant default.
    """
    if request.node.get_closest_marker("shipped_default") is None:
        from memspine.config import constants

        monkeypatch.setattr(constants, "DEFAULT_TEMPLATE", "core")


@pytest.fixture(autouse=True)
def _scrub_memspine_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        # MEMSPINE_TEST_* are live-server DSNs for the opt-in server cells; they are
        # not engine config (the Engine reads MEMSPINE_<SECTION>__<KEY>), keep them.
        if key.startswith("MEMSPINE_") and not key.startswith("MEMSPINE_TEST_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
async def _close_orphan_sqlite_clients(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Guarantee every ``SQLiteClient`` that opens during a test is closed.

    Many test helpers build a ``:memory:`` client + storage and never close it;
    the client's checked-out keep-alive anchor connection is then garbage-
    collected while still checked out, which SQLAlchemy reports as an
    *unraisable* ``SAWarning`` — and pytest turns any unraisable into a failure,
    blaming whichever unrelated test happened to trigger the GC. Tracking and
    closing here makes teardown deterministic. ``close()`` is idempotent, so
    tests that already close their engine/client are unaffected.
    """
    connected: list[SQLiteClient] = []
    original_open = SQLiteClient.open

    async def _tracking_open(self: SQLiteClient) -> None:
        await original_open(self)
        connected.append(self)

    monkeypatch.setattr(SQLiteClient, "open", _tracking_open)
    yield
    for client in connected:
        await client.close()
