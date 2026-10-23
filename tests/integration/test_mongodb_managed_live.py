"""ADR-083 / RM-119 live: a managed local mongod runs the records and documents suites.

Skipped unless a real ``mongod`` is available:

- ``MEMSPINE_TEST_MONGOD_BINARY``: path to an existing ``mongod`` executable (its version is
  read from ``--version`` unless ``MEMSPINE_TEST_MONGOD_VERSION`` pins it), or
- ``MEMSPINE_TEST_MONGOD_DOWNLOAD=1`` with ``MEMSPINE_TEST_MONGOD_VERSION`` (e.g. ``9.0.4``):
  the launcher's own consented download from fastdl.mongodb.org into
  ``MEMSPINE_TEST_MONGOD_CACHE`` (default: the user cache directory).

``MEMSPINE_TEST_MONGOD_PORT`` (default 57117) and the next port are used; neither may be taken
by anything but a matching managed server. The module server is started by the launcher and
the records and documents conformance suites (and the RM-112 Engine e2e) run against it in a
child pytest with ``MEMSPINE_TEST_MONGO_URL`` set. A second test runs an Engine whose
``storage.records`` and ``storage.documents`` use ``launch.mode: managed`` itself (start, write,
search, forget, stop, restart on the same dbpath).
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

BINARY = os.environ.get("MEMSPINE_TEST_MONGOD_BINARY")
DOWNLOAD = os.environ.get("MEMSPINE_TEST_MONGOD_DOWNLOAD") == "1"
VERSION = os.environ.get("MEMSPINE_TEST_MONGOD_VERSION")
CACHE = os.environ.get("MEMSPINE_TEST_MONGOD_CACHE")
PORT = int(os.environ.get("MEMSPINE_TEST_MONGOD_PORT", "57117"))
REPO = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.milestone,
    pytest.mark.skipif(
        importlib.util.find_spec("pymongo") is None or not (BINARY or (DOWNLOAD and VERSION)),
        reason=(
            "managed mongod live test: set MEMSPINE_TEST_MONGOD_BINARY, or "
            "MEMSPINE_TEST_MONGOD_DOWNLOAD=1 with MEMSPINE_TEST_MONGOD_VERSION"
        ),
    ),
]


def _version() -> str:
    if VERSION:
        return VERSION
    assert BINARY is not None
    out = subprocess.run([BINARY, "--version"], capture_output=True, text=True, check=True)
    match = re.search(r"db version v(\d+\.\d+\.\d+)", out.stdout)
    assert match is not None, out.stdout
    return match.group(1)


def _launch(port: int, data_dir: Path) -> dict[str, Any]:
    launch: dict[str, Any] = {
        "mode": "managed",
        "version": _version(),
        "port": port,
        "data_dir": str(data_dir),
        "start_timeout_s": 60.0,
    }
    if BINARY:
        launch["binary"] = BINARY
    else:
        launch["download"] = True
        if CACHE:
            launch["cache_dir"] = CACHE
    return launch


@pytest.fixture(scope="module")
def managed_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    from memspine.clients.mongodb.launcher import MongodLauncher
    from memspine.config.schema.providers import MongoLaunchSettings
    from memspine.providers.builtin.mongodb import mongod_spec

    launch = MongoLaunchSettings.model_validate(
        _launch(PORT, tmp_path_factory.mktemp("mongod") / "db")
    )
    launcher = MongodLauncher(mongod_spec(launch, None))
    started = time.monotonic()
    asyncio.run(launcher.open())
    print(f"\nmanaged mongod {launch.version} PRIMARY after {time.monotonic() - started:.2f} s")
    try:
        assert not launcher.adopted and launcher.process is not None
        yield launcher.handle
    finally:
        process = launcher.process
        asyncio.run(launcher.close())
        assert process is not None and process.wait(30) == 0  # a clean shutdown


def test_conformance_suites_and_engine_e2e_against_a_managed_server(
    managed_url: str, tmp_path: Path
) -> None:
    env = {**os.environ, "MEMSPINE_TEST_MONGO_URL": managed_url}
    report = tmp_path / "junit.xml"
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:deepeval",
            "-q",
            f"--junitxml={report}",
            "tests/unit/services/storage/test_records_conformance.py",
            "tests/unit/services/documents/test_documents_conformance.py",
            "tests/unit/services/storage/test_mongodb_records.py",
            "tests/e2e/test_mongodb_engine.py",
            "-k",
            "mongodb or mongo",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    tail = "\n".join(done.stdout.splitlines()[-25:])
    assert done.returncode == 0, tail + done.stderr[-4000:]
    counts: dict[str, dict[str, int]] = {}
    for case in ET.parse(report).getroot().iter("testcase"):
        suite = case.get("classname", "").rsplit(".", 1)[-1]
        outcome = "passed"
        for tag in ("failure", "error", "skipped"):
            if case.find(tag) is not None:
                outcome = tag
        counts.setdefault(suite, {}).setdefault(outcome, 0)
        counts[suite][outcome] += 1
    for suite, by_outcome in sorted(counts.items()):
        print(f"{suite}: {by_outcome}")
    assert all(set(c) == {"passed"} for c in counts.values()), counts


async def test_engine_with_managed_records_and_documents(tmp_path: Path) -> None:
    from memspine import Engine

    port = PORT + 1
    launch = _launch(port, tmp_path / "mongod")

    def engine() -> Any:
        return Engine(
            dotenv_path=None,
            deployment={"id": "managed-live", "data_dir": str(tmp_path / "data")},
            storage={
                "records": {"provider": "mongodb", "settings": {"launch": launch}},
                "documents": {"provider": "mongodb", "settings": {}},
            },
            model={"embedding": {"provider": "hash", "settings": {}}},
        )

    started = time.monotonic()
    eng = engine()
    await eng.start()
    print(f"\nEngine with a managed mongod started in {time.monotonic() - started:.2f} s")
    try:
        ns = "t1/alice"
        kept = await eng.write("Alice moved to Lisbon in 2024", namespace=ns)
        gone = await eng.write("Alice's locker code is 4417", namespace=ns)
        hits = await eng.search("Where does Alice live?", namespace=ns)
        assert kept.record_id in {record.record_id for record, _score in hits}
        head = await eng.documents.put(ns, b"%PDF-1.7 bytes", record_ids=[gone.record_id])
        await eng.forget(gone.record_id, namespace=ns, hard=True)
        assert gone.record_id not in {r.record_id for r in await eng.retrieve(ns)}
        assert await eng.documents.get(ns, head.digest) is None
    finally:
        await eng.stop()
    assert not _port_open(port)  # the last release shut the server down

    again = engine()  # the same dbpath: the replica-set config is found, not re-initiated
    await again.start()
    try:
        assert kept.record_id in {r.record_id for r in await again.retrieve("t1/alice")}
    finally:
        await again.stop()
    assert not _port_open(port)


def _port_open(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0
