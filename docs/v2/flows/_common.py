"""Shared harness for the per-flow manual checks (``python docs/v2/flow_checks.py ...``).

Every flow module defines ``STEPS``: an ordered list of ``Step(id, title, fn)``. ``fn(ctx)`` is async,
gets a ``Ctx`` (a fresh in-memory engine plus helpers) and calls ``ctx.show(...)`` / ``ctx.expect(...)``.
The runner prints, per step: what it does, the values it got, PASS/FAIL against the expected value,
and the wiretap events the step produced (``--events``) or every engine source line it executed
(``--lines``). CPU only: in-memory SQLite, hash embedder, no model, no network. Invented names only.
"""

from __future__ import annotations

import json
import linecache
import logging
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logging.disable(logging.INFO)
try:
    import structlog

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
except ImportError:  # pragma: no cover
    pass

SRC = Path(__file__).resolve().parents[3] / "src" / "memspine"


@dataclass
class Step:
    id: str
    title: str
    fn: Callable[[Ctx], Awaitable[None]]
    code: str = ""  # where the behaviour lives (file:function), printed with the step


@dataclass
class Ctx:
    """One flow run: the engine, the namespace, and the step's results."""

    engine: Any = None
    ns: str = "flowcheck"
    state: dict[str, Any] = field(
        default_factory=dict
    )  # values a later step reuses (record ids, ...)
    results: list[tuple[str, bool]] = field(default_factory=list)
    step_id: str = ""

    def show(self, label: str, value: Any, n: int = 400) -> None:
        text = value if isinstance(value, str) else _fmt(value)
        print(f"    {label}: {text[:n]}{' ...' if len(text) > n else ''}")

    def expect(self, what: str, expected: Any, got: Any) -> bool:
        ok = expected == got
        self.results.append((self.step_id, ok))
        print(f"    [{'PASS' if ok else 'FAIL'}] {what}: expected {expected!r}, got {got!r}")
        return ok

    def check(self, what: str, cond: bool, detail: Any = "") -> bool:
        self.results.append((self.step_id, bool(cond)))
        print(f"    [{'PASS' if cond else 'FAIL'}] {what}{f' ({detail})' if detail != '' else ''}")
        return bool(cond)

    @property
    def storage(self) -> Any:
        return self.engine._require_started()


def _fmt(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=True)
    try:
        return json.dumps(value, default=str, ensure_ascii=False)
    except TypeError:
        return repr(value)


async def make_engine(config: dict[str, Any] | None = None, template: str = "core") -> Any:
    """A fresh in-memory engine with the wiretap on (memory sink, content kept)."""
    from memspine import Engine

    cfg: dict[str, Any] = {
        "storage": {
            "provider": "sqlite",
            "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
        },
        "embedding": {"provider": "hash", "settings": {}},
        "memories": {"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        "observability": {"tap": {"enabled": True, "sink": "memory", "content": True}},
    }
    for key, val in (config or {}).items():
        if isinstance(val, dict) and isinstance(cfg.get(key), dict):
            cfg[key] = {**cfg[key], **val}
        else:
            cfg[key] = val
    eng = Engine(template=template, dotenv_path=None, **cfg)
    await eng.start()
    return eng


def _events_since(mark: int) -> tuple[list[dict[str, Any]], int]:
    from memspine.observability import tap

    traces = tap.memory_traces()
    rows: list[dict[str, Any]] = []

    def walk(t: dict[str, Any]) -> None:
        for e in t.get("events", []):
            rows.append({"event": e.get("stage"), **(e.get("fields") or {})})
        for child in t.get("children", []):
            walk(child)

    for t in traces[mark:]:
        walk(t)
    return rows, len(traces)


class _LineTracer:
    """Prints every executed line of ``src/memspine`` (``--lines``), indented by call depth."""

    def __init__(self, only: str | None, limit: int) -> None:
        self.only = only
        self.limit = limit
        self.count = 0
        self.root = SRC.as_posix().lower()

    def __call__(self, frame: Any, event: str, arg: Any) -> Any:
        path = frame.f_code.co_filename
        norm = path.replace("\\", "/").lower()
        if not norm.startswith(self.root) or (self.only and self.only.lower() not in norm):
            return None if event == "call" else self
        if event == "line" and self.count < self.limit:
            self.count += 1
            rel = Path(path).relative_to(SRC.parent).as_posix()
            line = linecache.getline(path, frame.f_lineno).rstrip()
            print(f"      {rel}:{frame.f_lineno} {frame.f_code.co_name}: {line.strip()}")
        return self


async def run_steps(
    name: str,
    steps: Sequence[Step],
    pick: Sequence[str],
    *,
    events: bool,
    lines: bool,
    only: str | None,
    limit: int,
    config: dict[str, Any] | None = None,
) -> list[tuple[str, bool]]:
    """Run the flow's steps in order on ONE engine (later steps build on earlier ones); print the
    picked steps (all when ``pick`` is empty). Unpicked earlier steps still run, quietly, so a single
    step can be inspected with its preconditions in place."""
    import contextlib
    import io

    ctx = Ctx(engine=await make_engine(config))
    mark = 0
    last = max((i for i, s in enumerate(steps) if not pick or s.id in pick), default=-1)
    try:
        for s in steps[: last + 1]:
            shown = not pick or s.id in pick
            ctx.step_id = s.id
            _, mark = _events_since(mark)
            if not shown:
                with contextlib.redirect_stdout(io.StringIO()):
                    await s.fn(ctx)
                continue
            print(f"\n[{name} {s.id}] {s.title}" + (f"\n    code: {s.code}" if s.code else ""))
            tracer = _LineTracer(only, limit) if lines else None
            if tracer:
                sys.settrace(tracer)
            try:
                await s.fn(ctx)
            except Exception as exc:  # report and continue with the next step
                ctx.results.append((s.id, False))
                print(f"    [ERROR] {type(exc).__name__}: {exc}")
            finally:
                if tracer:
                    sys.settrace(None)
                    if tracer.count >= limit:
                        print(f"      ... line limit {limit} reached (--limit N)")
            if events:
                rows, mark = _events_since(mark)
                print(f"    events ({len(rows)}):")
                for r in rows[:60]:
                    ev = r.pop("event", "")
                    print(f"      {ev}  {_fmt(r)[:220]}")
                if len(rows) > 60:
                    print(f"      ... {len(rows) - 60} more")
    finally:
        await ctx.engine.stop()
    return [r for r in ctx.results if not pick or r[0] in pick]
