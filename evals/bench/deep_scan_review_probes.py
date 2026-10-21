"""Isolated review probes: execute real pure/method code without booting MemSpine.

These report isolated current behaviour; regression tests cover the fixes.
No database, service, model or generative endpoint is started.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import importlib.util
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]


def method(path, class_name, name, globals_):
    source = (ROOT / path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    owner = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    node = next(n for n in owner.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
    body = [
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        node,
    ]
    exec(
        compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), path, "exec"),
        globals_,
    )
    return globals_[name]


async def queue_probe():
    class BackpressureRejected(Exception):
        pass

    persist = method(
        "src/memspine/workers/windows.py",
        "WindowPipeline",
        "_persist_staged",
        {
            "BackpressureRejected": BackpressureRejected,
            "WINDOW_STAGE": "window",
            "asyncio": asyncio,
        },
    )

    class Outbox:
        calls = 0

        async def enqueue(self, *args, **kwargs):
            self.calls += 1
            raise RuntimeError("simulated database failure before enqueue")

    outbox = Outbox()
    fake = SimpleNamespace(
        _outbox=outbox,
        _staged=[("ns", {"id": "w1"}), ("ns", {"id": "w2"})],
        _persist_lock=asyncio.Lock(),
        _scope=lambda ns: ns,
        _async=False,
        _stopped=False,
    )
    before = len(fake._staged)
    try:
        await persist(fake)
    except RuntimeError:
        pass
    return {
        "before": before,
        "after": len(fake._staged),
        "enqueue_calls": outbox.calls,
        "gap_reproduced": len(fake._staged) == 0,
    }


async def wait_probe():
    wait = method(
        "src/memspine/runtime/runner.py",
        "InlineRunner",
        "wait",
        {
            "timedelta": timedelta,
            "tap": SimpleNamespace(event=lambda *a, **k: None),
            "asyncio": asyncio,
        },
    )
    fake = SimpleNamespace(
        clock=lambda: datetime.now(UTC),
        store=SimpleNamespace(last_offset=lambda *a: 1),
        _drain_lock=asyncio.Lock(),
        _stopping=False,
        finished=False,
        _sleep=asyncio.sleep,
    )
    fake._barrier = lambda *a: (
        SimpleNamespace(
            status="ready" if fake.finished else "partial",
            missing=() if fake.finished else ("window",),
            holes={},
        ),
        not fake.finished,
    )

    async def drain(*args):
        await asyncio.sleep(0.06)
        fake.finished = True

    fake.drain = drain
    started = time.perf_counter()
    await wait(fake, "scope", ["window"], timeout=0.002)
    elapsed = time.perf_counter() - started
    return {
        "requested_seconds": 0.002,
        "simulated_drain_seconds": 0.06,
        "elapsed_seconds": elapsed,
        "gap_reproduced": elapsed > 0.04,
        "boundary": "Low-level wait; read.PendingBarrier has a separate asyncio.wait_for guard",
    }


def identity_probes():
    path = ROOT / "src/memspine/ingest/message_identity.py"
    spec = importlib.util.spec_from_file_location("identity_review_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def normalise(message):
        return module.normalise_messages(
            [message], namespace="n", now=datetime(2026, 10, 21, tzinfo=UTC)
        )[0]

    civil = normalise(
        {"message_id": "m1", "role": "user", "content": "A message", "created_at": "2026-10-01"}
    )
    a = normalise({"id": "provided-id", "role": "user", "content": "A message"})
    b = normalise({"id": "provided-id", "role": "user", "content": "A message"})
    return {
        "source_time": {
            "input": "2026-10-01",
            "accepted_created_at": civil["created_at"],
            "classification": "Contract gap; conversion also has an explicit naive-time assumption policy",
        },
        "unknown_input_key": {
            "unknown_id_accepted": a["id"] == "provided-id",
            "canonical_id_different": a["message_id"] != "provided-id",
            "second_normalisation_new_id": a["message_id"] != b["message_id"],
            "classification": "id is not canonical; reject unknown fields rather than add aliases",
        },
    }


def rule_probe():
    path = ROOT / "src/memspine/core/rule_miner.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    tree.body = [
        n
        for n in tree.body
        if not (isinstance(n, ast.ImportFrom) and n.module == "memspine.prompts.models")
    ]
    module = ModuleType("review_rule_miner")
    module.ExtractedFact = SimpleNamespace
    sys.modules[module.__name__] = module
    exec(compile(ast.fix_missing_locations(tree), str(path), "exec"), module.__dict__)
    cases = {
        "explicit": "[1] [2026-10-01] Asha: I live in Lisbon.",
        "conditional": "[2] [2026-10-01] Asha: If I live in Lisbon, I will need a new job.",
        "reported_speech": '[3] [2026-10-01] Asha: Daniel said, "I live in Porto."',
    }
    return {
        "method": "Actual rule parser; only output container stubbed, no Engine persistence",
        "cases": {key: [vars(f) for f in module.mine_rules(text)] for key, text in cases.items()},
        "interpretation": "Inspect conditional/reported candidates against explicit control; downstream integration not exercised",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    result = {
        "date": "2026-10-21",
        "scope": "Isolated current-code methods; not Engine/DB integration or full suite",
        "queue_failure": asyncio.run(queue_probe()),
        "barrier_timeout": asyncio.run(wait_probe()),
        "rule_candidates": rule_probe(),
        **identity_probes(),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
