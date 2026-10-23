"""I5: per-namespace size metrics."""

from __future__ import annotations

from memspine import Engine


async def test_namespace_stats_counts_one_user_only() -> None:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"episodic": {"enabled": True}},
    )
    await eng.start()
    try:
        await eng.ingest_messages(
            [
                {"role": "user", "content": "we went camping"},
                {"role": "assistant", "content": "nice"},
            ],
            namespace="alice",
            session_id="s1",
        )
        await eng.write("Bob likes tea", namespace="bob")
        alice = await eng.namespace_stats("alice")
        bob = await eng.namespace_stats("bob")
    finally:
        await eng.stop()
    assert alice["live"] == 2 and alice["conversations"] == 1
    assert alice["by_type"] == {"episodic": 2}
    assert alice["tokens_estimate"] > 0
    assert bob["live"] == 1 and bob["conversations"] == 0
