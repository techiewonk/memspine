"""CPU golden harness: a deterministic end-to-end engine run with no model.

Builds an engine with a fixed config (``core`` template, hash embedder, reranker
off, no LLM, temp storage), ingests ``fixtures/conversations.json``, runs the
model-free offline pass (``Engine.sleep``), runs the fixed query list and returns
a normalised snapshot (plain JSON types, stable ordering).

Normalisation: record ids are uuid4, so each id is replaced by a stable label
(``r001``...) assigned in ingest order, then by order of first appearance for
records created later (offline pass). Scores are rounded to six decimals. The
only clocks that matter are the fixture's explicit dates (``valid_from``); wall
clock fields (``recorded_at`` and the like) are never snapshotted.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from memspine import Engine

FIXTURE = Path(__file__).parent / "fixtures" / "conversations.json"
MEMORY_TYPES = ("episodic", "semantic", "working", "procedural", "associative", "reflective")
MODES = ("retrieve", "compose")
TOP_K = 5
BUDGET_TOKENS = 400


#: The wall clock the scoring, decay, read and offline code sees during a golden run. Scores
#: include a recency term of ``now - valid_from``, so without this they drift with the date.
FROZEN_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_CLOCK_MODULES = (
    "memspine.core.policies.scoring",
    "memspine.core.policies.decay",
    "memspine.api.engine",
    "memspine.read.pipeline.legs",
    "memspine.memory.pipeline.procedural",
    "memspine.memory.pipeline.prospective",
    "memspine.memory.pipeline.working",
    "memspine.derive.pipeline.integrate",
    "memspine.derive.pipeline.stages.communities",
    "memspine.derive.pipeline.stages.consolidate",
    "memspine.derive.pipeline.stages.entity_summaries",
    "memspine.derive.pipeline.stages.maintenance",
    "memspine.derive.pipeline.stages.mining",
    "memspine.derive.pipeline.stages.retention",
    "memspine.derive.pipeline.stages.watches",
)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
        return FROZEN_NOW if tz is None else FROZEN_NOW.astimezone(tz)


@contextmanager
def frozen_clock() -> Iterator[None]:
    """Replace the ``datetime`` name in the clock-reading modules; restored on exit."""
    saved: list[tuple[Any, Any]] = []
    for name in _CLOCK_MODULES:
        mod = importlib.import_module(name)
        saved.append((mod, mod.datetime))
        mod.datetime = _FrozenDatetime
    try:
        yield
    finally:
        for mod, original in saved:
            mod.datetime = original


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class _Labels:
    """Stable labels for uuid4 record ids: ingest order, then first appearance."""

    def __init__(self) -> None:
        self._by_id: dict[str, str] = {}

    def of(self, record_id: str) -> str:
        if record_id not in self._by_id:
            self._by_id[record_id] = f"r{len(self._by_id) + 1:03d}"
        return self._by_id[record_id]


def build_engine(storage_dir: Path) -> Engine:
    """The fixed golden configuration. Nothing here may reach a model or the network."""
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": str(storage_dir / "golden.db")},
        embedding={"provider": "hash"},
        read={"rerank": "off", "record_access": False},
        memories={
            "episodic": {"enabled": True},
            "semantic": {"enabled": True},
        },
    )


async def _counts(eng: Engine, namespaces: Iterable[str]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for ns in sorted(namespaces):
        per_layer: dict[str, int] = {}
        for mtype in MEMORY_TYPES:
            n = len(await eng.retrieve(namespace=ns, memory_type=mtype, include_held=True))
            if n:
                per_layer[mtype] = n
        out[ns] = per_layer
    return out


async def _ingest(eng: Engine, spec: dict[str, Any], labels: _Labels) -> None:
    for conv in spec["conversations"]:
        ns = conv["namespace"]
        for session in conv["sessions"]:
            written = await eng.write_messages(
                session["messages"],
                namespace=ns,
                session_id=session["session_id"],
                group_id=conv.get("group_id"),
                valid_from=_dt(session["date"]),
            )
            for rec in written:
                labels.of(rec.record_id)
        for fact in conv["facts"]:
            rec = await eng.write(
                fact["content"],
                namespace=ns,
                memory_type="semantic",
                entity=fact["entity"],
                attribute=fact["attribute"],
                valid_from=_dt(fact["valid_from"]),
            )
            labels.of(rec.record_id)


def _hit(record: Any, labels: _Labels, score: float | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": labels.of(record.record_id),
        "memory_type": record.memory_type,
        "status": getattr(record.status, "value", str(record.status)),
        "valid_from": record.valid_from.isoformat(),
        "content_sha256": _sha(record.content)[:16],
    }
    if score is not None:
        out["score"] = round(float(score), 6)
    return out


async def _query(eng: Engine, q: dict[str, Any], labels: _Labels) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"namespace": q["namespace"]}
    if "viewer" in q:
        kwargs["viewer"] = q["viewer"]
    hits = await eng.search(q["query"], top_k=TOP_K, **kwargs)
    result: dict[str, Any] = {
        "search": [_hit(rec, labels, score) for rec, score in hits],
    }
    read_kwargs = dict(kwargs)
    if "as_of" in q:
        read_kwargs["as_of"] = _dt(q["as_of"])
    for mode in MODES:
        read = await eng.read(
            q["query"], mode=mode, top_k=TOP_K, budget_tokens=BUDGET_TOKENS, **read_kwargs
        )
        ctx = read.context
        result[f"read_{mode}"] = {
            "mode": read.mode,
            "abstained": ctx.abstained,
            "tokens_used": ctx.tokens_used,
            "records": [_hit(rec, labels) for rec in ctx.records],
            "context_sha256": _sha("\n".join(rec.content for rec in ctx.records)),
        }
    return result


async def run(storage_dir: Path) -> dict[str, Any]:
    """Run the whole golden scenario in ``storage_dir`` and return the snapshot."""
    spec = json.loads(FIXTURE.read_text(encoding="utf-8"))
    namespaces = sorted({c["namespace"] for c in spec["conversations"]})
    labels = _Labels()
    eng = build_engine(storage_dir)
    with frozen_clock():
        return await _run(eng, spec, namespaces, labels)


async def _run(
    eng: Engine, spec: dict[str, Any], namespaces: list[str], labels: _Labels
) -> dict[str, Any]:
    await eng.start()
    try:
        await _ingest(eng, spec, labels)
        after_ingest = await _counts(eng, namespaces)
        sleep_stats = await eng.sleep()
        after_offline = await _counts(eng, namespaces)
        queries = {q["id"]: await _query(eng, q, labels) for q in spec["queries"]}
    finally:
        await eng.stop()
    return {
        "schema": 1,
        "counts_after_ingest": after_ingest,
        "counts_after_offline": after_offline,
        "offline_stages": {
            name: str(stage.get("status")) for name, stage in sorted(sleep_stats.items())
        },
        "queries": queries,
    }


def dumps(snapshot: dict[str, Any]) -> str:
    """Canonical text form: sorted keys, two-space indent, LF, trailing newline."""
    return json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
