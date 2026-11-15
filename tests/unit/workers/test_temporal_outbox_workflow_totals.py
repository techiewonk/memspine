"""Continue-as-new reporting with a deterministic Temporal workflow stand-in."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


class _ContinueAsNew(Exception):
    """The real Temporal call ends this generation instead of returning."""


def _workflow(
    monkeypatch: pytest.MonkeyPatch, results: list[dict[str, Any]]
) -> tuple[Any, list[tuple[str, object, dict[str, Any]]]]:
    calls: list[tuple[str, object, dict[str, Any]]] = []
    temporalio = ModuleType("temporalio")
    workflow = ModuleType("temporalio.workflow")
    common = ModuleType("temporalio.common")

    async def execute_activity(name: str, request: object, **kwargs: Any) -> dict[str, Any]:
        calls.append((name, request, kwargs))
        return results.pop(0)

    def continue_as_new(*, args: list[object]) -> None:
        """Match the SDK's keyword-only multi-argument API (Temporal 1.35)."""
        raise _ContinueAsNew(*args)

    workflow.defn = lambda **_kwargs: lambda cls: cls  # type: ignore[attr-defined]
    workflow.run = lambda fn: fn  # type: ignore[attr-defined]
    workflow.execute_activity = execute_activity  # type: ignore[attr-defined]
    workflow.continue_as_new = continue_as_new  # type: ignore[attr-defined]
    workflow.ActivityCancellationType = SimpleNamespace(  # type: ignore[attr-defined]
        WAIT_CANCELLATION_COMPLETED="wait"
    )
    common.RetryPolicy = lambda **kwargs: SimpleNamespace(**kwargs)  # type: ignore[attr-defined]
    temporalio.workflow = workflow  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "temporalio", temporalio)
    monkeypatch.setitem(sys.modules, "temporalio.workflow", workflow)
    monkeypatch.setitem(sys.modules, "temporalio.common", common)

    path = Path(__file__).resolve().parents[3] / "src/memspine/workers/temporal_workflow.py"
    spec = importlib.util.spec_from_file_location("_mocked_temporal_workflow", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.OutboxDrainWorkflow(), calls


def _request(*, max_steps: int = 2) -> dict[str, Any]:
    return {
        "tenant": "tenant",
        "owner": "owner",
        "owner_kind": "user",
        "stage": "derive",
        "max_items": 2,
        "activity_timeout_s": 30.0,
        "heartbeat_timeout_s": 5.0,
        "retry_max_attempts": 3,
        "max_steps": max_steps,
    }


async def test_totals_survive_multiple_bounded_continuations_and_terminal_empty_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results: list[dict[str, Any]] = [
        {"items": 2, "holes": 0, "watermark": 3, "more": True},
        {"items": 3, "holes": 1, "watermark": 7, "more": True},
        {"items": 1, "holes": 0, "watermark": None, "more": True},
        {"items": 4, "holes": 1, "watermark": 9, "more": True},
        {"items": 0, "holes": 0, "watermark": None, "more": False},
    ]
    instance, calls = _workflow(monkeypatch, results)
    request = _request()
    args: tuple[object, ...] = (request,)
    carried: list[dict[str, Any]] = []
    generation_steps: list[int] = []

    for _ in range(3):
        first_call = len(calls)
        try:
            totals = await instance.run(*args)
        except _ContinueAsNew as continuation:
            generation_steps.append(len(calls) - first_call)
            assert continuation.args[0] is request
            carried.append(continuation.args[1].copy())
            args = continuation.args
        else:
            generation_steps.append(len(calls) - first_call)
            break
    else:
        pytest.fail("the terminal empty batch did not finish the workflow")

    assert generation_steps == [2, 2, 1]
    assert carried == [
        {"steps": 2, "items": 5, "holes": 1, "watermark": 7},
        {"steps": 4, "items": 10, "holes": 2, "watermark": 9},
    ]
    assert totals == {"steps": 5, "items": 10, "holes": 2, "watermark": 9}
    assert results == []
    assert all(name == "memspine.outbox.drain_step" for name, _, _ in calls)
    assert all(activity_request is request for _, activity_request, _ in calls)
    assert all(options["result_type"] is dict for _, _, options in calls)
    assert all(options["retry_policy"].maximum_attempts == 3 for _, _, options in calls)


async def test_first_empty_batch_returns_one_step_and_its_watermark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, calls = _workflow(
        monkeypatch, [{"items": 0, "holes": 0, "watermark": -1, "more": False}]
    )
    request = _request(max_steps=1)

    assert await instance.run(request) == {
        "steps": 1,
        "items": 0,
        "holes": 0,
        "watermark": -1,
    }
    assert len(calls) == 1 and calls[0][1] is request
