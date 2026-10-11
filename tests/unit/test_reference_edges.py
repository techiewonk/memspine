"""A07: ``read.reference_edges`` writes reply / reference edges from the turn order and attaches
the antecedent of a retrieved short reply at read time (bounded, logged)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.config import constants
from memspine.core.reference_edges import antecedent_kind, reference_kinds
from memspine.engine import search_forensics

T0 = datetime(2023, 5, 8, 13, 0, tzinfo=UTC)


# -- the rule (pure) ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "speaker", "prev", "kind"),
    [
        ("Sounds great!", "bob", "amy", "short_reply"),
        ("Amy: Yes, in May", "bob", "amy", "short_reply"),
        ("Sounds great!", "amy", "amy", None),  # same speaker: not a reply
        ("Sounds great!", None, "amy", None),  # unknown speaker
        ("I spent the whole afternoon repainting the old fence behind the barn", "bob", "amy", None),
        ("That was so much fun", "amy", "amy", "deictic"),  # deictic needs no other speaker
        ("We should do this again some time soon", "bob", "amy", "deictic"),
        ("Those look lovely", "bob", "amy", "deictic"),
        ("Like that, but bigger and with more room for everyone", "bob", "amy", "deictic"),
        ("Thanks, that was great", "bob", "amy", "short_reply"),  # 'that' is not an opener
        ("The weather that day was warm and the whole town came out to watch", "bob", "amy", None),
        ("", "bob", "amy", None),
    ],
)
def test_antecedent_kind(content: str, speaker: str | None, prev: str | None, kind: str | None) -> None:
    assert antecedent_kind(content, speaker, prev) == kind


def test_no_previous_turn_means_no_edge() -> None:
    assert antecedent_kind("That was fun", "bob", None, has_previous=False) is None


def test_short_word_limit_is_a_parameter() -> None:
    text = "one two three four five six seven eight"
    assert antecedent_kind(text, "b", "a", max_words=6) is None
    assert antecedent_kind(text, "b", "a", max_words=8) == "short_reply"


def test_reference_kinds_reads_tags() -> None:
    assert reference_kinds(["x", "ref:deictic", "reply_to:abc"]) == ["deictic"]


# -- engine ------------------------------------------------------------------------------------

CONVERSATION = [
    {"role": "user", "speaker": "amy", "content": "Amy: I booked the Old Mill for the reunion dinner"},
    {"role": "user", "speaker": "bob", "content": "Bob: Sounds great!"},
    {"role": "user", "speaker": "bob", "content": "Bob: Filler about hiking boots and long trails"},
    {"role": "user", "speaker": "amy", "content": "Amy: Unrelated note about the garden shed roof"},
    {"role": "user", "speaker": "bob", "content": "Bob: We should plant roses there as well"},
]


def _engine(**read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"record_access": False, "hybrid": False, **read},
    )


def _messages() -> list[dict[str, Any]]:
    return [
        {**m, "timestamp": (T0 + timedelta(minutes=i)).isoformat()}
        for i, m in enumerate(CONVERSATION)
    ]


async def _ingest(eng: Engine) -> list[Any]:
    return await eng.write_messages(_messages(), namespace="a", session_id="s1")  # type: ignore[arg-type]


async def test_default_off_writes_no_edges() -> None:
    eng = _engine()
    await eng.start()
    try:
        recs = await _ingest(eng)
        assert not [t for r in recs for t in r.tags if t.startswith(("reply_to:", "ref:"))]
    finally:
        await eng.stop()


async def test_on_links_short_replies_and_deictic_turns_to_the_previous_turn() -> None:
    eng = _engine(reference_edges="on")
    await eng.start()
    try:
        recs = await _ingest(eng)
        pre = constants.REPLY_TO_PREFIX
        assert not any(t.startswith(pre) for t in recs[0].tags)  # nothing before the first
        assert f"{pre}{recs[0].record_id}" in recs[1].tags and "ref:short_reply" in recs[1].tags
        assert not any(t.startswith("ref:") for t in recs[2].tags)  # same speaker, long enough
        assert not any(t.startswith("ref:") for t in recs[3].tags)
        assert f"{pre}{recs[3].record_id}" in recs[4].tags and "ref:deictic" in recs[4].tags
    finally:
        await eng.stop()


async def test_an_explicit_reply_to_is_never_overridden() -> None:
    eng = _engine(reference_edges="on")
    await eng.start()
    try:
        msgs = _messages()
        msgs[1] = {**msgs[1], "reply_to": 0}
        msgs[4] = {**msgs[4], "reply_to": 0}
        recs = await eng.write_messages(msgs, namespace="a", session_id="s1")  # type: ignore[arg-type]
        pre = constants.REPLY_TO_PREFIX
        assert not any(t.startswith("ref:") for t in recs[1].tags + recs[4].tags)
        assert [t for t in recs[4].tags if t.startswith(pre)] == [f"{pre}{recs[0].record_id}"]
    finally:
        await eng.stop()


async def _replay(eng: Engine, query: str) -> tuple[Any, dict[str, Any]]:
    with search_forensics() as fx:
        out = await eng.read(query, namespace="a", mode="replay", top_k=1)
    return out, fx


async def test_read_attaches_the_antecedent_of_a_retrieved_short_reply() -> None:
    texts: dict[str, list[str]] = {}
    logs: dict[str, dict[str, Any]] = {}
    for mode in ("off", "on"):
        # no neighbour window: the antecedent can only arrive through the edge
        eng = _engine(reference_edges=mode, replay_window_before=0, replay_window_after=0)
        await eng.start()
        try:
            recs = await _ingest(eng)
            out, fx = await _replay(eng, "Bob: Sounds great!")
            texts[mode] = [r.content for r in out.context.records]
            logs[mode] = fx
        finally:
            await eng.stop()
    assert CONVERSATION[1]["content"] in texts["off"]
    assert CONVERSATION[0]["content"] not in texts["off"]  # control: the window is closed
    assert CONVERSATION[0]["content"] in texts["on"]
    edge = logs["on"]["reference_edges"][0]
    assert edge["outcome"] == "attached" and edge["kind"] == "short_reply"
    assert edge["antecedent"] == recs[0].record_id


async def test_read_is_unchanged_when_off() -> None:
    off = _engine()
    await off.start()
    try:
        await _ingest(off)
        _, fx = await _replay(off, "Sounds great!")
        assert "reference_edges" not in fx
    finally:
        await off.stop()


async def test_attach_is_bounded_logged_and_namespace_safe() -> None:
    eng = _engine(reference_edges="on", reference_max_per_read=1)
    await eng.start()
    try:
        recs = await _ingest(eng)
        hits = [recs[1], recs[4]]  # both carry ref: tags
        chosen: list[Any] = []
        seen: set[str] = set()
        best: set[str] = set()
        with search_forensics() as fx:
            used = await eng._attach_antecedents(
                "a", hits, chosen, seen, best, 0, 10_000, eng._config().read
            )
        assert [e["outcome"] for e in fx["reference_edges"]] == ["attached", "over_cap"]
        assert len(chosen) == 1 and chosen[0].record_id == recs[0].record_id and used > 0
        assert best == {recs[0].record_id}  # the best hit's antecedent joins the best window

        # a budget too small for the antecedent: logged, nothing attached
        chosen2: list[Any] = []
        with search_forensics() as fx2:
            await eng._attach_antecedents("a", hits, chosen2, set(), set(), 0, 1, eng._config().read)
        assert chosen2 == [] and fx2["reference_edges"][0]["outcome"] == "over_budget"

        # another namespace's id is never attached
        with search_forensics() as fx3:
            chosen3: list[Any] = []
            await eng._attach_antecedents("b", hits, chosen3, set(), set(), 0, 10_000, eng._config().read)
        assert chosen3 == [] and {e["outcome"] for e in fx3["reference_edges"]} == {"missing"}
    finally:
        await eng.stop()
