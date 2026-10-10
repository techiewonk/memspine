"""Read outputs on the defaults are byte-identical to the pre-Wave-1 build (d6dccc5).

``golden/pre_wave1_read.json`` was recorded by running this file against the source
of commit d6dccc5, the last build before the Wave 1 fix pass (the ``final_answer``
rewrite and the #29 said-anchor fix). Unlike ``test_wave3_read_off_golden.py`` it
seeds mined facts tagged ``atomic_fact`` + ``happened:<date>`` (the records the #29
fix reads), checks every read mode and a direct ``assemble()``, and covers the
``core`` defaults and ``resolve_relative_dates`` on, both with ``read.cards`` off.

With ``resolve_relative_dates`` on, #29 intentionally changes outputs: a
happened-tagged fact's relative phrases are resolved against its ``said:`` day, and a
legacy one (no ``said:`` tag, ``valid_from`` on its happened day) is not resolved at
all, so "yesterday [= Thu 2023-06-08]" is no longer rendered on it. The same holds
with ``read.cards: header`` (the cards block uses the same anchor). That config is
recorded too, and its test pins that this annotation is the ONLY difference; see
the CHANGELOG and docs/USAGE.md.

To re-record (only against d6dccc5's source, never the current tree)::

    git worktree add <tmp> d6dccc5
    PYTHONPATH=<tmp>/src MEMSPINE_UPDATE_PRE_WAVE1_GOLDEN=1 pytest tests/unit/test_pre_wave1_read_golden.py

The generic ``MEMSPINE_UPDATE_GOLDENS=1`` deliberately does NOT re-record this file (H4,
2026-10-10): agents refreshing other goldens on the current tree kept overwriting it.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from memspine import Engine

GOLDEN = Path(__file__).parent / "golden" / "pre_wave1_read.json"
_UPDATE = os.environ.get("MEMSPINE_UPDATE_PRE_WAVE1_GOLDEN") == "1"

T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)

#: (text, day offset)
TURNS = [
    ("Melanie: I went camping with the kids last weekend", 0),
    ("Caroline: I started a new painting of a lake sunset", 3),
    ("Melanie: we ran a charity race for mental health in May", 9),
    ("Caroline: I joined a support group last Tuesday", 33),
    ("Melanie: the kids loved the pottery class yesterday", 40),
    ("Caroline: I am researching adoption agencies this month", 62),
]

#: (entity, attribute, text, day offset, tags); the last two are mined facts.
FACTS = [
    ("Melanie", "hobby", "Melanie hobby: pottery and painting", 41, ["person:melanie"]),
    ("Caroline", "plan", "Caroline plan: adopt a child", 63, []),
    ("Melanie", "event", "Melanie event: charity race in May 2023", 10, ["person:melanie"]),
    (
        "Melanie",
        "event",
        "Melanie event: pottery class yesterday",
        39,
        ["atomic_fact", "kind:event", "happened:2023-06-09"],
    ),
    (
        "Caroline",
        "event",
        "Caroline event: joined a support group last Tuesday",
        30,
        ["atomic_fact", "happened:2023-05-30"],
    ),
]

QUERIES = [
    "What activities does Melanie do?",
    "When did Caroline join the support group?",
    "What did Melanie do in May 2023?",
    "How many times did Melanie go camping?",
    "When did Melanie's kids go to the pottery class?",
]

#: Read configs covered (cards stay off in all of them).
CONFIGS: dict[str, dict[str, Any]] = {
    "core-defaults": {},
    "resolve-relative-dates": {"resolve_relative_dates": True},
}


@pytest.fixture(autouse=True)
def _fixed_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Record ids break score ties: make them a fixed sequence, so runs compare."""
    import uuid

    from memspine.core import records

    counter = iter(range(1, 1_000_000))
    monkeypatch.setattr(records, "uuid4", lambda: uuid.UUID(int=next(counter)))


def _now() -> datetime:
    """The fake "now": the goldens must not depend on the day they are run (recency
    scoring, ``recorded_at`` and the read-time anchor all read the wall clock).
    ``MEMSPINE_FAKE_NOW`` (ISO datetime) overrides it, to prove the output is date-free."""
    now = datetime.fromisoformat(os.environ.get("MEMSPINE_FAKE_NOW", "2026-01-15T12:00:00"))
    return now if now.tzinfo else now.replace(tzinfo=UTC)


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the wall-clock reads on the write/read path to one fixed instant."""
    from memspine.core import records
    from memspine.core.policies import scoring

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return _now() if tz is None else _now().astimezone(tz)

    monkeypatch.setattr(records, "datetime", _Frozen)
    monkeypatch.setattr(scoring, "datetime", _Frozen)
    monkeypatch.setattr(records, "_last_record_time", datetime.min.replace(tzinfo=UTC))


async def _snapshot(read: dict[str, Any]) -> dict[str, Any]:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"semantic": {"enabled": True}, "episodic": {"enabled": True}},
        read={"record_access": False, **read},
    )
    await eng.start()
    eng._clock = _now
    out: dict[str, Any] = {}
    try:
        for text, day in TURNS:
            await eng.write(
                text, namespace="a", memory_type="episodic", valid_from=T0 + timedelta(day)
            )
        for entity, attribute, text, day, tags in FACTS:
            await eng.write(
                text,
                namespace="a",
                entity=entity,
                attribute=attribute,
                tags=tags,
                valid_from=T0 + timedelta(days=day, hours=2),
            )
        for query in QUERIES:
            for mode in ("auto", "retrieve", "replay", "compose", "full"):
                result = await eng.read(query, namespace="a", mode=mode, top_k=3, budget_tokens=120)
                out[f"read|{mode}|{query}"] = [
                    result.mode,
                    result.context.abstained,
                    result.context.tokens_used,
                    [r.content for r in result.context.records],
                ]
            ctx = await eng.assemble(query, namespace="a", top_k=3)
            out[f"assemble|{query}"] = [r.content for r in ctx.records]
            hits = await eng.search(query, namespace="a", top_k=4)
            out[f"search|{query}"] = [[r.content, round(score, 4)] for r, score in hits]
    finally:
        await eng.stop()
    return out


def _golden(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(GOLDEN.read_text(encoding="utf-8"))[name]
    return data


async def _check_or_record(name: str) -> dict[str, Any]:
    current = await _snapshot(CONFIGS[name])
    if _UPDATE:
        golden = json.loads(GOLDEN.read_text(encoding="utf-8")) if GOLDEN.exists() else {}
        golden[name] = current
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(golden, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return current


async def test_core_defaults_match_the_pre_wave1_golden() -> None:
    """Defaults, mined happened-tagged facts, every read mode, direct assemble(),
    search: byte-identical to d6dccc5."""
    assert await _check_or_record("core-defaults") == _golden("core-defaults")


#: The one rendering #29 removes: the legacy happened-tagged fact (``valid_from`` on
#: its happened day, no ``said:`` tag) is no longer resolved against ``valid_from``.
_DROPPED_ANNOTATION = (
    "Melanie event: pottery class yesterday [= Thu 2023-06-08]",
    "Melanie event: pottery class yesterday",
)


def _without_29(snapshot: dict[str, Any]) -> dict[str, Any]:
    """``snapshot`` with #29's intended change applied and token counts dropped."""
    before, after = _DROPPED_ANNOTATION

    def fix(contents: list[str]) -> list[str]:
        return [after if c == before else c for c in contents]

    out: dict[str, Any] = {}
    for key, value in snapshot.items():
        if key.startswith("read|"):
            mode, abstained, _tokens, contents = value
            out[key] = [mode, abstained, fix(contents)]
        elif key.startswith("assemble|"):
            out[key] = fix(value)
        else:
            out[key] = value
    return out


async def test_resolve_relative_dates_differs_only_by_the_29_anchor() -> None:
    """With ``resolve_relative_dates`` on, the only change from d6dccc5 is the
    intended #29 one (documented as not reproducible against pre-Wave-1 builds)."""
    current = await _check_or_record("resolve-relative-dates")
    golden = _golden("resolve-relative-dates")
    if not _UPDATE:
        assert current != golden  # the change is real, so the pin below means something
    assert _without_29(current) == _without_29(golden)
