"""The RM-31 summary must never convert missing pool evidence into a pass."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import check_remote_provider_resource_soak as soak
import rm31_owned_services as owned
from check_remote_provider_resource_soak import MongoPoolEvents, slope, summarize


def sample(*, phase: str, outstanding: int | None, error: str | None = None) -> dict[str, object]:
    return {
        "phase": phase,
        "error_type": error,
        "telemetry_available": outstanding is not None,
        "owned_outstanding_after_settle": outstanding,
        "rss_after_stop": 100,
        "handles_after_stop": 10,
    }


def test_summary_requires_complete_telemetry_and_zero_owned_connections() -> None:
    complete = summarize(
        [sample(phase="warmup", outstanding=0), sample(phase="measured", outstanding=0)]
    )
    assert complete["owned_outstanding_zero_after_settle"] is True
    missing = summarize(
        [sample(phase="warmup", outstanding=0), sample(phase="measured", outstanding=None)]
    )
    assert missing["owned_outstanding_zero_after_settle"] is None
    leaking = summarize([sample(phase="measured", outstanding=1)])
    assert leaking["owned_outstanding_zero_after_settle"] is False


def test_summary_records_failures_and_does_not_count_failed_cycle() -> None:
    result = summarize([sample(phase="measured", outstanding=None, error="TimeoutError")])
    assert result["measured_completed"] == 0
    assert result["failure_types"] == ["TimeoutError"]
    assert result["owned_outstanding_zero_after_settle"] is None


def test_mongo_listener_tracks_checkout_checkin_and_physical_close() -> None:
    events = MongoPoolEvents()
    event = SimpleNamespace(connection_id=7)
    events.connection_created(event)
    events.connection_checked_out(event)
    assert events.sample()["checked_out"] == 1
    events.connection_checked_in(event)
    events.connection_closed(event)
    assert events.sample()["checked_out"] == 0
    assert events.sample()["open_created_connections"] == 0


def test_slope_requires_all_samples() -> None:
    assert slope([1, 2, 3]) == 1
    assert slope([1, None, 3]) is None


def test_worker_holds_exclusive_lock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    entered: list[bool] = []

    @contextmanager
    def guard() -> Iterator[None]:
        entered.append(True)
        yield

    monkeypatch.setattr(soak, "exclusive_run", guard)
    monkeypatch.setattr(soak, "load_state", lambda: {"password": "ignored"})
    monkeypatch.setattr(soak, "_run_locked", lambda data, out, state: 0)
    monkeypatch.setattr(soak.isolated_services, "new_state", lambda: {"run_id": "a" * 12})
    monkeypatch.setattr(soak.isolated_services, "save", lambda state: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["check_remote_provider_resource_soak.py", "--worker", "--out", str(tmp_path / "out.json")],
    )
    assert soak.main() == 0
    assert entered == [True]


def _lock_holder_script(ready: Path) -> str:
    return (
        "import sys, time\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(Path(soak.__file__).parent)!r})\n"
        "from check_remote_provider_resource_soak import exclusive_run\n"
        "with exclusive_run():\n"
        f"    Path({str(ready)!r}).write_text('ready', encoding='utf-8')\n"
        "    time.sleep(10)\n"
    )


def _wait_for_lock_release() -> None:
    deadline = time.monotonic() + 5
    while True:
        try:
            with soak.exclusive_run():
                return
        except OSError:
            if time.monotonic() >= deadline:
                pytest.fail("OS lock remained held after worker termination")
            time.sleep(0.02)


def test_os_lock_stays_with_measuring_process_until_exit(tmp_path: Path) -> None:
    ready = tmp_path / "worker-ready"
    child = subprocess.Popen(
        [sys.executable, "-c", _lock_holder_script(ready)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), (
            child.stderr.read()
            if child.poll() is not None and child.stderr
            else "worker did not enter lock"
        )
        with pytest.raises(OSError), soak.exclusive_run():
            pass
    finally:
        child.kill()
        child.wait(timeout=5)
    _wait_for_lock_release()


def test_parent_timeout_kills_owned_lock_holder(tmp_path: Path) -> None:
    ready = tmp_path / "timeout-worker-ready"
    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run(
            [sys.executable, "-c", _lock_holder_script(ready)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    assert ready.exists()
    _wait_for_lock_release()


def _owned_inspect(state: dict[str, object], service: str, container_id: str) -> dict[str, object]:
    return {
        "Id": container_id,
        "Name": f"/{state['project']}-{service}",
        "Config": {
            "Image": owned.PINS[service],
            "Labels": {
                owned.LABEL_OWNER: state["owner"],
                owned.LABEL_PROJECT: state["project"],
            },
        },
    }


def test_rm31_cleanup_requires_exact_id_and_both_owner_labels(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(owned, "STATE_DIR", tmp_path)
    state = owned.new_state()
    container_id = "a" * 64
    cidfile = tmp_path / f"{state['project']}-opensearch.cid"
    cidfile.write_text(container_id, encoding="utf-8")
    state["containers"]["opensearch"] = {"id": container_id, "cidfile": str(cidfile)}
    owned.save(state)
    inspected = _owned_inspect(state, "opensearch", container_id)
    removed: list[list[str]] = []
    monkeypatch.setattr(owned, "_inspect", lambda identity: inspected)
    monkeypatch.setattr(
        owned,
        "docker",
        lambda args, check=True: removed.append(args) or SimpleNamespace(returncode=0),
    )
    inspected["Config"]["Labels"][owned.LABEL_OWNER] = "wrong"
    with pytest.raises(RuntimeError, match="ownership mismatch"):
        owned.cleanup(state)
    assert removed == []
    inspected["Config"]["Labels"][owned.LABEL_OWNER] = state["owner"]
    assert owned.cleanup(state) == {"opensearch": "removed"}
    assert removed == [["rm", "--force", "--volumes", container_id]]


def test_rm31_start_rejects_name_collision_and_non_loopback_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(owned, "STATE_DIR", tmp_path)
    state = owned.new_state()
    owned.save(state)
    calls: list[str] = []
    monkeypatch.setattr(
        owned, "_inspect", lambda identity: calls.append(identity) or {"Id": "a" * 64}
    )
    with pytest.raises(RuntimeError, match="already exists"):
        owned.start(state, "opensearch", deadline=time.monotonic() + 1)
    assert calls == [f"{state['project']}-opensearch"]
    with pytest.raises(RuntimeError, match="loopback"):
        owned._published_port(
            {
                "NetworkSettings": {
                    "Ports": {"9200/tcp": [{"HostIp": "0.0.0.0", "HostPort": "1234"}]}
                }
            },
            9200,
        )


def test_rm31_inspect_failure_is_not_treated_as_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        owned,
        "docker",
        lambda args, check=True: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="Cannot connect to the Docker daemon",
        ),
    )
    with pytest.raises(RuntimeError, match="Docker inspect"):
        owned._inspect("a" * 64)


def test_rm31_inspect_absence_requires_successful_exact_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def inspect_failed(args: list[str], *, check: bool = True) -> SimpleNamespace:
        calls.append(args)
        return SimpleNamespace(returncode=1 if args[0] == "inspect" else 0, stdout="")

    monkeypatch.setattr(owned, "docker", inspect_failed)
    assert owned._inspect("a" * 64) is None
    assert calls == [
        ["inspect", "a" * 64],
        ["ps", "-a", "--no-trunc", "--filter", f"id={'a' * 64}", "--format", "{{.ID}}"],
    ]


def test_rm31_refuses_uncached_pin_without_creating_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(owned, "STATE_DIR", tmp_path)
    state = owned.new_state()
    owned.save(state)
    monkeypatch.setattr(owned, "_inspect", lambda identity: None)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        owned,
        "docker",
        lambda args, check=True: calls.append(args) or SimpleNamespace(returncode=1),
    )
    with pytest.raises(RuntimeError, match="not cached"):
        owned.start(state, "weaviate", deadline=time.monotonic() + 1)
    assert calls == [["image", "inspect", owned.PINS["weaviate"]]]


def test_neo4j_scratch_cleanup_runs_if_post_stop_telemetry_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import memspine

    label = "MemNode_" + "a" * 24
    dropped: list[str] = []

    class Graph:
        _ex = SimpleNamespace(_fmt={"N": label})

        async def upsert_node(self, *args: object, **kwargs: object) -> None:
            pass

        async def delete_node(self, *args: object, **kwargs: object) -> None:
            pass

    class FakeEngine:
        def __init__(self, **kwargs: object) -> None:
            self._graph = Graph()
            self._provider_resources = SimpleNamespace(
                states={"graph": SimpleNamespace(value="stopped")}
            )
            self.content = ""

        async def start(self) -> None:
            pass

        async def write(self, content: str, **kwargs: object) -> object:
            self.content = content
            return object()

        async def search(self, *args: object, **kwargs: object) -> list[tuple[object, float]]:
            return [(SimpleNamespace(content=self.content), 1.0)]

        async def stop(self) -> None:
            pass

    readings = iter(({"pooled_sockets": 1}, RuntimeError("lost native telemetry")))

    def probe() -> dict[str, int]:
        value = next(readings)
        if isinstance(value, Exception):
            raise value
        return value

    async def cleanup(value: str) -> None:
        dropped.append(value)

    monkeypatch.setattr(memspine, "Engine", FakeEngine)
    monkeypatch.setattr(soak, "_engine_config", lambda *args: {})
    monkeypatch.setattr(soak, "engine_native_probe", lambda *args: (probe, lambda value: 0))
    monkeypatch.setattr(soak, "_neo4j_drop_owned_schema", cleanup)
    result = asyncio.run(soak.engine_cycle("neo4j_graph", {}, {}, "abc123def456", 0, tmp_path))
    assert result["error_type"] == "telemetry_after_stop:RuntimeError"
    assert result["telemetry_available"] is False
    assert result["scratch_cleanup"] == {"neo4j_schema": "dropped"}
    assert dropped == [label]


def test_durable_records_cycles_reuse_one_owned_deployment_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(soak, "ports_for", lambda data: {"POSTGRES": 15432, "MONGO": 27018})
    for row in ("postgres_records", "mongo_records"):
        first = soak._engine_config(row, {"password": "redacted"}, {}, "abc123def456", 0, tmp_path)
        second = soak._engine_config(row, {"password": "redacted"}, {}, "abc123def456", 1, tmp_path)
        assert first["deployment"]["id"] == second["deployment"]["id"]
        assert first["deployment"]["data_dir"] == second["deployment"]["data_dir"]
        assert (
            first["storage"]["records"]["settings"]["url"]
            == second["storage"]["records"]["settings"]["url"]
        )
    sqlite_first = soak._engine_config("opensearch_lexical", {}, {}, "abc123def456", 0, tmp_path)
    sqlite_second = soak._engine_config("opensearch_lexical", {}, {}, "abc123def456", 1, tmp_path)
    assert sqlite_first["deployment"]["id"] != sqlite_second["deployment"]["id"]


def test_connector_closes_when_native_telemetry_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import memspine.clients.opensearch as opensearch

    closed: list[bool] = []

    class FakeClient:
        def __init__(self, spec: object) -> None:
            self.handle = SimpleNamespace(
                transport=SimpleNamespace(connection_pool=SimpleNamespace(connections=[]))
            )

        async def open(self) -> None:
            pass

        async def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(opensearch, "OpenSearchClient", FakeClient)
    with pytest.raises(RuntimeError, match="telemetry unavailable"):
        asyncio.run(soak.opensearch_connector_cycle())
    assert closed == [True]
