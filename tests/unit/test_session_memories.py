"""N19 (plan v3.2): ``Engine.session_memories`` lists what was derived from turns."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from memspine import Engine


async def test_lists_records_derived_from_the_session_turns() -> None:
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
        texts = ["Caroline: I moved to Sweden last year", "Melanie: wow", "Caroline: yes!"]
        msgs = [
            {"role": "user", "content": c, "timestamp": (t0 + timedelta(minutes=i)).isoformat()}
            for i, c in enumerate(texts)
        ]
        await eng.write_messages(msgs, namespace="a", session_id="s1", group_id="s1")
        await eng.sleep()
        turns = await eng._require_started().list_records("a", "episodic")
        derived = await eng.session_memories("a", [t.record_id for t in turns])
        contents = [r.content for r in derived]
        assert "Caroline home: Sweden" in contents  # the mined fact
        assert any("Melanie: wow" in c for c in contents)  # the session summary
        assert not any(r.memory_type == "episodic" for r in derived)
        assert await eng.session_memories("a", []) == []
    finally:
        await eng.stop()


async def test_write_messages_keeps_an_image_caption() -> None:
    """G30 (plan v3.2): a turn's caption is stored with its text."""
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
    )
    await eng.start()
    try:
        await eng.write_messages(
            [{"role": "user", "content": "Look at this!", "caption": "a dog on a beach"}],
            namespace="a",
        )
        [turn] = await eng._require_started().list_records("a", "episodic")
        assert turn.content == "Look at this! [image: a dog on a beach]"
    finally:
        await eng.stop()


async def test_rating_profile_aggregates_by_domain() -> None:
    """G12 (plan v3.2): mean / std / count of logged ratings per domain."""
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
    )
    await eng.start()
    try:
        for i, (rating, domain) in enumerate([(5, "books"), (3, "books"), (2, "films")]):
            await eng.write(
                f"logged item {i}",
                namespace="a",
                memory_type="episodic",
                tags=[f"rating:{rating}", f"domain:{domain}"],
            )
        await eng.write("plain chat", namespace="a", memory_type="episodic")
        profile = await eng.rating_profile("a")
    finally:
        await eng.stop()
    assert profile == {
        "books": {"mean": 4.0, "std": 1.0, "n": 2.0},
        "films": {"mean": 2.0, "std": 0.0, "n": 1.0},
    }
