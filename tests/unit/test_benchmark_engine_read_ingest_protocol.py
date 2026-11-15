"""Model-free checks for the PA-053 repeated-run evidence contract."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from scripts import benchmark_engine_read_ingest as benchmark

from memspine.config.loader import load_config


def _row(rate: float = 1.0) -> dict[str, Any]:
    return {
        "target_msgs_per_s": rate,
        "achieved_msgs_per_s": rate,
        "achieved_msgs_per_s_exact": rate,
        "messages": 10,
        "acked_messages": 10,
        "saturated": False,
        "messages_unscheduled_at_step_cap": 0,
        "calls_cancelled_at_step_cap": 0,
        "write_tasks_unresolved": 0,
        "excluded_messages": 0,
        "tail_complete": True,
        "lag_censored": 0,
        "failed_readiness": 0,
        "unavailable_readiness": 0,
        "error_count": 0,
        "read_timeouts": 0,
        "poll_timeouts": 0,
        "read_tasks_cancelled": 0,
        "unresolved_awaits": 0,
        "warmup_failures": 0,
        "write_ack_ms": {"p95_ms": 12.0},
        "read_ms": {"p95_ms": 20.0},
        "derivation_lag_ms": {"p95_ms": 30.0},
    }


def test_budget_gate_and_independent_dispersion() -> None:
    a = _row()
    b = _row()
    b["read_ms"] = {"p95_ms": 30.0}
    dispersion = benchmark._between_repetition([a, b])
    assert dispersion["independent_repetitions"] == 2
    assert dispersion["read_p95_ms"]["mean"] == 25.0
    assert dispersion["read_p95_ms"]["stdev"] > 0
    assert benchmark._complete_at_target(a)
    assert not benchmark._eligible_repetition(
        {"seed": {"size": 7, "requested_size": 10, "truncated_by_time_budget": True}, "metrics": a}
    )
    for change in (
        {"saturated": True},
        {"lag_censored": 1},
        {"failed_readiness": 1},
        {"achieved_msgs_per_s_exact": 0.99},
        {"messages_unscheduled_at_step_cap": 1},
        {"excluded_messages": 1},
        {"read_timeouts": 1},
        {"poll_timeouts": 1},
    ):
        assert not benchmark._complete_at_target({**a, **change})


def test_budget_requires_clean_controlled_repeated_current_default() -> None:
    base: dict[str, Any] = dict(
        complete_at_target=True,
        arm="current-default",
        repetitions=3,
        controlled_host=True,
        diagnostic_only=False,
    )
    assert benchmark._proposal_eligible(**base)
    for change in (
        {"repetitions": 1},
        {"repetitions": 2},
        {"controlled_host": False},
        {"diagnostic_only": True},
        {"arm": "off"},
        {"complete_at_target": False},
    ):
        assert not benchmark._proposal_eligible(**{**base, **change})


def test_config_redacts_credentials_and_urls() -> None:
    raw = {
        "write": {"window_max_turns": 20},
        "model": {
            "settings": {
                "api_key": "secret",
                "password": "pass",
                "endpoint": "https://user:pass@example.test/a?token=secret",
                "connection_string": "host=db user=alice password=secret",
                "dsn": "host=db user=bob password=secret",
            }
        },
    }
    safe = benchmark._redact_config(raw)
    assert safe["write"]["window_max_turns"] == 20
    assert safe["model"]["settings"]["api_key"] == "<redacted>"
    assert safe["model"]["settings"]["password"] == "<redacted>"
    assert safe["model"]["settings"]["endpoint"] == "<redacted-url>"
    assert safe["model"]["settings"]["connection_string"] == "<redacted-connection>"
    assert safe["model"]["settings"]["dsn"] == "<redacted-connection>"
    assert "secret" not in str(safe)


def test_numeric_auth_token_is_not_mistaken_for_token_budget() -> None:
    safe = benchmark._redact_config({"api_token": 123456, "max_tokens": 2048, "token_budget": 4096})
    assert safe["api_token"] == "<redacted>"
    assert safe["max_tokens"] == 2048
    assert safe["token_budget"] == 4096


@pytest.mark.asyncio
async def test_seed_counts_only_retained_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Engine:
        async def ingest_messages(self, _msgs: Any, **_kwargs: Any) -> list[Any]:
            return []

        async def flush_windows(self, _namespace: str, **_kwargs: Any) -> dict[str, Any]:
            return {}

        async def stop(self) -> None:
            pass

    async def fake_open(_data: Path, *, rerank: bool | None) -> tuple[Engine, float]:
        return Engine(), 1.0

    monkeypatch.setattr(benchmark, "_open", fake_open)
    info = await benchmark.seed(tmp_path, 2, name="excluded-seed", reuse=False)
    assert info["requested_size"] == 2
    assert info["size"] == 0
    assert info["submitted_messages"] == 2
    assert info["dropped_messages"] == 2
    assert info["time_cap_reached"] is False
    assert info["seed_incomplete"] is True
    assert info["ingest_call_progression"][0]["retained_messages_before"] == 0
    assert info["ingest_call_progression"][0]["submitted_messages_before"] == 0
    assert info["truncated_by_time_budget"] is False


@pytest.mark.asyncio
async def test_seed_marker_reuse_requires_same_protocol_and_complete_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    starts: list[int] = []

    class Engine:
        async def ingest_messages(self, msgs: list[Any], **_kwargs: Any) -> list[Any]:
            return [type("Record", (), {"record_id": str(i)})() for i, _ in enumerate(msgs)]

        async def flush_windows(self, _namespace: str, **_kwargs: Any) -> dict[str, Any]:
            return {}

        async def stop(self) -> None:
            pass

    async def fake_open(_data: Path, *, rerank: bool | None) -> tuple[Engine, float]:
        starts.append(1)
        return Engine(), 1.0

    monkeypatch.setattr(benchmark, "_open", fake_open)
    first = await benchmark.seed(tmp_path, 2, name="marker", reuse=True)
    assert first["size"] == first["submitted_messages"] == 2
    second = await benchmark.seed(tmp_path, 2, name="marker", reuse=True)
    assert second["reused"] is True
    assert len(starts) == 1
    marker = tmp_path / "marker" / "seed.json"
    stale = json.loads(marker.read_text("utf-8"))
    stale["seed_identity"]["version"] = 1
    marker.write_text(json.dumps(stale), "utf-8")
    third = await benchmark.seed(tmp_path, 2, name="marker", reuse=True)
    assert third["reused"] is False
    assert len(starts) == 2
    monkeypatch.setenv("MEMSPINE_WRITE__WINDOW_IDLE_S", "45")
    fourth = await benchmark.seed(tmp_path, 2, name="marker", reuse=True)
    assert fourth["reused"] is False
    assert len(starts) == 3
    monkeypatch.setattr(
        benchmark,
        "_engine_source_identity",
        lambda: {"commit": "changed", "content_sha256": "changed"},
    )
    fifth = await benchmark.seed(tmp_path, 2, name="marker", reuse=True)
    assert fifth["reused"] is False
    assert len(starts) == 4


@pytest.mark.asyncio
async def test_seed_refuses_escape_and_work_root_before_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    deleted: list[Path] = []
    monkeypatch.setattr(shutil, "rmtree", lambda p: deleted.append(p))
    for name in ("../victim", ".", str(victim)):
        with pytest.raises(ValueError, match="seed path"):
            await benchmark.seed(work, 1, name=name)
    assert not deleted
    assert victim.is_dir()


@pytest.mark.asyncio
async def test_excluded_write_is_not_acknowledged() -> None:
    class Engine:
        async def ingest_messages(self, _msgs: Any, **_kwargs: Any) -> list[Any]:
            return []

    row = await benchmark.run_rate(cast(Any, Engine()), "single_1", 1.0, 1.0, 1, 0, 1, 0.1, 2.0)
    assert row["messages"] == 1
    assert row["acked_messages"] == 0
    assert row["excluded_messages"] == 1
    assert row["lag_tracked"] == 0
    assert not benchmark._complete_at_target(row)


@pytest.mark.asyncio
async def test_hung_read_and_poll_are_bounded_and_censored() -> None:
    never = asyncio.Event()

    class Engine:
        async def ingest_messages(self, msgs: list[dict[str, Any]], **_kwargs: Any) -> list[Any]:
            source = type("Source", (), {"message": {"message_id": msgs[0]["message_id"]}})()
            return [type("Record", (), {"record_id": "record-1", "source": source})()]

        async def read(self, _query: str, **_kwargs: Any) -> None:
            await never.wait()

        async def wait_for_derivation(self, _ids: list[str], **_kwargs: Any) -> None:
            await never.wait()

    row = await asyncio.wait_for(
        benchmark.run_rate(cast(Any, Engine()), "single_1", 100.0, 0.01, 1, 1, 1, 0.1, 0.2),
        timeout=2.0,
    )
    assert row["read_timeouts"] or row["read_tasks_cancelled"]
    assert row["poll_timeouts"] or not row["tail_complete"]
    assert row["lag_censored"] == 1
    assert not benchmark._complete_at_target(row)


@pytest.mark.asyncio
async def test_bounded_await_reports_uncooperative_cancellation() -> None:
    finished = asyncio.Event()

    async def stubborn() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.4)
            finished.set()

    with pytest.raises(benchmark.UnresolvedEngineTask):
        await benchmark._bounded(stubborn(), 0.01)
    await asyncio.wait_for(finished.wait(), 1.0)


@pytest.mark.asyncio
async def test_outer_cancellation_drains_child_await() -> None:
    finished = asyncio.Event()

    async def waiting() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    outer = asyncio.create_task(benchmark._bounded(waiting(), 10.0))
    await asyncio.sleep(0)
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    assert finished.is_set()


@pytest.mark.asyncio
async def test_unresolved_warmup_aborts_before_next_repetition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[Path] = []
    read_calls: list[int] = []
    release = asyncio.Event()
    records: list[dict[str, Any]] = []

    class FakeRecorder:
        def __init__(self, _family: str, out: Path, **_kwargs: Any) -> None:
            self.out = out
            self.record: dict[str, Any] = {"results": {}, "diagnostic_only": False}

    class FakeEngine:
        def __init__(self, config: Any) -> None:
            self.config = config

        async def read(self, _query: str, **_kwargs: Any) -> None:
            read_calls.append(1)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()

        async def stop(self) -> None:
            pass

    async def fake_seed(_work: Path, _size: int, **_kwargs: Any) -> dict[str, Any]:
        return {"size": 1, "requested_size": 1, "truncated_by_time_budget": False}

    async def fake_open(data: Path, *, rerank: bool | None) -> tuple[FakeEngine, float]:
        opened.append(data)
        cfg = load_config(
            template="base", user_config=benchmark._config(data, rerank=rerank)
        ).config
        return FakeEngine(cfg), 1.0

    monkeypatch.setattr(benchmark, "Recorder", FakeRecorder)
    monkeypatch.setattr(benchmark, "seed", fake_seed)
    monkeypatch.setattr(benchmark, "_open", fake_open)
    monkeypatch.setattr(benchmark, "install_cut_probe", lambda _eng: True)
    monkeypatch.setattr(benchmark, "host_snapshot", lambda: {})
    monkeypatch.setattr(benchmark, "load_sample", lambda: {})
    monkeypatch.setattr(benchmark, "write_record", lambda _out, record: records.append(record))
    args = argparse.Namespace(
        modes=["single"],
        rates=[1.0],
        durations=[1.0],
        steps=None,
        concurrent_arm="current-default",
        family=None,
        suffix="",
        allow_dirty=True,
        seed_size=1,
        concurrency_repetitions=2,
        concurrent_read_warmups=1,
        controlled_host=True,
        readers=1,
        step_max_s=0.01,
        poll_ms=1,
        tail_s=0.1,
        max_seed_s=1.0,
        work_dir=str(tmp_path),
    )
    previous = asyncio.all_tasks()
    try:
        with pytest.raises(benchmark.UnresolvedEngineTask):
            await benchmark.part_concurrent(args)
        assert len(opened) == 1
        assert read_calls == [1]
        assert records[-1]["status"] == "aborted_incomplete"
        assert "proposed_budgets" not in str(records[-1])
    finally:
        release.set()
        owned = asyncio.all_tasks() - previous
        if owned:
            done, pending = await asyncio.wait(owned, timeout=1.0)
            assert done and not pending


@pytest.mark.parametrize("partial_json", [False, True])
def test_supervisor_kills_owned_uncooperative_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, partial_json: bool
) -> None:
    out = tmp_path / "incomplete.json"
    worker = tmp_path / "stubborn.py"
    payload = (
        '{"status": "aborted_incomplete",'
        if partial_json
        else json.dumps(
            {
                "status": "aborted_incomplete",
                "results": {"single_1": {"proposed_budgets": {"read_p95_ms": 1}}},
            }
        )
    )
    worker.write_text(
        "import asyncio, pathlib, sys\n"
        "async def stubborn():\n"
        "    while True:\n"
        "        try: await asyncio.Event().wait()\n"
        "        except asyncio.CancelledError: continue\n"
        "async def main():\n"
        "    task = asyncio.create_task(stubborn())\n"
        "    await asyncio.sleep(0.05)\n"
        "    task.cancel()\n"
        f"    pathlib.Path(sys.argv[1]).write_text({payload!r})\n"
        "asyncio.run(main())\n",
        "utf-8",
    )
    created: list[subprocess.Popen[Any]] = []
    original = subprocess.Popen

    def tracked_popen(cmd: list[str]) -> subprocess.Popen[Any]:
        proc = original(cmd)
        created.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", tracked_popen)
    with pytest.raises(RuntimeError, match="incomplete record"):
        benchmark._run_isolated_command(
            [sys.executable, str(worker), str(out)], out, 1.0 if partial_json else 3.0
        )
    assert len(created) == 1
    assert created[0].poll() is not None
    record = json.loads(out.read_text("utf-8"))
    assert record["status"] == "aborted_incomplete"
    if partial_json:
        assert record["partial_record_unreadable"] is True
        assert record["results"] == {}
        assert "hard deadline" in record["abort_reason"]
    else:
        assert record["results"]["single_1"]["proposal_eligible"] is False
        assert "proposed_budgets" not in record["results"]["single_1"]


@pytest.mark.parametrize(
    ("arm", "rerank_flag", "onnx_model_count"),
    [("current-default", None, 2), ("off", False, 1), ("on", True, 2)],
)
@pytest.mark.parametrize("seed_truncated", [False, True])
@pytest.mark.asyncio
async def test_concurrent_repetitions_have_fresh_paths_and_resolved_arm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arm: str,
    rerank_flag: bool | None,
    onnx_model_count: int,
    seed_truncated: bool,
) -> None:
    records: list[dict[str, Any]] = []
    opens: list[tuple[Path, bool | None]] = []
    seeds: list[Path] = []

    class FakeRecorder:
        def __init__(self, _family: str, out: Path, **_kwargs: Any) -> None:
            self.out = out
            self.record: dict[str, Any] = {
                "settings": _kwargs["settings"],
                "results": {},
                "diagnostic_only": False,
            }
            records.append(self.record)

        def finish(self) -> None:
            self.record["status"] = "completed"

    class FakeEngine:
        def __init__(self, config: Any) -> None:
            self.config = config

        async def stop(self) -> None:
            pass

        async def read(self, _query: str, **_kwargs: Any) -> None:
            pass

    async def fake_seed(work: Path, _size: int, **kwargs: Any) -> dict[str, Any]:
        seeds.append(work / kwargs["name"])
        return {
            "size": 7 if seed_truncated else 10,
            "requested_size": 10,
            "truncated_by_time_budget": seed_truncated,
        }

    async def fake_open(data: Path, *, rerank: bool | None) -> tuple[FakeEngine, float]:
        opens.append((data, rerank))
        cfg = load_config(
            template="base", user_config=benchmark._config(data, rerank=rerank)
        ).config
        return FakeEngine(cfg), 1.0

    async def fake_rate(_eng: Any, _label: str, rate: float, *_args: Any) -> dict[str, Any]:
        return _row(rate)

    monkeypatch.setattr(benchmark, "Recorder", FakeRecorder)
    monkeypatch.setattr(benchmark, "seed", fake_seed)
    monkeypatch.setattr(benchmark, "_open", fake_open)
    monkeypatch.setattr(benchmark, "run_rate", fake_rate)
    monkeypatch.setattr(benchmark, "install_cut_probe", lambda _eng: True)
    monkeypatch.setattr(benchmark, "host_snapshot", lambda: {"power_plan": "test"})
    monkeypatch.setattr(benchmark, "load_sample", lambda: {"cpu_percent_total": 0})
    monkeypatch.setattr(benchmark, "write_record", lambda _out, _record: None)
    args = argparse.Namespace(
        modes=["single"],
        rates=[1.0, 10.0],
        durations=[1.0, 1.0],
        steps=None,
        concurrent_arm=arm,
        family=None,
        suffix="",
        allow_dirty=True,
        seed_size=10,
        concurrency_repetitions=3,
        concurrent_read_warmups=2,
        controlled_host=True,
        readers=2,
        step_max_s=2.0,
        poll_ms=100,
        tail_s=1.0,
        max_seed_s=1.0,
        work_dir=str(tmp_path),
    )
    await benchmark.part_concurrent(args)
    assert len(seeds) == len(set(seeds)) == 6
    assert [path for path, _ in opens] == seeds
    assert all(rerank is rerank_flag for _, rerank in opens)
    initial = 7 if seed_truncated else 10
    assert (
        records[0]["results"]["single_1"]["repetitions"][0]["namespace_messages_before"] == initial
    )
    assert (
        records[0]["results"]["single_10"]["repetitions"][1]["namespace_messages_before"] == initial
    )
    assert records[0]["results"]["single_1"]["complete_at_target"] is not seed_truncated
    if seed_truncated or arm != "current-default":
        assert "proposed_budgets" not in records[0]["results"]["single_1"]
    else:
        assert records[0]["results"]["single_1"]["proposed_budgets"][
            "read_p95_ms"
        ] == benchmark._budget(20.0)
    models = records[0]["results"]["single_1"]["repetitions"][0]["effective_config"]["onnx_models"]
    assert len(models) == onnx_model_count
    if onnx_model_count == 2:
        assert models[1]["model"] == "Xenova/ms-marco-MiniLM-L-6-v2"
    assert records[0]["results"]["single_1"]["repetitions"][0]["metrics"]["read_warmups"] == 2
    assert "2026-11-02" not in records[0]["settings"]["execution_at_utc"]


@pytest.mark.parametrize(
    "bad_args",
    [
        ["--concurrency-repetitions", "0"],
        ["--rates", "1", "10", "--durations", "1"],
        ["--rates", "0"],
        ["--durations", "0"],
        ["--readers", "0"],
    ],
)
def test_cli_rejects_invalid_workload_before_creating_workdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_args: list[str]
) -> None:
    work = tmp_path / "never-created"
    monkeypatch.setattr(
        sys, "argv", ["benchmark", "--part", "concurrent", "--work-dir", str(work), *bad_args]
    )
    with pytest.raises(SystemExit) as exc:
        benchmark.main()
    assert exc.value.code == 2
    assert not work.exists()


@pytest.mark.asyncio
async def test_failed_facts_readiness_is_censored() -> None:
    class Result:
        timed_out = False
        readiness = type("Readiness", (), {"facts": "failed"})()

    class Engine:
        async def ingest_messages(self, msgs: list[dict[str, Any]], **_kwargs: Any) -> list[Any]:
            source = type("Source", (), {"message": {"message_id": msgs[0]["message_id"]}})()
            return [type("Record", (), {"record_id": "record-1", "source": source})()]

        async def wait_for_derivation(self, ids: list[str], **_kwargs: Any) -> dict[str, Any]:
            return {mid: Result() for mid in ids}

    row = await benchmark.run_rate(cast(Any, Engine()), "single_1", 1.0, 1.0, 1, 0, 1, 0.1, 2.0)
    assert row["derived"] == 0
    assert row["failed_readiness"] == 1
    assert row["lag_censored"] == 1
    assert not benchmark._complete_at_target(row)
