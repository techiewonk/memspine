"""W1: the harness runs the window write (call 1) before reads and at build; logs its counters."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from memspine_evals.systems.memspine_system import MemspineSystem


class _FakeEngine:
    def __init__(self) -> None:
        self.calls = 0
        self.flushed: list[tuple[str | None, bool]] = []

    def model_calls(self) -> dict[str, int]:
        return {"window_extract": self.calls}

    async def flush_windows(self, namespace: str | None = None, *, final: bool = True) -> dict:
        self.flushed.append((namespace, final))
        self.calls += 4 if final else 1
        return {"windows": 4, "calls": self.calls, "parse_failures": 0, "facts_ungrounded": 2}


def _system(window: bool) -> MemspineSystem:
    system = MemspineSystem(config={"write": {"window_extract": "facts"}} if window else {})
    system._engine = _FakeEngine()
    return system


def test_build_flushes_the_windows_and_bills_their_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMSPINE_FORENSICS_DIR", str(tmp_path))
    system = _system(window=True)
    system._run_id = "r1"
    result = asyncio.run(system.build())
    assert system._engine.flushed == [(system.namespace, True)]  # type: ignore[union-attr]
    assert result.model_calls == 4
    assert result.meta["window_extract"]["facts_ungrounded"] == 2
    rows = [
        json.loads(line)
        for line in (tmp_path / "ingest.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    [row] = [r for r in rows if r.get("kind") == "window_stats"]
    assert row["windows"] == 4 and row["final"] is True and row["run_id"] == "r1"


def test_no_window_arm_does_not_touch_the_engine() -> None:
    system = _system(window=False)
    assert asyncio.run(system._flush_windows(final=True)) is None
    assert system._engine.flushed == []  # type: ignore[union-attr]
    assert asyncio.run(system.build()).model_calls == 0


def test_mid_stream_flush_leaves_the_open_window() -> None:
    system = _system(window=True)
    result: Any = asyncio.run(system._flush_windows(final=False))
    assert system._engine.flushed == [(system.namespace, False)]  # type: ignore[union-attr]
    assert result.model_calls == 1
