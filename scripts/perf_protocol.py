"""PA-051 / ADR-096: the reference-host timing protocol shared by the wave-6 benchmarks.

Every performance record written through :func:`write_record` carries:

* ``revision``: commit, tree, branch and dirty state (the ADR-087 style of
  ``scripts/record_verification_run.py``). A dirty tree is refused unless the caller passes
  ``allow_dirty``; such a record is marked ``diagnostic_only``. The record files of the
  run itself (``ignore`` paths) do not count as dirt, so several families can be recorded
  on one clean commit;
* ``host``: CPU model, physical cores and logical threads, RAM, OS build, the active power
  plan (``powercfg /getactivescheme`` on Windows), Python, and the versions of the packages
  that matter for the measurement;
* ``load``: host CPU% sampled before and after the measurement with the top processes, so
  a reader can tell whether the host was quiet;
* per measurement, :func:`summarize` statistics: n, warm-ups, p50/p95/p99 (nearest rank),
  mean, stdev, min and max, in milliseconds.

Stdlib plus ``psutil`` (optional: without it the load block says so). No network.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = "PA-051/ADR-096 reference-host protocol v1"
DEFAULT_PACKAGES = (
    "memspine",
    "onnxruntime",
    "tokenizers",
    "huggingface_hub",
    "numpy",
    "lancedb",
    "tantivy",
    "sqlalchemy",
    "aiosqlite",
    "psutil",
)


# ----------------------------------------------------------------------------- statistics


def percentile(samples: Sequence[float], q: float) -> float:
    """Nearest-rank percentile (``q`` in 0..100) of a non-empty sample."""
    if not samples:
        raise ValueError("percentile of an empty sample")
    if not 0 <= q <= 100:
        raise ValueError("q must be within 0..100")
    ordered = sorted(samples)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def summarize(samples_ms: Sequence[float], *, warmups: int = 0) -> dict[str, Any]:
    """n, warm-ups, p50/p95/p99 (nearest rank), mean, stdev, min, max; values in ms."""
    if not samples_ms:
        return {"n": 0, "warmups": warmups}
    return {
        "n": len(samples_ms),
        "warmups": warmups,
        "p50_ms": round(percentile(samples_ms, 50), 6),
        "p95_ms": round(percentile(samples_ms, 95), 6),
        "p99_ms": round(percentile(samples_ms, 99), 6),
        "mean_ms": round(statistics.fmean(samples_ms), 6),
        "stdev_ms": round(statistics.stdev(samples_ms), 6) if len(samples_ms) > 1 else 0.0,
        "min_ms": round(min(samples_ms), 6),
        "max_ms": round(max(samples_ms), 6),
    }


def measure(fn: Callable[[], object], *, reps: int, warmups: int) -> list[float]:
    """Run ``fn`` ``warmups`` times untimed, then ``reps`` times; per-call wall time in ms."""
    if reps < 1 or warmups < 0:
        raise ValueError("reps must be >= 1 and warmups >= 0")
    for _ in range(warmups):
        fn()
    out: list[float] = []
    for _ in range(reps):
        start = time.perf_counter()
        fn()
        out.append((time.perf_counter() - start) * 1000.0)
    return out


async def measure_async(fn: Callable[[], Any], *, reps: int, warmups: int) -> list[float]:
    """:func:`measure` for a coroutine function."""
    if reps < 1 or warmups < 0:
        raise ValueError("reps must be >= 1 and warmups >= 0")
    for _ in range(warmups):
        await fn()
    out: list[float] = []
    for _ in range(reps):
        start = time.perf_counter()
        await fn()
        out.append((time.perf_counter() - start) * 1000.0)
    return out


# ----------------------------------------------------------------------------- revision


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def revision(root: Path = ROOT, ignore: Iterable[str | Path] = ()) -> dict[str, Any]:
    """Commit, tree, branch and dirty state; ``ignore`` paths (the run's own records) and
    anything below an ignored directory do not count as dirt."""
    ignored: list[str] = []
    for item in ignore:
        path = Path(item)
        resolved = (path if path.is_absolute() else root / path).resolve()
        with contextlib.suppress(ValueError):
            ignored.append(resolved.relative_to(root.resolve()).as_posix())

    def is_ignored(rel: str) -> bool:
        return any(rel == p or rel.startswith(p.rstrip("/") + "/") for p in ignored)

    changes = []
    for line in _git(root, "status", "--porcelain", "--untracked-files=all").splitlines():
        if not line.strip():
            continue
        rel = line[3:].split(" -> ")[-1].strip().strip('"')
        if not is_ignored(rel):
            changes.append(rel)
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    return {
        "commit": _git(root, "rev-parse", "HEAD"),
        "tree": _git(root, "rev-parse", "HEAD^{tree}"),
        "branch": None if branch == "HEAD" else branch,
        "dirty": bool(changes),
        "dirty_paths": changes[:20],
    }


class DirtyTreeError(RuntimeError):
    """The working tree has changes outside the run's own records (ADR-087)."""


# ----------------------------------------------------------------------------- host


def _run_text(cmd: Sequence[str], timeout: float = 15.0) -> str | None:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _cpu_model() -> str:
    if sys.platform == "win32":
        out = _run_text(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_Processor | Select-Object -First 1).Name",
            ]
        )
        if out:
            return out.strip()
    return platform.processor() or platform.machine()


def _power_plan() -> str | None:
    if sys.platform != "win32":
        return None
    return _run_text(["powercfg", "/getactivescheme"])


def _version(package: str) -> str | None:
    try:
        return version(package)
    except PackageNotFoundError:
        return None


def host_snapshot(packages: Sequence[str] = DEFAULT_PACKAGES) -> dict[str, Any]:
    """Host specs for the PA-051 record (no network)."""
    physical: int | None = None
    ram_bytes: int | None = None
    try:
        import psutil

        physical = psutil.cpu_count(logical=False)
        ram_bytes = int(psutil.virtual_memory().total)
    except ImportError:
        pass
    return {
        "cpu_model": _cpu_model(),
        "physical_cores": physical,
        "logical_threads": os.cpu_count(),
        "ram_gib": None if ram_bytes is None else round(ram_bytes / 2**30, 1),
        "os": platform.platform(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "power_plan": _power_plan(),
        "python": sys.version.split()[0],
        "python_build": sys.version,
        "executable": sys.executable,
        "packages": {name: _version(name) for name in packages},
    }


def load_sample(seconds: float = 2.0, top: int = 8) -> dict[str, Any]:
    """Host CPU% over ``seconds`` plus the ``top`` processes by CPU% over the same window."""
    try:
        import psutil
    except ImportError:
        return {"available": False, "reason": "psutil not installed"}
    procs = []
    for proc in psutil.process_iter(["pid", "name"]):
        with contextlib.suppress(psutil.Error):
            proc.cpu_percent(None)
            procs.append(proc)
    total = psutil.cpu_percent(interval=seconds)
    rows = []
    for proc in procs:
        with contextlib.suppress(psutil.Error):
            pct = proc.cpu_percent(None)
            if proc.info["pid"] == 0:  # the Idle pseudo-process
                continue
            rows.append({"pid": proc.info["pid"], "name": proc.info["name"], "cpu_percent": pct})
    rows.sort(key=lambda row: row["cpu_percent"], reverse=True)
    mem = psutil.virtual_memory()
    return {
        "available": True,
        "at_utc": datetime.now(UTC).isoformat(),
        "window_s": seconds,
        "cpu_percent_total": total,
        "memory_used_percent": mem.percent,
        "top_processes": rows[:top],
        "note": "process cpu_percent is per one logical CPU (100 = one full thread)",
    }


# ----------------------------------------------------------------------------- record


class Recorder:
    """One measurement family's record: start/finish load samples, results, write."""

    def __init__(
        self,
        family: str,
        out: Path,
        *,
        command: str | None = None,
        allow_dirty: bool = False,
        ignore: Iterable[str | Path] = (),
        root: Path = ROOT,
        settings: dict[str, Any] | None = None,
    ) -> None:
        self.out = out
        self.root = root
        ignored = [out, *ignore]
        rev = revision(root, ignored)
        if rev["dirty"] and not allow_dirty:
            raise DirtyTreeError(
                "working tree is dirty (" + ", ".join(rev["dirty_paths"]) + "); commit first "
                "or pass --allow-dirty for a diagnostic-only record"
            )
        self.record: dict[str, Any] = {
            "family": family,
            "protocol": PROTOCOL,
            "command": command if command is not None else " ".join(sys.argv),
            "started_at_utc": datetime.now(UTC).isoformat(),
            "revision": rev,
            "diagnostic_only": bool(rev["dirty"]),
            "host": host_snapshot(),
            "load_before": load_sample(),
            "settings": settings or {},
            "results": {},
            "notes": [],
        }
        self._t0 = time.perf_counter()

    def note(self, text: str) -> None:
        self.record["notes"].append(text)

    def finish(self, status: str = "completed") -> dict[str, Any]:
        self.record["load_after"] = load_sample()
        self.record["status"] = status
        self.record["finished_at_utc"] = datetime.now(UTC).isoformat()
        self.record["wall_s"] = round(time.perf_counter() - self._t0, 3)
        write_record(self.out, self.record)
        return self.record


def write_record(out: Path, record: dict[str, Any]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2, sort_keys=False, default=str) + "\n", "utf-8")


# ----------------------------------------------------------------------------- corpus

_PEOPLE = (
    "Alice", "Rowan", "Mira", "Jun", "Nadia", "Kai", "Irene", "Ravi", "Elena", "Tomas",
    "Priya", "Owen", "Sofia", "Malik", "Hana", "Lucas", "Amara", "Felix", "Yuki", "Dmitri",
)  # fmt: skip
_PLACES = (
    "Lisbon", "the Cedar room", "Kyoto", "the north office", "Berlin", "the lake house",
    "Toronto", "the community garden", "Nairobi", "the downtown gym", "Oslo", "the library",
)  # fmt: skip
_ACTIVITIES = (
    "planning the quarterly budget review", "training for a half marathon",
    "moving into a new apartment", "learning to bake sourdough", "fixing the release pipeline",
    "preparing the board presentation", "repainting the spare bedroom",
    "organising a birthday dinner", "studying for the Spanish exam",
    "migrating the billing database", "adopting a rescue dog", "booking flights for the wedding",
)  # fmt: skip
_TEMPLATES = (
    "I met {p} at {l} yesterday and we spent most of the afternoon {a}.",
    "{p} told me they started {a} last week, and it is going better than expected.",
    "Reminder for later: {p} wants an update on {a} before Friday.",
    "We decided to move the meeting with {p} to {l} because the usual room was booked.",
    "Honestly I am a bit worried about {a}; {p} thinks we are behind schedule.",
    "My sister and {p} are both in {l} this month, so dinner plans are on hold.",
    "Today I finally finished {a}. {p} helped with the hardest part.",
    "{p} prefers short written notes after calls, especially about {a}.",
    "Next Tuesday I fly to {l} for three days; {p} will cover {a} while I am away.",
    "The doctor said my knee is fine, so I can keep {a} as long as I stretch first.",
    # first-person statements the base template's deterministic rules can derive facts from
    "I live in {l} now, and {p} visits on weekends.",
    "I work at Northwind Labs, mostly on {a}.",
    "I really enjoy {a}, even when {p} teases me about it.",
    "I am planning to visit {l} in the spring with {p}.",
)  # fmt: skip
#: The sender every corpus turn carries (rules derive user facts only for an identified
#: human sender).
SENDER = {"id": "user-sam", "kind": "human", "name": "Sam"}
_QUERIES = (
    "What is {p} working on?",
    "When did I meet {p} at {l}?",
    "Who is helping with {a}?",
    "What did {p} say about {a}?",
    "Where am I travelling next week?",
    "Is anyone worried about {a}?",
    "What should I tell {p} before Friday?",
    "Which people are in {l} this month?",
)  # fmt: skip


def corpus_messages(n: int, seed: int = 7) -> list[str]:
    """``n`` invented, varied first-person chat turns (about 15-30 words each)."""
    import random

    rng = random.Random(seed)
    return [
        rng.choice(_TEMPLATES).format(
            p=rng.choice(_PEOPLE), l=rng.choice(_PLACES), a=rng.choice(_ACTIVITIES)
        )
        for _ in range(n)
    ]


def corpus_queries(n: int, seed: int = 11) -> list[str]:
    """``n`` invented questions over the :func:`corpus_messages` vocabulary."""
    import random

    rng = random.Random(seed)
    return [
        rng.choice(_QUERIES).format(
            p=rng.choice(_PEOPLE), l=rng.choice(_PLACES), a=rng.choice(_ACTIVITIES)
        )
        for _ in range(n)
    ]
