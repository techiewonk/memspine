"""G27 (plan v3.2, ADR-061): ``reply_to`` links and their replay expansion."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.config import constants
from memspine.exceptions import ConflictError

T0 = datetime(2023, 5, 8, 13, 0, tzinfo=UTC)


def _engine(associative: bool = False, **read: Any) -> Engine:
    memories: dict[str, Any] = {"episodic": {"enabled": True}}
    if associative:
        memories |= {"semantic": {"enabled": True}, "associative": {"enabled": True}}
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories=memories,
        read={"hybrid": False, "record_access": False, **read},
    )


async def test_write_reply_to_tags_the_reply_and_refuses_unknown_targets() -> None:
    eng = _engine()
    await eng.start()
    try:
        asked = await eng.write("Which venue should we book?", namespace="a")
        reply = await eng.write(
            "The Old Mill, it has parking.", namespace="a", reply_to=asked.record_id
        )
        assert f"{constants.REPLY_TO_PREFIX}{asked.record_id}" in reply.tags
        with pytest.raises(ConflictError):
            await eng.write("orphan", namespace="a", reply_to="no-such-id")
        with pytest.raises(ConflictError):
            await eng.write("foreign", namespace="b", reply_to=asked.record_id)
    finally:
        await eng.stop()


async def test_write_messages_reply_to_by_index_or_id() -> None:
    eng = _engine()
    await eng.start()
    try:
        first = await eng.write("Earlier note about the venue", namespace="a")
        msgs = [
            {"role": "user", "content": "Which venue should we book?"},
            {"role": "assistant", "content": "The Old Mill.", "reply_to": 0},
            {"role": "user", "content": "Same as my note?", "reply_to": first.record_id},
        ]
        records = await eng.write_messages(msgs, namespace="a", session_id="s1")  # type: ignore[arg-type]
        assert f"{constants.REPLY_TO_PREFIX}{records[0].record_id}" in records[1].tags
        assert f"{constants.REPLY_TO_PREFIX}{first.record_id}" in records[2].tags
        with pytest.raises(ValueError, match="reply_to"):
            await eng.write_messages(
                [{"role": "user", "content": "x", "reply_to": 3}],  # type: ignore[list-item]
                namespace="a",
            )
    finally:
        await eng.stop()


async def test_reply_link_edge_is_written_with_associative_memory() -> None:
    eng = _engine(associative=True)
    await eng.start()
    try:
        asked = await eng.write(
            "Which venue should we book?", namespace="a", memory_type="episodic"
        )
        reply = await eng.write(
            "The Old Mill.", namespace="a", memory_type="episodic", reply_to=asked.record_id
        )
        edges = [
            e
            for e in await eng._graph.edges_of(reply.record_id)
            if e.rel_type == constants.REPLY_TO_REL
        ]
        assert [(e.src, e.dst) for e in edges] == [(reply.record_id, asked.record_id)]
    finally:
        await eng.stop()


async def _thread(eng: Engine) -> tuple[str, str]:
    """A question, far-away chatter, then its answer replying to it."""
    msgs: list[dict[str, Any]] = [{"role": "user", "content": "Which venue should we book?"}]
    msgs += [{"role": "user", "content": f"unrelated chatter number {i}"} for i in range(8)]
    msgs.append({"role": "assistant", "content": "The Old Mill has parking.", "reply_to": 0})
    for i, msg in enumerate(msgs):
        msg["timestamp"] = (T0 + timedelta(minutes=i)).isoformat()
    records = await eng.write_messages(msgs, namespace="a", session_id="s1")  # type: ignore[arg-type]
    return records[0].content, records[-1].content


@pytest.mark.parametrize("on", [False, True])
async def test_replay_pulls_the_answered_message_only_when_on(on: bool) -> None:
    eng = _engine(reply_links=on)
    await eng.start()
    try:
        question, answer = await _thread(eng)
        result = await eng.read(
            "Old Mill parking", namespace="a", mode="replay", top_k=1, replay_window=1
        )
        assert result.mode == "replay"
        shown = [r.content for r in result.context.records]
        assert answer in shown
        assert (question in shown) is on
    finally:
        await eng.stop()
