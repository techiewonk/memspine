"""Managed local ``mongod`` (ADR-083, RM-119): an opt-in launcher owned by the client layer.

``storage.records`` / ``storage.documents`` provider ``mongodb`` with ``launch.mode: managed``
makes memspine start and own a one-member replica set on this host:

- **Binary.** ``launch.binary`` (an explicit path), else ``<cache_dir>/<version>/mongod[.exe]``,
  else a download that needs ``launch.download: true``. The archive comes only from MongoDB's
  official download host (``fastdl.mongodb.org``) for the pinned version and this platform; its
  published SHA-256 (``<archive>.sha256``) is checked before extraction, and only the ``mongod``
  executable is extracted into the short cache path (the 1-2 GB of debug symbols and the long
  archive paths that broke pymongo_inmemory on Windows are never written). memspine never
  ships the binary (SSPL); the licence notice is printed once per process.
- **Start.** ``mongod --replSet <rs> --bind_ip 127.0.0.1 --port <p> --dbpath <dir>`` (plus
  ``--wiredTigerCacheSizeGB``), logging to ``<dir>/mongod.log``. ``replSetInitiate`` runs only
  when the member has no config; the launcher waits for PRIMARY within ``start_timeout_s``.
  On Windows an exit with ``STATUS_DLL_NOT_FOUND`` names the missing VC++ runtime.
- **Port.** Never picked implicitly. A ``mongod`` already listening there that reports the same
  replica set is adopted (never killed on close); any other listener is a start error naming
  the port.
- **Sharing.** :class:`MongodLauncher` is a :class:`~memspine.clients.base.Connector`, shared
  through the ADR-079 registry keyed by (binary, port, data_dir): one ``mongod`` per process,
  reference-counted across Engines, stopped with ``shutdown`` on the last release.

The server binds loopback only; exposing it is out of scope (use ``launch.mode: external``).
No new dependency: the stdlib plus pymongo (imported lazily, ``[mongodb]``).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from collections.abc import Callable
from contextlib import AbstractContextManager, closing
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, TYPE_CHECKING, Any, ClassVar, Protocol, cast
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import urlopen

from memspine.clients._common.deadlines import probe
from memspine.clients.base import ConnectorIdentity, Locality, Scope
from memspine.exceptions import MissingServiceError, StorageError

if TYPE_CHECKING:
    from pymongo import MongoClient

__all__ = [
    "DOWNLOAD_HOST",
    "SSPL_NOTICE",
    "VCRUNTIME_EXIT_CODES",
    "MongodAdmin",
    "MongodBinaryError",
    "MongodChecksumError",
    "MongodLaunchError",
    "MongodLauncher",
    "MongodPortConflictError",
    "MongodSpec",
    "MongodStartError",
    "MongodUnreachableError",
    "archive_urls",
    "default_cache_dir",
    "download_mongod",
    "executable_name",
]

_log = logging.getLogger(__name__)

#: The only host an archive or checksum is fetched from (MongoDB's official download CDN).
DOWNLOAD_HOST = "fastdl.mongodb.org"
#: ``STATUS_DLL_NOT_FOUND`` as an unsigned and a signed exit code: on a clean Windows machine
#: ``mongod.exe`` exits with it when the Microsoft Visual C++ runtime is missing.
VCRUNTIME_EXIT_CODES = frozenset({0xC0000135, 0xC0000135 - (1 << 32)})
SSPL_NOTICE = (
    "memspine: launch.mode managed runs MongoDB Community Server, which MongoDB, Inc. licenses "
    "under the Server Side Public License (SSPL v1, https://www.mongodb.com/legal/licensing/"
    "server-side-public-license). memspine does not ship or vendor it; it is downloaded from "
    "MongoDB's servers only with launch.download: true, or taken from launch.binary."
)
_CHUNK = 1024 * 1024
_POLL_S = 0.1
_PORT_PROBE_S = 0.5
_ADMIN_TIMEOUT_S = 2.0
_TERMINATE_GRACE_S = 3.0
_LOG_TAIL_LINES = 12
_VERSION_RE = re.compile(r"db version v(\d+\.\d+\.\d+)")

_notice_lock = threading.Lock()
_notice_printed = False


class MongodLaunchError(StorageError):
    """The managed ``mongod`` could not be provided (ADR-083)."""


class MongodBinaryError(MongodLaunchError):
    """No usable ``mongod`` executable (missing, not consented to download, wrong version)."""


class MongodChecksumError(MongodBinaryError):
    """The downloaded archive does not match MongoDB's published SHA-256."""


class MongodPortConflictError(MongodLaunchError):
    """Another process (not a ``mongod`` of the same replica set) holds the port."""


class MongodStartError(MongodLaunchError):
    """``mongod`` exited, or did not become PRIMARY within ``start_timeout_s``."""


class MongodUnreachableError(Exception):
    """The admin connection could not reach the server (yet); the start loop retries."""


# ---------------------------------------------------------------------------------- spec
@dataclass(frozen=True, slots=True)
class MongodSpec:
    """One managed server (a frozen spec, never a config model)."""

    version: str
    port: int
    data_dir: str
    binary: str | None = None
    download: bool = False
    cache_dir: str | None = None
    replica_set: str = "rs0"
    start_timeout_s: float = 30.0
    cache_size_gb: float | None = None
    shutdown_timeout_s: float = 8.0

    @property
    def url(self) -> str:
        """``mongodb://127.0.0.1:<port>/?directConnection=true&replicaSet=<rs>``."""
        return (
            f"mongodb://127.0.0.1:{self.port}/?directConnection=true&replicaSet={self.replica_set}"
        )

    def binary_path(self) -> Path:
        """The explicit ``binary``, else the cached ``<cache_dir>/<version>/mongod[.exe]``."""
        if self.binary is not None:
            return Path(self.binary).expanduser()
        base = Path(self.cache_dir).expanduser() if self.cache_dir else default_cache_dir()
        return base / self.version / executable_name()


def executable_name(system: str | None = None) -> str:
    return "mongod.exe" if (system or platform.system()) == "Windows" else "mongod"


def default_cache_dir() -> Path:
    """A short per-user cache path: ``%LOCALAPPDATA%\\memspine\\mongod`` on Windows,
    ``~/Library/Caches/memspine/mongod`` on macOS, ``$XDG_CACHE_HOME/memspine/mongod``
    (``~/.cache``) elsewhere."""
    system = platform.system()
    if system == "Windows":
        root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(root) / "memspine" / "mongod"
    if system == "Darwin":
        return Path.home() / "Library" / "Caches" / "memspine" / "mongod"
    root = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(root) / "memspine" / "mongod"


def _path_identity(path: Path) -> str:
    return os.path.normcase(str(path.expanduser().resolve(strict=False)))


# ------------------------------------------------------------------------------ download
def _os_release() -> dict[str, str]:
    out: dict[str, str] = {}
    for name in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            text = Path(name).read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                out[key.strip()] = value.strip().strip('"')
        break
    return out


def _linux_targets(release: dict[str, str]) -> list[str]:
    distro = release.get("ID", "").lower()
    like = release.get("ID_LIKE", "").lower().split()
    version = release.get("VERSION_ID", "")
    major, _, minor = version.partition(".")
    minor = minor.partition(".")[0]
    if distro == "ubuntu":
        return [f"ubuntu{version.replace('.', '')}"]
    if distro == "debian":
        return [f"debian{major}"]
    if distro == "amzn":
        return ["amazon2023" if major == "2023" else "amazon2"]
    if distro in ("sles", "opensuse-leap"):
        return [f"suse{major}"]
    if distro in ("rhel", "centos", "rocky", "almalinux", "ol") or "rhel" in like:
        names = [f"rhel{major}{minor}"] if minor else []
        return [*names, f"rhel{major}", f"rhel{major}0"]
    return []


def archive_urls(
    version: str,
    *,
    system: str | None = None,
    machine: str | None = None,
    os_release: dict[str, str] | None = None,
) -> list[str]:
    """Candidate archive URLs on :data:`DOWNLOAD_HOST` for ``version`` on this platform, most
    specific first (Linux archives are per distribution; the first one whose ``.sha256``
    exists is used)."""
    system = system or platform.system()
    arch = (machine or platform.machine()).lower()
    base = f"https://{DOWNLOAD_HOST}"
    if system == "Windows":
        if arch not in ("amd64", "x86_64"):
            return []
        return [f"{base}/windows/mongodb-windows-x86_64-{version}.zip"]
    if system == "Darwin":
        name = "arm64" if arch in ("arm64", "aarch64") else "x86_64"
        return [f"{base}/osx/mongodb-macos-{name}-{version}.tgz"]
    if system == "Linux":
        name = "aarch64" if arch in ("arm64", "aarch64") else "x86_64"
        targets = _linux_targets(os_release if os_release is not None else _os_release())
        return [f"{base}/linux/mongodb-linux-{name}-{t}-{version}.tgz" for t in targets]
    return []


def _check_official(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != DOWNLOAD_HOST:
        raise MongodBinaryError(
            f"refusing to download {url!r}: managed mongod archives come only from "
            f"https://{DOWNLOAD_HOST}"
        )


def _open_url(url: str, timeout_s: float) -> AbstractContextManager[IO[bytes]]:
    """Open an official download URL (tests replace this seam)."""
    _check_official(url)
    return cast(AbstractContextManager[IO[bytes]], urlopen(url, timeout=timeout_s))


def _published_sha256(url: str, timeout_s: float) -> str | None:
    """The hex digest from ``<url>.sha256``, or None when the archive does not exist (404)."""
    try:
        with _open_url(f"{url}.sha256", timeout_s) as response:
            text = response.read(4096).decode("ascii", "replace")
    except HTTPError as exc:
        if exc.code in (403, 404):
            return None
        raise
    match = re.search(r"\b([0-9a-fA-F]{64})\b", text)
    if match is None:
        raise MongodChecksumError(f"{url}.sha256 does not hold a SHA-256 digest")
    return match.group(1).lower()


def _extract_mongod(archive: Path, target: Path) -> None:
    """Write only ``bin/mongod[.exe]`` from ``archive`` to ``target``."""
    names = ("mongod.exe", "mongod")

    def wanted(name: str) -> bool:
        path = PurePosixPath(name.replace("\\", "/"))
        return path.name in names and path.parent.name == "bin"

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zipped:
            for info in zipped.infolist():
                if not info.is_dir() and wanted(info.filename):
                    with zipped.open(info) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst, _CHUNK)
                    return
    else:
        with tarfile.open(archive, "r:*") as tarred:
            for member in tarred:
                if member.isfile() and wanted(member.name):
                    member_file = tarred.extractfile(member)
                    if member_file is None:  # pragma: no cover - isfile() members extract
                        break
                    with closing(member_file), target.open("wb") as dst:
                        shutil.copyfileobj(member_file, dst, _CHUNK)
                    return
    raise MongodBinaryError(f"{archive.name} has no bin/mongod executable")


def download_mongod(
    version: str,
    dest_dir: Path,
    *,
    urls: list[str] | None = None,
    timeout_s: float = 60.0,
) -> Path:
    """Download MongoDB ``version`` for this platform, verify the published SHA-256 and extract
    only ``mongod`` into ``dest_dir`` (written atomically). The archive is deleted afterwards."""
    candidates = urls if urls is not None else archive_urls(version)
    if not candidates:
        raise MongodBinaryError(
            f"no official MongoDB {version} archive is known for {platform.system()} "
            f"{platform.machine()}; set settings.launch.binary to an existing mongod"
        )
    for url in candidates:
        _check_official(url)
    tried: list[str] = []
    for url in candidates:
        expected = _published_sha256(url, timeout_s)
        if expected is None:
            tried.append(url)
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        archive = dest_dir / f".download-{os.getpid()}.part"
        target = dest_dir / executable_name()
        partial = dest_dir / f".{executable_name()}-{os.getpid()}.part"
        _log.info("mongod.download", extra={"url": url, "dest": str(dest_dir)})
        digest = hashlib.sha256()
        try:
            with _open_url(url, timeout_s) as response, archive.open("wb") as out:
                while chunk := response.read(_CHUNK):
                    digest.update(chunk)
                    out.write(chunk)
            actual = digest.hexdigest()
            if actual != expected:
                raise MongodChecksumError(
                    f"{url}: SHA-256 {actual} does not match the published {expected}; the "
                    "archive was deleted and nothing was extracted"
                )
            _extract_mongod(archive, partial)
            if platform.system() != "Windows":
                partial.chmod(0o755)
            os.replace(partial, target)
        finally:
            archive.unlink(missing_ok=True)
            partial.unlink(missing_ok=True)
        return target
    raise MongodBinaryError(
        f"MongoDB {version} has no official archive for this platform (tried "
        f"{', '.join(tried)}); check settings.launch.version or set settings.launch.binary"
    )


# --------------------------------------------------------------------------------- admin
class MongodAdmin(Protocol):
    """The few admin commands the launcher needs (tests provide a fake)."""

    def hello(self) -> dict[str, Any]: ...

    def initiated(self) -> bool: ...

    def initiate(self, replica_set: str, host: str) -> None: ...

    def shutdown(self) -> None: ...

    def close(self) -> None: ...


class _PymongoAdmin:
    """A short-timeout synchronous direct connection to ``127.0.0.1:<port>``."""

    _NOT_YET_INITIALIZED = 94
    _ALREADY_INITIALIZED = 23

    def __init__(self, port: int, timeout_s: float) -> None:
        try:
            from pymongo import MongoClient
        except ImportError as exc:
            raise MissingServiceError("storage:mongodb", extra="mongodb") from exc
        ms = int(timeout_s * 1000)
        self._client: MongoClient[dict[str, Any]] = MongoClient(
            "127.0.0.1",
            port,
            directConnection=True,
            serverSelectionTimeoutMS=ms,
            connectTimeoutMS=ms,
            socketTimeoutMS=max(ms, 10_000),
            appname="memspine-mongod-launcher",
        )

    def _command(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        from pymongo.errors import AutoReconnect, ServerSelectionTimeoutError

        try:
            return cast(dict[str, Any], self._client.admin.command(*args, **kwargs))
        except (ServerSelectionTimeoutError, AutoReconnect) as exc:
            raise MongodUnreachableError(str(exc)) from exc

    def hello(self) -> dict[str, Any]:
        return self._command("hello")

    def initiated(self) -> bool:
        from pymongo.errors import OperationFailure

        try:
            self._command("replSetGetStatus")
        except OperationFailure as exc:
            if exc.code == self._NOT_YET_INITIALIZED:
                return False
            raise
        return True

    def initiate(self, replica_set: str, host: str) -> None:
        from pymongo.errors import OperationFailure

        config = {"_id": replica_set, "members": [{"_id": 0, "host": host}]}
        try:
            self._command("replSetInitiate", config)
        except OperationFailure as exc:
            if exc.code != self._ALREADY_INITIALIZED:
                raise

    def shutdown(self) -> None:
        from pymongo.errors import ConnectionFailure, OperationFailure

        for force in (False, True):
            try:
                self._client.admin.command("shutdown", 1, force=force)
            except ConnectionFailure:
                return  # the server closes the connection as it goes down
            except OperationFailure:
                if force:
                    raise
                continue
            return

    def close(self) -> None:
        self._client.close()


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(_PORT_PROBE_S)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _print_notice_once() -> None:
    global _notice_printed
    with _notice_lock:
        if _notice_printed:
            return
        _notice_printed = True
    print(SSPL_NOTICE, file=sys.stderr)


# ------------------------------------------------------------------------------ launcher
class MongodLauncher:
    """Owns one local ``mongod`` (or adopts a matching one). ``handle`` is its URL."""

    kind: ClassVar[str] = "mongod"
    #: a process is not bound to an event loop: Engines on any loop in this process share it
    scope: ClassVar[Scope] = "thread"
    #: the admin connection factory (port, timeout_s); tests replace it
    admin_factory: ClassVar[Callable[[int, float], MongodAdmin]] = _PymongoAdmin

    def __init__(self, spec: MongodSpec) -> None:
        self._spec = spec
        self._process: subprocess.Popen[bytes] | None = None
        self._adopted = False
        self._opened = False
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- identity
    @classmethod
    def identity(cls, spec: MongodSpec) -> ConnectorIdentity:
        """Keyed by (binary, port, data_dir) (ADR-083 section 4); the replica set is a knob, so
        a second name on one port meets the running server as a port conflict."""
        target = {
            "binary": _path_identity(spec.binary_path()),
            "port": spec.port,
            "data_dir": _path_identity(Path(spec.data_dir)),
        }
        return ConnectorIdentity(
            cls.kind,
            target,
            None,
            {"replica_set": spec.replica_set},
            {"kind": "mongod", "host": "127.0.0.1", "port": spec.port},
        )

    @classmethod
    def locality(cls, spec: MongodSpec) -> Locality:
        return "host"

    @property
    def spec(self) -> MongodSpec:
        return self._spec

    @property
    def adopted(self) -> bool:
        """True when an already running ``mongod`` was adopted (it is never stopped)."""
        return self._adopted

    @property
    def process(self) -> subprocess.Popen[bytes] | None:
        return self._process

    @property
    def handle(self) -> str:
        if not self._opened:
            raise StorageError("MongodLauncher is not open: call open() first")
        return self._spec.url

    # ---------------------------------------------------------------- lifecycle
    async def open(self) -> None:
        if self._opened:
            return
        await asyncio.to_thread(self._open_sync)

    def _open_sync(self) -> None:
        with self._lock:
            if self._opened:
                return
            _print_notice_once()
            if _port_in_use(self._spec.port):
                self._adopt()
            else:
                binary = self._resolve_binary()
                self._start(binary)
            self._opened = True

    def _resolve_binary(self) -> Path:
        spec = self._spec
        path = spec.binary_path()
        if spec.binary is not None:
            if not path.is_file():
                raise MongodBinaryError(
                    f"settings.launch.binary {str(path)!r} is not a file; point it at an "
                    "existing mongod executable"
                )
            self._check_version(path)
            return path
        if path.is_file():
            return path
        if not spec.download:
            raise MongodBinaryError(
                f"no mongod {spec.version} in the cache ({path}); set settings.launch.download: "
                "true to download MongoDB Community Server (SSPL) from "
                f"{DOWNLOAD_HOST}, or settings.launch.binary to an existing mongod"
            )
        return download_mongod(spec.version, path.parent)

    def _check_version(self, binary: Path) -> None:
        try:
            done = subprocess.run(
                [*_command_prefix(binary), "--version"],
                capture_output=True,
                timeout=max(self._spec.start_timeout_s, 10.0),
                check=False,
                **_no_window(),
            )
        except OSError as exc:
            raise MongodBinaryError(
                f"settings.launch.binary {str(binary)!r} cannot run: {exc}"
            ) from exc
        if done.returncode in VCRUNTIME_EXIT_CODES:
            raise MongodStartError(_vcruntime_message(done.returncode))
        match = _VERSION_RE.search(done.stdout.decode("utf-8", "replace"))
        found = match.group(1) if match else None
        if found != self._spec.version:
            raise MongodBinaryError(
                f"settings.launch.binary {str(binary)!r} is mongod {found or 'of unknown version'}"
                f", but settings.launch.version pins {self._spec.version}"
            )

    def _argv(self, binary: Path, data_dir: Path) -> list[str]:
        spec = self._spec
        argv = [
            *_command_prefix(binary),
            "--replSet",
            spec.replica_set,
            "--bind_ip",
            "127.0.0.1",
            "--port",
            str(spec.port),
            "--dbpath",
            str(data_dir),
            "--logpath",
            str(data_dir / "mongod.log"),
            "--logappend",
        ]
        if spec.cache_size_gb is not None:
            argv += ["--wiredTigerCacheSizeGB", f"{spec.cache_size_gb:g}"]
        return argv

    def _start(self, binary: Path) -> None:
        spec = self._spec
        data_dir = Path(spec.data_dir).expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)
        console = data_dir / "mongod.console.log"
        _log.info("mongod.start", extra={"port": spec.port, "dbpath": str(data_dir)})
        with console.open("ab") as out:
            try:
                self._process = subprocess.Popen(
                    self._argv(binary, data_dir),
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    **_no_window(),
                )
            except OSError as exc:
                raise MongodStartError(f"cannot run {str(binary)!r}: {exc}") from exc
        try:
            self._wait_primary(initiate=True)
        except BaseException:
            self._terminate()
            raise

    def _adopt(self) -> None:
        spec = self._spec
        admin = self._admin()
        try:
            hello = admin.hello()
        except Exception as exc:
            raise MongodPortConflictError(
                f"port {spec.port} on 127.0.0.1 is in use by a process that does not answer "
                "as mongod; choose another settings.launch.port or stop that process"
            ) from exc
        finally:
            admin.close()
        found = hello.get("setName")
        if found != spec.replica_set:
            what = f"replica set {found!r}" if found else "no replica set (standalone)"
            raise MongodPortConflictError(
                f"port {spec.port} on 127.0.0.1 is in use by a mongod with {what}, not "
                f"{spec.replica_set!r}; choose another settings.launch.port or stop that server"
            )
        self._adopted = True
        _log.info("mongod.adopt", extra={"port": spec.port, "replica_set": found})
        self._wait_primary(initiate=False)

    def _admin(self) -> MongodAdmin:
        return type(self).admin_factory(self._spec.port, _ADMIN_TIMEOUT_S)

    def _wait_primary(self, *, initiate: bool) -> None:
        spec = self._spec
        deadline = time.monotonic() + spec.start_timeout_s
        admin: MongodAdmin | None = None
        checked = not initiate
        try:
            while True:
                process = self._process
                if process is not None:
                    code = process.poll()
                    if code is not None:
                        raise MongodStartError(self._exit_message(code))
                if time.monotonic() > deadline:
                    raise MongodStartError(
                        f"mongod on port {spec.port} did not become PRIMARY within "
                        f"settings.launch.start_timeout_s={spec.start_timeout_s:g}"
                        f"{self._log_tail()}"
                    )
                try:
                    if admin is None:
                        admin = self._admin()
                    if not checked:
                        if not admin.initiated():
                            admin.initiate(spec.replica_set, f"127.0.0.1:{spec.port}")
                        checked = True
                    if admin.hello().get("isWritablePrimary"):
                        return
                except MongodUnreachableError:
                    pass
                time.sleep(_POLL_S)
        finally:
            if admin is not None:
                admin.close()

    def _exit_message(self, code: int) -> str:
        if code in VCRUNTIME_EXIT_CODES:
            return _vcruntime_message(code)
        return (
            f"mongod on port {self._spec.port} exited with code {code} before becoming "
            f"PRIMARY{self._log_tail()}"
        )

    def _log_tail(self) -> str:
        data_dir = Path(self._spec.data_dir).expanduser()
        lines: list[str] = []
        for name in ("mongod.console.log", "mongod.log"):
            try:
                text = (data_dir / name).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines += text.splitlines()[-_LOG_TAIL_LINES:]
        if not lines:
            return ""
        return f"; log {data_dir / 'mongod.log'} ends with:\n" + "\n".join(lines)

    async def health(self, deadline_s: float = 5.0) -> bool:
        """A real ``hello`` round trip: the server is a writable primary."""
        if not self._opened:
            return False

        def check() -> bool:
            admin = self._admin()
            try:
                return bool(admin.hello().get("isWritablePrimary"))
            finally:
                admin.close()

        async def run() -> bool:
            return await asyncio.to_thread(check)

        return await probe(deadline_s, run)

    async def close(self) -> None:
        """Stop the owned server with ``shutdown`` (terminate if it hangs); an adopted server
        is left running. Idempotent."""
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        with self._lock:
            self._opened = False
            process, self._process = self._process, None
            if process is None or process.poll() is not None:
                return
            _log.info("mongod.shutdown", extra={"port": self._spec.port})
            try:
                admin = self._admin()
                try:
                    admin.shutdown()
                finally:
                    admin.close()
            except Exception:  # fall through to terminate
                _log.warning("mongod.shutdown_failed", exc_info=True)
            try:
                process.wait(self._spec.shutdown_timeout_s)
            except subprocess.TimeoutExpired:
                self._process = process
                self._terminate()

    def _terminate(self) -> None:
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(_TERMINATE_GRACE_S)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(_TERMINATE_GRACE_S)


def _vcruntime_message(code: int) -> str:
    return (
        f"mongod exited with code {code & 0xFFFFFFFF:#010x} (STATUS_DLL_NOT_FOUND): the "
        "Microsoft Visual C++ Redistributable (x64) runtime is missing on this machine. Install "
        "it (vc_redist.x64.exe from Microsoft; it needs admin rights) and start again"
    )


def _command_prefix(binary: Path) -> list[str]:
    """The argv that runs ``binary`` (tests replace this seam with a fake mongod script)."""
    return [str(binary)]


def _no_window() -> dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}
