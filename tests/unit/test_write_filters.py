"""H21: deposit filters on write_messages (self-contamination)."""

from __future__ import annotations

from memspine import Engine


def _engine(**fw: object) -> Engine:
    return Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        firewall=fw,
    )


async def test_filters_skip_system_tool_and_recall_and_tag_claims() -> None:
    eng = _engine(
        skip_message_roles=["system", "tool"], skip_injected_recall=True, tag_assistant_claims=True
    )
    await eng.start()
    try:
        out = await eng.write_messages(
            [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "tool", "content": "search results: ..."},
                {"role": "user", "content": "CURRENT (since 2023-05-01): Ana lives in Lyon"},
                {"role": "user", "content": "I moved to Nice last week"},
                {"role": "assistant", "content": "You could try the new bakery"},
            ],
            namespace="a",
            session_id="s1",
        )
        assert [r.content for r in out] == [
            "I moved to Nice last week",
            "You could try the new bakery",
        ]
        assert "assistant_claim" in out[1].tags and "assistant_claim" not in out[0].tags
    finally:
        await eng.stop()


async def test_filters_off_by_default() -> None:
    eng = _engine()
    await eng.start()
    try:
        out = await eng.write_messages(
            [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "assistant", "content": "You could try the new bakery"},
            ],
            namespace="a",
            session_id="s1",
        )
        assert len(out) == 2 and all("assistant_claim" not in r.tags for r in out)
    finally:
        await eng.stop()
