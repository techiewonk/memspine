"""#14: with the graph features off, ``read()`` is byte-identical to the pre-change engine.

The snapshot in ``golden/graph_leg_off_read.json`` was recorded on the engine
before the entity layer and the graph read leg existed (commit d6dccc5), on the
fixed fixture below. The fixture turns associative memory on and writes edge
facts (``kind:``/``rel:``/``dst:`` tags), so the graph projection has
something to walk; with ``read.graph_leg`` and ``read.cards_include_edges``
off, every mode must still produce exactly the recorded contexts.

To accept an intended change, regenerate and review the diff::

    MEMSPINE_UPDATE_GOLDENS=1 pytest tests/unit/test_graph_leg_off_golden.py
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from memspine import Engine

GOLDEN = Path(__file__).parent / "golden" / "graph_leg_off_read.json"
_UPDATE = os.environ.get("MEMSPINE_UPDATE_GOLDENS") == "1"

T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)

TURNS = [
    "Melanie said she finished reading Charlotte's Web last night",
    "we chatted about the rain in Boston",
    "Melanie is now reading Nothing Is Impossible",
    "Caroline moved to Denver for a new job",
    "the weekend hike got cancelled",
    "Caroline adopted a dog named Biscuit",
]

#: (entity, rel, kind, dst, fact, turn index)
EDGES = [
    ("Melanie", "read", "event", "Charlotte's Web", 'Melanie read "Charlotte\'s Web"', 0),
    ("Melanie", "read", "event", "Nothing Is Impossible", 'Melanie read "Nothing Is Impossible"', 2),
    ("Caroline", "lives_in", "state", "Denver", "Caroline lives in Denver", 3),
    ("Caroline", "owns", "event", "Biscuit", "Caroline owns a dog named Biscuit", 5),
]

QUERIES = [
    "what books has Melanie read",
    "where does Caroline live",
    "tell me about Biscuit",
]


def _engine(**read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            "semantic": {"enabled": True},
            "episodic": {"enabled": True},
            "associative": {"enabled": True, "policies": {"entity_nodes": True}},
        },
        read={"record_access": False, **read},
    )


async def seed(eng: Engine, ns: str = "a") -> dict[str, str]:
    """The fixed fixture: six turns and four edge facts derived from them."""
    ids: dict[str, str] = {}
    turn_ids = []
    for i, text in enumerate(TURNS):
        rec = await eng.write(
            text, namespace=ns, memory_type="episodic", valid_from=T0 + timedelta(days=i)
        )
        turn_ids.append(rec.record_id)
        ids[f"turn{i}"] = rec.record_id
    for entity, rel, kind, dst, fact, turn in EDGES:
        rec = await eng.write(
            fact,
            namespace=ns,
            entity=entity,
            attribute=rel if kind == "state" else None,
            tags=[f"kind:{kind}", f"rel:{rel}", f"dst:{dst}"],
            derived_from=[turn_ids[turn]],
            valid_from=T0 + timedelta(days=turn, hours=1),
        )
        ids[fact] = rec.record_id
    return ids


async def _snapshot(eng: Engine) -> dict[str, Any]:
    await seed(eng)
    out: dict[str, Any] = {}
    for query in QUERIES:
        for mode in ("retrieve", "replay", "compose", "full"):
            result = await eng.read(query, namespace="a", mode=mode, top_k=4, budget_tokens=300)
            out[f"{mode}|{query}"] = {
                "mode": result.mode,
                "abstained": result.context.abstained,
                "tokens_used": result.context.tokens_used,
                "contents": [r.content for r in result.context.records],
            }
    return out


async def test_graph_off_read_matches_the_pre_change_golden() -> None:
    eng = _engine()
    await eng.start()
    try:
        current = await _snapshot(eng)
    finally:
        await eng.stop()
    if _UPDATE:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert current == expected


async def test_explicit_off_flags_match_the_golden() -> None:
    """The flags spelled out at their off values change nothing either."""
    if not _has_graph_flags():
        return
    eng = _engine(graph_leg=False, cards_include_edges=False)
    await eng.start()
    try:
        current = await _snapshot(eng)
    finally:
        await eng.stop()
    assert current == json.loads(GOLDEN.read_text(encoding="utf-8"))


def _has_graph_flags() -> bool:
    from memspine.config.schema import ReadConfig

    return "graph_leg" in ReadConfig.model_fields
