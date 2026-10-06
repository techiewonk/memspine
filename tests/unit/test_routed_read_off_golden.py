"""ADR-055 question-shape gates off: ``read()`` and ``assemble()`` are byte-identical.

``golden/routed_read_off.json`` was recorded on the engine before the replay-path
question-shape gates existed (``read.aggregate_in_replay``,
``read.list_cards_only_aggregate``, ``read.temporal_leg_event_dates``,
``read.cards_temporal``, ``read.profile_skip_temporal``, ``read.lead_budget_share``),
on the fixed fixture below: dated turns, mined facts (``atomic_fact``, some
``happened:``-tagged), a #30 list card and H14 profile insights. With every new key at
its default, every read mode and a direct ``assemble()`` must reproduce it exactly,
under the core defaults and under configs that switch the existing lead blocks on.

To accept an intended change, regenerate and review the diff::

    MEMSPINE_UPDATE_GOLDENS=1 pytest tests/unit/test_routed_read_off_golden.py
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from memspine import Engine
from memspine.config import constants
from memspine.workers.list_cards import ListCard

GOLDEN = Path(__file__).parent / "golden" / "routed_read_off.json"
_UPDATE = os.environ.get("MEMSPINE_UPDATE_GOLDENS") == "1"

T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)

#: (text, day offset)
TURNS = [
    ("Melanie: I went camping with the kids last weekend", 0),
    ("Caroline: I started a new painting of a lake sunset", 3),
    ("Melanie: we ran a charity race for mental health in May", 9),
    ("Melanie: I went camping at the beach again with the family", 20),
    ("Caroline: I joined a support group last Tuesday", 33),
    ("Melanie: the kids loved the pottery class yesterday", 40),
    ("Caroline: I am researching adoption agencies this month", 62),
]

#: (text, entity, parent turn indexes, day offset, happened, said)
MINED = [
    ("Melanie event: went camping with the kids", "Melanie", [0], 0, "2023-04-29", "2023-05-01"),
    ("Melanie event: ran a charity race for mental health", "Melanie", [2], 9, None, None),
    ("Melanie event: went camping at the beach", "Melanie", [3], 20, None, None),
    ("Caroline event: joined a support group", "Caroline", [4], 33, "2023-05-30", "2023-06-03"),
    ("Melanie event: took the kids to a pottery class", "Melanie", [5], 40, "2023-06-09", None),
]

#: (insight, parent turn indexes)
INSIGHTS = [
    ("Melanie loves camping and outdoor family trips", [0, 3]),
    ("Caroline is creative and is planning to adopt", [1, 6]),
]

QUERIES = [
    "What does Melanie like to do?",
    "What activities has Melanie done?",
    "How many times did Melanie go camping?",
    "When did Caroline join the support group?",
    "What did Melanie do in May 2023?",
]

#: Read configs covered: the defaults and the existing lead blocks switched on.
CONFIGS: dict[str, dict[str, Any]] = {
    "core-defaults": {},
    "lead-blocks": {
        "cards": "header",
        "cards_event_date": True,
        "profile_header": True,
        "count_timeline": True,
        "count_dedupe": True,
        "aggregate_top_k": 20,
    },
    "lead-blocks-skip-temporal": {
        "cards": "header",
        "cards_skip_temporal": True,
        "cards_event_date": True,
        "profile_header": True,
        "count_timeline": True,
        "temporal_leg": True,
        "aggregate_top_k": 20,
    },
    "packed-profile": {"cards": "header", "profile_header_packing": True},
}


@pytest.fixture(autouse=True)
def _fixed_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Record ids break score ties: make them a fixed sequence, so runs compare."""
    import uuid

    from memspine.core import records

    counter = iter(range(1, 1_000_000))
    monkeypatch.setattr(records, "uuid4", lambda: uuid.UUID(int=next(counter)))


async def seed(eng: Engine, ns: str = "a") -> None:
    """The fixed fixture: turns, mined facts, one list card, two profile insights."""
    ids = []
    for text, day in TURNS:
        rec = await eng.write(
            text, namespace=ns, memory_type="episodic", valid_from=T0 + timedelta(day)
        )
        ids.append(rec.record_id)
    facts = []
    for text, entity, parents, day, happened, said in MINED:
        fact = await eng._deposit_mined_fact(
            ns,
            text,
            entity,
            "event",
            [ids[i] for i in parents],
            T0 + timedelta(days=day, hours=2),
            "s1",
            kind="event",
            happened=happened,
            said=said,
            persons=[entity],
        )
        facts.append(fact)
    melanie = [f for f in facts if f.entity == "Melanie"][:3]
    await eng._deposit_list_card(
        ns,
        ListCard(
            person="Melanie",
            label="activities",
            text="Melanie — activities: camping (2023-05); charity race (2023-05)",
            parents=sorted(f.record_id for f in melanie),
            valid_from=max(f.valid_from for f in melanie),
            tags=[constants.LIST_CARD_TAG, "atomic_fact", "person:melanie", "topic:activity"],
        ),
        [],
    )
    for text, parents in INSIGHTS:
        await eng._deposit_profile_reflection(ns, text, [ids[i] for i in parents], "s1")


def _engine(read: dict[str, Any]) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            "semantic": {"enabled": True},
            "episodic": {"enabled": True},
            "reflective": {"enabled": True},
        },
        read={"record_access": False, **read},
    )


def _stable(text: str) -> str:
    """Profile insights are dated when reflected (today): mask that date."""
    return text.replace(datetime.now(UTC).date().isoformat(), "<today>")


async def snapshot(read: dict[str, Any]) -> dict[str, Any]:
    eng = _engine(read)
    await eng.start()
    out: dict[str, Any] = {}
    try:
        await seed(eng)
        for query in QUERIES:
            for mode in ("auto", "retrieve", "replay", "compose"):
                result = await eng.read(query, namespace="a", mode=mode, top_k=3, budget_tokens=400)
                out[f"read|{mode}|{query}"] = [
                    result.mode,
                    result.context.abstained,
                    result.context.tokens_used,
                    [
                        [r.memory_type, sorted(r.tags), _stable(r.content)]
                        for r in result.context.records
                    ],
                ]
            ctx = await eng.assemble(query, namespace="a", top_k=3, budget_tokens=400)
            out[f"assemble|{query}"] = [ctx.tokens_used, [_stable(r.content) for r in ctx.records]]
    finally:
        await eng.stop()
    return out


@pytest.mark.parametrize("name", sorted(CONFIGS))
async def test_new_gates_at_defaults_match_the_golden(name: str) -> None:
    current = await snapshot(CONFIGS[name])
    if _UPDATE:
        golden = json.loads(GOLDEN.read_text(encoding="utf-8")) if GOLDEN.exists() else {}
        golden[name] = current
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(golden, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert current == json.loads(GOLDEN.read_text(encoding="utf-8"))[name]


#: Every ADR-055 key, spelled out at its default value.
NEW_KEYS_AT_DEFAULT: dict[str, Any] = {
    "aggregate_in_replay": False,
    "list_cards_only_aggregate": False,
    "temporal_leg_event_dates": False,
    "cards_temporal": "skip",
    "profile_skip_temporal": False,
    "lead_budget_share": None,
}


@pytest.mark.parametrize("name", ["lead-blocks", "lead-blocks-skip-temporal"])
async def test_new_keys_spelled_out_at_default_match_the_golden(name: str) -> None:
    current = await snapshot({**CONFIGS[name], **NEW_KEYS_AT_DEFAULT})
    assert current == json.loads(GOLDEN.read_text(encoding="utf-8"))[name]
