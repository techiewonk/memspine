"""Global test hygiene: the suite must be hermetic against the developer env.

Engine.start() feeds ``os.environ`` into the config layering, so any exported
``MEMSPINE_*`` variable (e.g. someone experimenting with
``MEMSPINE_EVENT_LOG__MODE=ephemeral``) would silently reconfigure every test
engine. Scrub them for the duration of each test.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest

from memspine.clients.sqlite import SQLiteClient


@pytest.fixture(autouse=True)
def _scrub_memspine_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("MEMSPINE_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
async def _close_orphan_sqlite_clients(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Guarantee every ``SQLiteClient`` that connects during a test is closed.

    Many test helpers build a ``:memory:`` client + storage and never close it;
    the client's checked-out keep-alive anchor connection is then garbage-
    collected while still checked out, which SQLAlchemy reports as an
    *unraisable* ``SAWarning`` — and pytest turns any unraisable into a failure,
    blaming whichever unrelated test happened to trigger the GC. Tracking and
    closing here makes teardown deterministic. ``close()`` is idempotent, so
    tests that already close their engine/client are unaffected.
    """
    connected: list[SQLiteClient] = []
    original_connect = SQLiteClient.connect

    async def _tracking_connect(self: SQLiteClient) -> None:
        await original_connect(self)
        connected.append(self)

    monkeypatch.setattr(SQLiteClient, "connect", _tracking_connect)
    yield
    for client in connected:
        await client.close()
