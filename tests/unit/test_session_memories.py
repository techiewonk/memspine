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
