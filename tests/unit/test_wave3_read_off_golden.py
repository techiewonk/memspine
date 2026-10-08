"""Wave 3 read items (#36-#40) off: ``read()`` and ``search()`` are byte-identical.

The snapshot in ``golden/wave3_read_off.json`` was recorded on the engine before the
persons / time-expression leg (#36), the search date filters (#37), the completeness
check (#38), answer verification (#39) and profile-header packing (#40) existed, on the
fixed fixture below. With every new flag at its off value (and no date filter passed),
every read mode and search must still produce exactly the recorded output.

To accept an intended change, regenerate and review the diff::

    MEMSPINE_UPDATE_GOLDENS=1 pytest tests/unit/test_wave3_read_off_golden.py
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from memspine import Engine

GOLDEN = Path(__file__).parent / "golden" / "wave3_read_off.json"
_UPDATE = os.environ.get("MEMSPINE_UPDATE_GOLDENS") == "1"

T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)

#: (speaker, text, day offset)
TURNS = [
    ("Melanie", "Melanie: I went camping with the kids last weekend", 0),
    ("Caroline", "Caroline: I started a new painting of a lake sunset", 3),
    ("Melanie", "Melanie: we ran a charity race for mental health in May", 9),
    ("Caroline", "Caroline: I joined a support group last Tuesday", 33),
    ("Melanie", "Melanie: the kids loved the pottery class yesterday", 40),
    ("Caroline", "Caroline: I am researching adoption agencies this month", 62),
]

#: (entity, attribute, text, day offset, tags)
FACTS = [
    ("Melanie", "hobby", "Melanie hobby: pottery and painting", 41, ["person:melanie"]),
    ("Caroline", "plan", "Caroline plan: adopt a child", 63, []),
    ("Melanie", "event", "Melanie event: charity race in May 2023", 10, ["person:melanie"]),
]

QUERIES = [
    "What activities does Melanie do?",
    "When did Caroline join the support group?",
    "What did Melanie do in May 2023?",
    "How many times did Melanie go camping?",
]


def _engine(**read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"semantic": {"enabled": True}, "episodic": {"enabled": True}},
        read={"record_access": False, **read},
    )


async def seed(eng: Engine, ns: str = "a") -> None:
    """The fixed fixture: six dated turns and three keyed facts."""
    for _speaker, text, day in TURNS:
        await eng.write(text, namespace=ns, memory_type="episodic", valid_from=T0 + timedelta(day))
    for entity, attribute, text, day, tags in FACTS:
        await eng.write(
            text,
            namespace=ns,
            entity=entity,
            attribute=attribute,
            tags=tags,
            valid_from=T0 + timedelta(days=day, hours=2),
        )


async def _snapshot(eng: Engine) -> dict[str, Any]:
    await seed(eng)
    out: dict[str, Any] = {}
    for query in QUERIES:
        for mode in ("auto", "retrieve", "replay", "compose"):
            result = await eng.read(query, namespace="a", mode=mode, top_k=3, budget_tokens=70)
            out[f"read|{mode}|{query}"] = {
                "mode": result.mode,
                "abstained": result.context.abstained,
                "tokens_used": result.context.tokens_used,
                "contents": [r.content for r in result.context.records],
            }
        hits = await eng.search(query, namespace="a", top_k=4)
        out[f"search|{query}"] = [[r.content, round(score, 4)] for r, score in hits]
    return out


@pytest.fixture(autouse=True)
def _fixed_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Record ids break score ties: make them a fixed sequence, so runs compare."""
    import uuid

    from memspine.core import records

    counter = iter(range(1, 1_000_000))
    monkeypatch.setattr(records, "uuid4", lambda: uuid.UUID(int=next(counter)))


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze the wall clock seen by record times and recency scoring.

    Recency decays with the real time between write and search, so a search score
    on a 4-decimal rounding boundary (0.6465 / 0.6464) flipped from run to run."""
    from memspine.core import records
    from memspine.core.policies import scoring

    frozen = datetime(2023, 7, 15, 12, 0, tzinfo=UTC)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return frozen if tz is not None else frozen.replace(tzinfo=None)

    monkeypatch.setattr(records, "datetime", _Frozen)
    monkeypatch.setattr(scoring, "datetime", _Frozen)
    monkeypatch.setattr(records, "_last_record_time", datetime.min.replace(tzinfo=UTC))


async def _current(**read: Any) -> dict[str, Any]:
    eng = _engine(**read)
    await eng.start()
    try:
        return await _snapshot(eng)
    finally:
        await eng.stop()


async def test_wave3_off_read_matches_the_pre_change_golden() -> None:
    current = await _current()
    if _UPDATE:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert current == json.loads(GOLDEN.read_text(encoding="utf-8"))


def _new_flags() -> dict[str, Any]:
    from memspine.config.schema import ReadConfig

    off = {
        "completeness_check": False,
        "profile_header_packing": False,
        "person_time_leg_k": 10,
    }
    return {k: v for k, v in off.items() if k in ReadConfig.model_fields}


async def test_explicit_off_flags_match_the_golden() -> None:
    """The new keys spelled out at their off values change nothing either."""
    current = await _current(**_new_flags())
    assert current == json.loads(GOLDEN.read_text(encoding="utf-8"))
