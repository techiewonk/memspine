"""D2: group_id + tags sub-scoping facet.

group_id/tags are a filter within a namespace (a conversation, a document, a
project), never an isolation boundary. These prove the fields round-trip through
storage and that write/retrieve/search honor them, while un-tagged writes behave
exactly as before.
"""

from __future__ import annotations

from memspine.engine import Engine


async def _engine() -> Engine:
    engine = Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
    )
    await engine.start()
    return engine


async def test_write_persists_group_id_and_tags() -> None:
    engine = await _engine()
    try:
        rec = await engine.write(
            "deploy is blue-green", namespace="a", group_id="proj-x", tags=["ops", "infra"]
        )
        assert rec.group_id == "proj-x"
        assert set(rec.tags) == {"ops", "infra"}
        # Round-trips through the read model (fetched fresh, not the in-memory obj).
        fetched = (await engine.retrieve("a"))[0]
        assert fetched.group_id == "proj-x" and set(fetched.tags) == {"ops", "infra"}
    finally:
        await engine.stop()


async def test_defaults_are_unchanged_for_plain_writes() -> None:
    engine = await _engine()
    try:
        rec = await engine.write("plain fact", namespace="a")
        assert rec.group_id is None and rec.tags == []
    finally:
        await engine.stop()


async def test_retrieve_filters_by_group_id() -> None:
    engine = await _engine()
    try:
        await engine.write("x", namespace="a", group_id="g1")
        await engine.write("y", namespace="a", group_id="g2")
        await engine.write("z", namespace="a")  # no group
        g1 = await engine.retrieve("a", group_id="g1")
        assert [r.content for r in g1] == ["x"]
        # No filter still returns everything.
        assert len(await engine.retrieve("a")) == 3
    finally:
        await engine.stop()


async def test_retrieve_filters_by_tags_all_must_match() -> None:
    engine = await _engine()
    try:
        await engine.write("both", namespace="a", tags=["red", "blue"])
        await engine.write("one", namespace="a", tags=["red"])
        matched = await engine.retrieve("a", tags=["red", "blue"])
        assert [r.content for r in matched] == ["both"]
        # A single-tag filter matches the superset too.
        assert {r.content for r in await engine.retrieve("a", tags=["red"])} == {"both", "one"}
    finally:
        await engine.stop()


async def test_search_honors_group_id_gate() -> None:
    engine = await _engine()
    try:
        await engine.write("blue-green deploy pipeline", namespace="a", group_id="g1")
        await engine.write("blue-green deploy pipeline", namespace="a", group_id="g2")
        hits = await engine.search("deploy pipeline", namespace="a", group_id="g1")
        assert hits and all(r.group_id == "g1" for r, _ in hits)
    finally:
        await engine.stop()


async def test_write_episode_stamps_a_shared_group() -> None:
    engine = await _engine()
    try:
        records = await engine.write_episode(
            [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
            namespace="chat",
        )
        groups = {r.group_id for r in records}
        assert len(groups) == 1 and None not in groups
        # The whole episode is retrievable as one group.
        assert len(await engine.retrieve("chat", group_id=records[0].group_id)) == 2
    finally:
        await engine.stop()
