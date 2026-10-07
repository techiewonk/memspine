"""W5 (plan v3.2): the rule miner (``consolidation.miner: rules``), no model."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from memspine import Engine
from memspine.core.policies.consolidation import ConsolidationOptions
from memspine.core.records import RecordStatus
from memspine.core.rule_miner import mine_rules

LINES = "\n".join(
    [
        "[1] [2023-05-08] Caroline: I moved to Sweden four years ago and I love painting sunsets.",
        "[2] [2023-05-08] Melanie: My dog is called Oscar, and my favorite book is"
        " Charlotte's Web!",
        "[3] [2023-05-09] Caroline: I'm from Ohio originally. I work as a counselor at the center.",
        "[4] [2023-05-09] Melanie: I went camping last week. I'm planning to run a marathon.",
        "[5] [2023-05-09] Melanie: I love how you explained that.",
        "[6] [2023-05-10] I'm vegetarian and I can't stand loud bars.",
    ]
)


def _facts() -> set[tuple[str, str, str, str]]:
    return {(f.entity, f.attribute, f.value, f.kind) for f in mine_rules(LINES)}


@pytest.mark.parametrize(
    "fact",
    [
        ("Caroline", "home", "Sweden", "state"),
        ("Caroline", "likes", "painting sunsets", "event"),
        ("Melanie", "pets", "Oscar", "event"),
        ("Melanie", "favourite_book", "Charlotte's Web", "state"),
        ("Caroline", "origin", "Ohio", "state"),
        ("Caroline", "job", "counselor", "state"),
        ("Caroline", "employer", "the center", "state"),
        ("Melanie", "activities", "camping", "event"),
        ("Melanie", "plans", "run a marathon", "event"),
        ("user", "diet", "vegetarian", "state"),
        ("user", "dislikes", "loud bars", "event"),
    ],
)
def test_rule_miner_finds_personal_facts(fact: tuple[str, str, str, str]) -> None:
    assert fact in _facts()


def test_rule_miner_cites_its_line_and_date() -> None:
    home = next(f for f in mine_rules(LINES) if f.attribute == "home")
    assert home.turns == [1]
    assert home.date == "2023-05-08"


def test_compliment_to_the_assistant_is_not_a_like() -> None:
    assert not any("explained" in value for _, _, value, _ in _facts())


def test_miner_defaults_to_llm() -> None:
    assert ConsolidationOptions().miner == "llm"


async def test_rule_mining_end_to_end_supersedes_home_without_an_llm() -> None:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            "episodic": {
                "enabled": True,
                "policies": {"consolidation": {"mine_facts": True, "miner": "rules"}},
            },
            "semantic": {"enabled": True},
        },
        read={"hybrid": False},
    )
    await eng.start()
    try:
        t0 = datetime(2023, 5, 8, 13, 0, tzinfo=UTC)
        sessions = {
            0: ["Caroline: I moved to Sweden last year", "Melanie: wow", "Caroline: yes, cold!"],
            30: ["Caroline: I moved to Norway", "Melanie: again?", "Caroline: for work"],
        }
        for day, texts in sessions.items():
            msgs = [
                {
                    "role": "user",
                    "content": text,
                    "timestamp": (t0 + timedelta(days=day, minutes=i)).isoformat(),
                }
                for i, text in enumerate(texts)
            ]
            await eng.write_messages(msgs, namespace="a", session_id=f"s{day}", group_id=f"s{day}")
        await eng.sleep()
        assert eng.model_calls() == {}
        homes = [
            r
            for r in await eng._require_started().list_records("a", "semantic")
            if r.attribute == "home"
        ]
        current = [r for r in homes if r.status is RecordStatus.ACTIVATED and r.valid_to is None]
        assert [r.content for r in current] == ["Caroline home: Norway"]
        assert any("Sweden" in r.content and r.valid_to is not None for r in homes)
    finally:
        await eng.stop()


async def test_auto_watch_turns_a_dated_plan_into_a_watch() -> None:
    """G33 (plan v3.2): a mined plan with a future date in its turn becomes a watch."""
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            "episodic": {
                "enabled": True,
                "policies": {
                    "consolidation": {"mine_facts": True, "miner": "rules", "auto_watch": True}
                },
            },
            "semantic": {"enabled": True},
            "prospective": {"enabled": True},
        },
        read={"hybrid": False},
    )
    await eng.start()
    try:
        t0 = datetime(2023, 5, 8, 13, 0, tzinfo=UTC)
        texts = [
            "Melanie: I'm planning to run a marathon next month",
            "Caroline: wow",
            "Melanie: yes!",
        ]
        msgs = [
            {"role": "user", "content": c, "timestamp": (t0 + timedelta(minutes=i)).isoformat()}
            for i, c in enumerate(texts)
        ]
        await eng.write_messages(msgs, namespace="a", session_id="s1", group_id="s1")
        await eng.sleep()
        watches = await eng._require_started().list_records("a", "prospective")
        assert [w.content for w in watches] == ["Melanie plans: run a marathon"]
        assert watches[0].valid_from.date() > t0.date()
        assert eng.model_calls() == {}
    finally:
        await eng.stop()


def test_attitude_slot_and_canonical_favourite_keys() -> None:
    """N07 / N08 (plan v3.2): a change of mind supersedes; synonymous slots merge."""
    from memspine.core.rule_miner import canonical_thing

    lines = (
        "[1] [2023-05-08] Ana: I love jazz bars. My favorite novel is Dune.\n"
        "[2] [2023-07-01] Ana: I no longer like jazz bars, too loud. "
        "My favourite book is Emma."
    )
    facts = [(f.attribute, f.value, f.kind, f.turns) for f in mine_rules(lines)]
    assert ("attitude:jazz_bars", "likes", "state", [1]) in facts
    assert ("attitude:jazz_bars", "dislikes", "state", [2]) in facts
    assert ("favourite_book", "Dune", "state", [1]) in facts
    assert ("favourite_book", "Emma", "state", [2]) in facts
    assert canonical_thing("TV show") == "show"
    assert canonical_thing("colour") == "color"
