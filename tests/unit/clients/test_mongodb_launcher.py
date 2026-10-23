"""ADR-083 / RM-119: the managed local mongod launcher, with a fake mongod and a fake archive.

The fake ``mongod`` is a Python script run through the launcher's ``_command_prefix`` seam: it
answers ``--version``, records its argv, exits with ``FAKE_MONGOD_EXIT`` when set, and otherwise
stays up until a ``shutdown`` marker appears in its dbpath. The admin connection is a fake
(:class:`FakeAdmin`) holding the replica-set state; downloads go through the ``_open_url`` seam.
No network, no real server.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import socket
import sys
import tarfile
import zipfile
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import IO, Any
from urllib.error import HTTPError

import pytest

from memspine.clients.mongodb import launcher as mod
from memspine.clients.mongodb.launcher import (
    MongodBinaryError,
    MongodChecksumError,
    MongodLauncher,
    MongodPortConflictError,
    MongodSpec,
    MongodStartError,
    MongodUnreachableError,
)

VERSION = "9.0.4"
REAL_PORT_IN_USE = mod._port_in_use

FAKE_MONGOD = r"""
import json, os, sys, time
from pathlib import Path

args = sys.argv[1:]
if "--version" in args:
    print("db version v" + os.environ.get("FAKE_MONGOD_VERSION", "9.0.4"))
    print("Build Info: {}")
    sys.exit(0)
dbpath = Path(args[args.index("--dbpath") + 1])
code = os.environ.get("FAKE_MONGOD_EXIT")
if code:
    print("fake mongod: exiting with", code)
    sys.exit(int(code))
(dbpath / "argv.json").write_text(json.dumps(args))
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    if (dbpath / "shutdown").exists():
        sys.exit(0)
    time.sleep(0.05)
"""


class FakeState:
    def __init__(self) -> None:
        self.initiated = False
        self.set_name: str | None = "rs0"
        self.primary_after = 1  # hello polls before PRIMARY (after initiate)
        self.unreachable = 0  # admin connections that fail first
        self.never_primary = False
        self.hello_error: Exception | None = None
        self.calls: list[str] = []
        self.dbpath: Path | None = None
        self.polls = 0
        self.external = False  # an already running server (adoption): always up

    def up(self) -> bool:
        if self.external:
            return True
        return (
            self.dbpath is not None
            and (self.dbpath / "argv.json").exists()
            and not (self.dbpath / "shutdown").exists()
        )


class FakeAdmin:
    def __init__(self, state: FakeState) -> None:
        self.state = state

    def hello(self) -> dict[str, Any]:
        s = self.state
        s.calls.append("hello")
        if s.hello_error is not None:
            raise s.hello_error
        if s.unreachable > 0 or not s.up():
            s.unreachable = max(0, s.unreachable - 1)
            raise MongodUnreachableError("not yet")
        out: dict[str, Any] = {"ok": 1}
        if s.set_name is not None and s.initiated:
            out["setName"] = s.set_name
            s.polls += 1
            out["isWritablePrimary"] = not s.never_primary and s.polls >= s.primary_after
        return out

    def initiated(self) -> bool:
        self.state.calls.append("status")
        if not self.state.up():
            raise MongodUnreachableError("not yet")
        return self.state.initiated

    def initiate(self, replica_set: str, host: str) -> None:
        self.state.calls.append(f"initiate:{replica_set}:{host}")
        self.state.initiated = True

    def shutdown(self) -> None:
        self.state.calls.append("shutdown")
        assert self.state.dbpath is not None
        (self.state.dbpath / "shutdown").write_text("1")

    def close(self) -> None:
        pass


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeState:
    fake = FakeState()
    monkeypatch.setattr(MongodLauncher, "admin_factory", lambda port, timeout: FakeAdmin(fake))
    script = tmp_path / "fake_mongod.py"
    script.write_text(FAKE_MONGOD, encoding="utf-8")
    monkeypatch.setattr(mod, "_command_prefix", lambda binary: [sys.executable, str(script)])
    monkeypatch.setattr(mod, "_port_in_use", lambda port: False)
    monkeypatch.setattr(mod, "_notice_printed", True)
    monkeypatch.setattr(mod, "_open_url", _no_network)
    return fake


def _no_network(url: str, timeout_s: float) -> Any:
    raise AssertionError(f"unexpected download of {url}")


def _binary(tmp_path: Path) -> Path:
    path = tmp_path / "bin" / mod.executable_name()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake")
    return path


def _spec(tmp_path: Path, state: FakeState | None = None, **kw: Any) -> MongodSpec:
    data_dir = tmp_path / "db"
    if state is not None:
        state.dbpath = data_dir
    base: dict[str, Any] = {
        "version": VERSION,
        "port": 57999,
        "data_dir": str(data_dir),
        "cache_dir": str(tmp_path / "cache"),
        "start_timeout_s": 15.0,
        "shutdown_timeout_s": 10.0,
    }
    base.update(kw)
    return MongodSpec(**base)


# ------------------------------------------------------------------ binary resolution
def test_explicit_binary_wins_over_the_cache(tmp_path: Path, state: FakeState) -> None:
    explicit = _binary(tmp_path)
    cached = tmp_path / "cache" / VERSION / mod.executable_name()
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"cached")
    launcher = MongodLauncher(_spec(tmp_path, binary=str(explicit), download=True))
    assert launcher._resolve_binary() == explicit


def test_the_cache_is_used_before_any_download(tmp_path: Path, state: FakeState) -> None:
    cached = tmp_path / "cache" / VERSION / mod.executable_name()
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"cached")
    launcher = MongodLauncher(_spec(tmp_path, download=True))
    assert launcher._resolve_binary() == cached  # _open_url would fail the test


def test_no_download_without_consent(tmp_path: Path, state: FakeState) -> None:
    launcher = MongodLauncher(_spec(tmp_path))
    with pytest.raises(MongodBinaryError, match="launch.download: true") as raised:
        launcher._resolve_binary()
    assert "launch.binary" in str(raised.value)
    assert not (tmp_path / "cache").exists()


def test_a_missing_explicit_binary_names_the_key(tmp_path: Path, state: FakeState) -> None:
    launcher = MongodLauncher(_spec(tmp_path, binary=str(tmp_path / "nope" / "mongod")))
    with pytest.raises(MongodBinaryError, match="settings.launch.binary"):
        launcher._resolve_binary()


def test_an_explicit_binary_must_match_the_pinned_version(
    tmp_path: Path, state: FakeState, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MONGOD_VERSION", "8.0.4")
    launcher = MongodLauncher(_spec(tmp_path, binary=str(_binary(tmp_path))))
    with pytest.raises(MongodBinaryError, match=r"mongod 8\.0\.4.*pins 9\.0\.4"):
        launcher._resolve_binary()


# --------------------------------------------------------------------------- download
def _zip_archive() -> bytes:
    buf = io.BytesIO()
    root = f"mongodb-win32-x86_64-windows-{VERSION}/"
    with zipfile.ZipFile(buf, "w") as bundle:
        bundle.writestr(root + "LICENSE-Community.txt", "SSPL")
        bundle.writestr(root + "bin/mongod.pdb", b"x" * 1000)
        bundle.writestr(root + "bin/mongos.exe", b"router")
        bundle.writestr(root + "bin/mongod.exe", b"MZ fake mongod")
    return buf.getvalue()


def _tgz_archive() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as bundle:
        for name, data in (
            (f"mongodb-linux-x86_64-ubuntu2404-{VERSION}/bin/mongos", b"router"),
            (f"mongodb-linux-x86_64-ubuntu2404-{VERSION}/bin/mongod", b"\x7fELF fake mongod"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _serve(monkeypatch: pytest.MonkeyPatch, files: dict[str, bytes], opened: list[str]) -> None:
    def open_url(url: str, timeout_s: float) -> AbstractContextManager[IO[bytes]]:
        mod._check_official(url)
        opened.append(url)
        if url not in files:
            raise HTTPError(url, 404, "Not Found", None, None)  # type: ignore[arg-type]
        return io.BytesIO(files[url])

    monkeypatch.setattr(mod, "_open_url", open_url)


@pytest.mark.parametrize(
    ("url", "archive", "name", "payload"),
    [
        (
            f"https://fastdl.mongodb.org/windows/mongodb-windows-x86_64-{VERSION}.zip",
            _zip_archive(),
            "mongod.exe",
            b"MZ fake mongod",
        ),
        (
            f"https://fastdl.mongodb.org/linux/mongodb-linux-x86_64-ubuntu2404-{VERSION}.tgz",
            _tgz_archive(),
            "mongod",
            b"\x7fELF fake mongod",
        ),
    ],
)
def test_download_verifies_the_sha256_and_extracts_only_mongod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    archive: bytes,
    name: str,
    payload: bytes,
) -> None:
    digest = hashlib.sha256(archive).hexdigest()
    opened: list[str] = []
    _serve(monkeypatch, {url + ".sha256": f"{digest}  x\n".encode(), url: archive}, opened)
    monkeypatch.setattr(mod, "executable_name", lambda system=None: name)
    dest = tmp_path / "cache" / VERSION
    path = mod.download_mongod(VERSION, dest, urls=[url])
    assert path == dest / name and path.read_bytes() == payload
    assert sorted(p.name for p in dest.iterdir()) == [name]  # no archive, pdb or mongos
    assert opened == [url + ".sha256", url]


def test_a_checksum_mismatch_refuses_before_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"https://fastdl.mongodb.org/windows/mongodb-windows-x86_64-{VERSION}.zip"
    _serve(monkeypatch, {url + ".sha256": b"0" * 64 + b"  x\n", url: _zip_archive()}, [])
    dest = tmp_path / "cache" / VERSION
    with pytest.raises(MongodChecksumError, match="does not match the published"):
        mod.download_mongod(VERSION, dest, urls=[url])
    assert list(dest.iterdir()) == []  # the archive was deleted, nothing extracted


def test_the_first_published_linux_target_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    urls = mod.archive_urls(
        VERSION,
        system="Linux",
        machine="x86_64",
        os_release={"ID": "rocky", "ID_LIKE": "rhel centos fedora", "VERSION_ID": "9.3"},
    )
    assert [u.rsplit("/", 1)[1] for u in urls] == [
        f"mongodb-linux-x86_64-rhel93-{VERSION}.tgz",
        f"mongodb-linux-x86_64-rhel9-{VERSION}.tgz",
        f"mongodb-linux-x86_64-rhel90-{VERSION}.tgz",
    ]
    archive = _tgz_archive()
    opened: list[str] = []
    files = {urls[1] + ".sha256": hashlib.sha256(archive).hexdigest().encode(), urls[1]: archive}
    _serve(monkeypatch, files, opened)
    monkeypatch.setattr(mod, "executable_name", lambda system=None: "mongod")
    mod.download_mongod(VERSION, tmp_path / "c", urls=urls)
    assert opened == [urls[0] + ".sha256", urls[1] + ".sha256", urls[1]]


def test_platform_archive_urls_are_official() -> None:
    assert mod.archive_urls(VERSION, system="Windows", machine="AMD64") == [
        f"https://fastdl.mongodb.org/windows/mongodb-windows-x86_64-{VERSION}.zip"
    ]
    assert mod.archive_urls(VERSION, system="Darwin", machine="arm64") == [
        f"https://fastdl.mongodb.org/osx/mongodb-macos-arm64-{VERSION}.tgz"
    ]
    assert mod.archive_urls(VERSION, system="Linux", machine="aarch64", os_release={}) == []


@pytest.mark.parametrize(
    "url",
    [
        "http://fastdl.mongodb.org/windows/x.zip",
        "https://example.com/mongodb-windows-x86_64-9.0.4.zip",
        "https://fastdl.mongodb.org.evil.test/x.zip",
    ],
)
def test_only_the_official_host_is_fetched(tmp_path: Path, url: str) -> None:
    with pytest.raises(MongodBinaryError, match="fastdl.mongodb.org"):
        mod.download_mongod(VERSION, tmp_path, urls=[url])


# --------------------------------------------------------------------------- lifecycle
async def test_start_initiates_once_waits_for_primary_and_shuts_down(
    tmp_path: Path, state: FakeState
) -> None:
    state.unreachable = 2
    state.primary_after = 3
    spec = _spec(tmp_path, state, binary=str(_binary(tmp_path)), cache_size_gb=0.5)
    launcher = MongodLauncher(spec)
    await launcher.open()
    process = launcher.process
    assert process is not None and process.poll() is None
    assert launcher.handle == "mongodb://127.0.0.1:57999/?directConnection=true&replicaSet=rs0"
    argv = json.loads((tmp_path / "db" / "argv.json").read_text())
    assert argv[:6] == ["--replSet", "rs0", "--bind_ip", "127.0.0.1", "--port", "57999"]
    assert argv[argv.index("--dbpath") + 1] == str(tmp_path / "db")
    assert argv[argv.index("--logpath") + 1] == str(tmp_path / "db" / "mongod.log")
    assert argv[argv.index("--wiredTigerCacheSizeGB") + 1] == "0.5"
    assert state.calls.count("initiate:rs0:127.0.0.1:57999") == 1
    assert await launcher.health(5.0) is True
    await launcher.close()
    assert process.wait(10) == 0 and "shutdown" in state.calls
    assert await launcher.health(5.0) is False
    await launcher.close()  # idempotent

    # A restart on the existing dbpath finds the config and does not initiate again.
    (tmp_path / "db" / "shutdown").unlink()
    state.calls.clear()
    again = MongodLauncher(spec)
    await again.open()
    await again.close()
    assert "status" in state.calls
    assert not [c for c in state.calls if c.startswith("initiate")]


async def test_a_server_that_never_becomes_primary_times_out_and_is_stopped(
    tmp_path: Path, state: FakeState
) -> None:
    state.never_primary = True
    spec = _spec(tmp_path, state, binary=str(_binary(tmp_path)), start_timeout_s=0.6)
    launcher = MongodLauncher(spec)
    with pytest.raises(MongodStartError, match="did not become PRIMARY.*start_timeout_s=0.6"):
        await launcher.open()
    assert launcher.process is None  # terminated, not leaked


async def test_an_early_exit_reports_the_code_and_the_log(
    tmp_path: Path, state: FakeState, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MONGOD_EXIT", "100")
    launcher = MongodLauncher(_spec(tmp_path, state, binary=str(_binary(tmp_path))))
    with pytest.raises(MongodStartError, match="exited with code 100") as raised:
        await launcher.open()
    assert "fake mongod: exiting with 100" in str(raised.value)


def test_a_missing_vc_runtime_exit_is_named(tmp_path: Path) -> None:
    launcher = MongodLauncher(_spec(tmp_path))
    for code in mod.VCRUNTIME_EXIT_CODES:
        message = launcher._exit_message(code)
        assert "0xc0000135" in message and "Visual C++ Redistributable" in message
    assert "Visual C++" not in launcher._exit_message(1)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows exit codes are 32-bit")
async def test_a_real_vc_runtime_exit_code_is_detected(
    tmp_path: Path, state: FakeState, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MONGOD_EXIT", str(0xC0000135))
    launcher = MongodLauncher(_spec(tmp_path, state, binary=str(_binary(tmp_path))))
    with pytest.raises(MongodStartError, match="Visual C\\+\\+ Redistributable"):
        await launcher.open()


@contextmanager
def _listener() -> Iterator[int]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        yield int(sock.getsockname()[1])


async def test_a_foreign_process_on_the_port_is_a_start_error_naming_it(
    tmp_path: Path, state: FakeState, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod, "_port_in_use", REAL_PORT_IN_USE)
    state.hello_error = MongodUnreachableError("not mongo")
    with _listener() as port:
        launcher = MongodLauncher(_spec(tmp_path, state, port=port, download=True))
        with pytest.raises(MongodPortConflictError, match=f"port {port} on 127.0.0.1"):
            await launcher.open()
    assert not (tmp_path / "db").exists()  # nothing was started


async def test_a_mongod_of_another_replica_set_is_a_port_conflict(
    tmp_path: Path, state: FakeState, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod, "_port_in_use", lambda port: True)
    state.initiated, state.set_name, state.external = True, "other", True
    launcher = MongodLauncher(_spec(tmp_path, state, download=True))
    with pytest.raises(MongodPortConflictError, match="replica set 'other', not 'rs0'"):
        await launcher.open()


async def test_a_matching_running_mongod_is_adopted_and_never_stopped(
    tmp_path: Path, state: FakeState, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod, "_port_in_use", lambda port: True)
    state.initiated = state.external = True
    launcher = MongodLauncher(_spec(tmp_path, state))  # no binary, no consent: not needed
    await launcher.open()
    assert launcher.adopted and launcher.process is None
    await launcher.close()
    assert "shutdown" not in state.calls
    assert not [c for c in state.calls if c.startswith("initiate")]


async def test_engines_share_one_refcounted_server_stopped_on_the_last_release(
    tmp_path: Path, state: FakeState
) -> None:
    from memspine.providers.connectors import acquire
    from memspine.providers.contexts import SharedBuildContext
    from memspine.providers.lifecycle import ProviderLifecycleManager
    from memspine.providers.shared import ResourceRegistry

    registry = ResourceRegistry()
    spec = _spec(tmp_path, state, binary=str(_binary(tmp_path)))
    managers = [ProviderLifecycleManager(close_timeout_s=20.0) for _ in range(2)]
    launchers = []
    for i, manager in enumerate(managers):
        ctx = SharedBuildContext(manager, f"engine-{i}", 2.0, registry=registry)
        launcher, _ref = await acquire(ctx, MongodLauncher, spec, resource_id="records.mongod")
        launchers.append(launcher)
    assert launchers[0] is launchers[1]
    process = launchers[0].process
    assert process is not None
    await managers[0].close_all()
    await asyncio.sleep(0.2)
    assert process.poll() is None and "shutdown" not in state.calls
    await managers[1].close_all()
    assert process.wait(10) == 0 and state.calls.count("shutdown") == 1


def test_the_registry_key_is_binary_port_and_data_dir(tmp_path: Path) -> None:
    a = _spec(tmp_path, binary=str(tmp_path / "m"))
    same = _spec(tmp_path, binary=str(tmp_path / "x" / ".." / "m"), start_timeout_s=99.0)
    other_port = _spec(tmp_path, binary=str(tmp_path / "m"), port=58000)
    other_dir = _spec(tmp_path, binary=str(tmp_path / "m"), data_dir=str(tmp_path / "db2"))
    key = MongodLauncher.identity
    assert key(a).key_target() == key(same).key_target()
    assert key(a).key_target() != key(other_port).key_target()
    assert key(a).key_target() != key(other_dir).key_target()
    assert MongodLauncher.locality(a) == "host"


def test_the_sspl_notice_is_printed_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(mod, "_notice_printed", False)
    mod._print_notice_once()
    mod._print_notice_once()
    assert capsys.readouterr().err.count("Server Side Public License") == 1
