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
from collections.abc import AsyncIterator, Callable, Iterator

import pytest
import pytest_asyncio

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
    "test_server_template",  # ADR-081 server storage template
    "test_multi_node_locality",  # ADR-081 WP6: two multi_node Engines on one Postgres
)
_PG_LIVE_ONLY_TESTS = {
    "test_provider_live_contracts": frozenset({"test_postgres_storage_contract"}),
    "test_side_tables": frozenset({"test_postgres_engine_keeps_side_tables_in_the_database"}),
    "test_assets_slots": frozenset({"test_postgres_registry_rows_and_erasure"}),
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


@pytest.fixture
def _tracked_sqlite_clients(monkeypatch: pytest.MonkeyPatch) -> list[SQLiteClient]:
    """Track opened clients without allocating an event loop for pure tests."""
    connected: list[SQLiteClient] = []
    original_open = SQLiteClient.open

    async def _tracking_open(self: SQLiteClient) -> None:
        await original_open(self)
        connected.append(self)

    monkeypatch.setattr(SQLiteClient, "open", _tracking_open)
    return connected


@pytest.fixture
async def _close_sqlite_clients_async(
    _tracked_sqlite_clients: list[SQLiteClient],
) -> AsyncIterator[None]:
    yield
    for client in _tracked_sqlite_clients:
        await client.close()


@pytest.fixture(autouse=True)
def _close_orphan_sqlite_clients(
    request: pytest.FixtureRequest,
    _tracked_sqlite_clients: list[SQLiteClient],
) -> Iterator[None]:
    """Close orphan clients while keeping synchronous tests free of asyncio runners.

    Creating a Windows Proactor loop also creates a socket pair. An async autouse
    fixture forced one even for thousands of pure policy/parser cases, exhausting
    socket buffers during the full suite. Async tests retain their scoped runner;
    synchronous tests need a cleanup loop only when they actually opened a client.
    """
    if pytest_asyncio.is_async_test(request.node):
        request.getfixturevalue("_close_sqlite_clients_async")
        yield
        return
    yield
    if _tracked_sqlite_clients:

        async def close_clients() -> None:
            for client in _tracked_sqlite_clients:
                await client.close()

        asyncio.run(close_clients())
