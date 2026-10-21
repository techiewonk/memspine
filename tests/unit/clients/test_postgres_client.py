"""PostgreSQL connect deadline (RM-40), without a live server.

psycopg applies libpq's ``connect_timeout`` per host/address attempt and raises
any value below 2 s to 2 s, so ``connect_timeout=1`` to ``localhost`` (::1 then
127.0.0.1) waits about 4 s. The client therefore enforces ``connect_timeout_s``
as one overall deadline. The peer here is a listening socket that never
accepts: TCP connects, the startup reply never comes, so only a timeout ends
the attempt.
"""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import Iterator

import pytest

pytest.importorskip("psycopg")

from memspine.clients.postgres import PostgresClient, driver_connect_timeout
from memspine.config import constants

_DEADLINE_S = 0.5
#: Scheduling slack on a loaded CI runner; still well below the driver's 2 s floor.
_SLACK_S = 1.0


@pytest.fixture
def silent_port() -> Iterator[int]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    try:
        yield int(sock.getsockname()[1])
    finally:
        sock.close()


def test_driver_timeout_is_whole_seconds_with_the_driver_floor() -> None:
    floor = constants.PG_DRIVER_MIN_CONNECT_TIMEOUT_S
    assert driver_connect_timeout(0.5) == floor
    assert driver_connect_timeout(1.0) == floor
    assert driver_connect_timeout(2.0) == 2
    assert driver_connect_timeout(2.1) == 3


def test_connect_timeout_is_one_overall_deadline(silent_port: int) -> None:
    client = PostgresClient(
        f"postgresql://unused:unused@localhost:{silent_port}/unused",
        connect_timeout_s=_DEADLINE_S,
    )

    async def attempt() -> float:
        start = time.perf_counter()
        with pytest.raises(TimeoutError):
            await client.connect()
        return time.perf_counter() - start

    # psycopg's async mode needs a selector loop (Windows defaults to Proactor).
    elapsed = asyncio.run(attempt(), loop_factory=asyncio.SelectorEventLoop)
    assert _DEADLINE_S <= elapsed < _DEADLINE_S + _SLACK_S
    assert not asyncio.run(client.health(), loop_factory=asyncio.SelectorEventLoop)
