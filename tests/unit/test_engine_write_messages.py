"""C4: write_messages() / write_episode() transcript verbs.

Chat transcripts become per-turn episodic records through the ordinary write
door (firewall + lifecycle apply); write_episode links the turns under one
content-derived session id.
"""

from __future__ import annotations

import pytest

from memspine.engine import Engine


async def _engine() -> Engine:
    engine = Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
    )
    await engine.start()
    return engine


async def test_write_messages_makes_one_episodic_record_per_turn() -> None:
    engine = await _engine()
    try:
        messages = [
            {"role": "user", "content": "What's the capital of France?"},
            {"role": "assistant", "content": "Paris."},
            {"role": "user", "content": "Thanks!"},
        ]
        records = await engine.write_messages(messages, namespace="chat/1")
        assert len(records) == 3
        assert all(r.memory_type == "episodic" for r in records)
        assert [r.source.role for r in records] == ["user", "assistant", "user"]
        assert all(r.source.channel == "messages" for r in records)
        # Retrievable like any episodic write.
        got = await engine.retrieve("chat/1")
        assert len(got) == 3
    finally:
        await engine.stop()


async def test_write_episode_links_turns_under_one_session_id() -> None:
    engine = await _engine()
    try:
        messages = [
            {"role": "user", "content": "Let's plan the trip."},
            {"role": "assistant", "content": "Sure, where to?"},
        ]
        records = await engine.write_episode(messages, namespace="chat/2")
        session_ids = {r.source.message_id for r in records}
        assert len(session_ids) == 1 and None not in session_ids
        # Deterministic: the same transcript yields the same session id (N6).
        again = await engine.write_episode(messages, namespace="chat/3")
        assert again[0].source.message_id == records[0].source.message_id
    finally:
        await engine.stop()


async def test_malformed_message_fails_loudly() -> None:
    engine = await _engine()
    try:
        with pytest.raises(ValueError, match="messages\\[1\\] must be a mapping"):
            await engine.write_messages(
                [{"role": "user", "content": "ok"}, {"role": "user"}],  # missing content
                namespace="chat/4",
            )
    finally:
        await engine.stop()
