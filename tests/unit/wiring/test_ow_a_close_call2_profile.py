"""OW-A: O4-02 (call 2 at session close) and O4-07 (guarded profile card), flag-gated, model-free.

A fake ``session_close`` role answers with JSON that cites the numbered lines of the prompt.
Flag off: the stub runs and nothing is written. Flag on: summary and episodes land through the
derived-write door (lineage edges when ``write.lineage_table``), bad items are dropped by reason,
a repeat close is a no-op, a late turn is a new version, and profile ops go through the guard."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.derive.pipeline.stages import profile_card
from memspine.derive.profile import ProfileCard
from memspine.services.llm.base import LLMRouter
from memspine.store import lineage as lineage_store

T0 = datetime(2023, 5, 8, 13, 0, tzinfo=UTC)
LINE = re.compile(r"^\[(\d+)\] \S+ (\S+): (.+)$", re.M)

_TEXTS = (
    "I repotted the orchid on the balcony",
    "We booked a kayak tour along the northern coast",
    "The violin recital starts at seven on Friday",
    "I prefer tea without sugar in the evening",
)


class FakeCloseLLM:
    """Stand-in for the call-2 role: a summary, one episode over all lines, and profile ops."""

    def __init__(self) -> None:
        self.prompts: list[tuple[str, str]] = []
        self.raise_once = False
        self.reply: dict[str, Any] | str | None = None
        self.ops: list[dict[str, Any]] | None = None
        self.episode_texts: list[str] = []  # popped per call; else a fixed text

    @property
    def provider_id(self) -> str:
        return "fake:close"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        system, user = messages[0]["content"], messages[-1]["content"]
        self.prompts.append((system, user))
        if self.raise_once:
            self.raise_once = False
            raise RuntimeError("model down")
        if isinstance(self.reply, str):
            return self.reply
        if isinstance(self.reply, dict):
            return json.dumps(self.reply)
        lines = [int(n) for n, _, _ in LINE.findall(user)]
        out: dict[str, Any] = {
            "summary": {"text": "Mira talks about her plants, trips and tea."},
            "episodes": [
                {
                    "text": self.episode_texts.pop(0)
                    if self.episode_texts
                    else "Mira discussed her orchid, a kayak tour and a recital.",
                    "lines": lines,
                }
            ],
            "profile_ops": self.ops
            if self.ops is not None
            else [
                {
                    "op": "add",
                    "entry_type": "attribute",
                    "key": "tea",
                    "value": "prefers tea without sugar",
                    "stance": "stated",
                    "cites": [lines[-1]],
                }
            ],
        }
        return json.dumps(out)


def _turns(count: int, start: int = 0, session: str | None = None) -> list[dict[str, str]]:
    return [
        {
            "role": "user",
            "content": f"Mira: {_TEXTS[(start + i) % len(_TEXTS)]}",
            "timestamp": (T0 + timedelta(minutes=start + i)).isoformat(),
        }
        for i in range(count)
    ]


async def _engine(
    monkeypatch: pytest.MonkeyPatch, llm: FakeCloseLLM, *, role: str = "session_close", **write: Any
) -> Engine:
    async def router(
        config: MemspineConfig, hooks: object = None, wiring: object = None
    ) -> LLMRouter:
        return LLMRouter({role: llm})  # type: ignore[dict-item]

    monkeypatch.setattr("memspine.runtime.container.build_llm_router", router)
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={
            "embedding": {"provider": "hash", "settings": {}},
            "llm": {
                role: {
                    "provider": "litellm",
                    "timeout_s": 60.0,
                    "settings": {"model": "fake/close"},
                }
            },
        },
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        offline={k: v for k, v in write.items() if k != "lineage_table"},
        **({"write": {"lineage_table": True}} if write.get("lineage_table") else {}),
    )
    await eng.start()
    return eng


async def _send(eng: Engine, session: str, messages: list[dict[str, str]], ns: str = "a") -> None:
    await eng.ingest_messages(messages, namespace=ns, session_id=session, group_id=session)


async def _derived(eng: Engine, ns: str = "a") -> list[Any]:
    storage = eng._require_started()
    return [
        r for r in await storage.list_records(ns, "semantic") if r.source.channel == "session_close"
    ]


async def _markers(eng: Engine, name: str) -> list[Any]:
    events = await eng._require_started().read_events(after_seq=0, limit=10000)
    return [e for e in events if (e.payload or {}).get("marker") == name]


async def test_flag_off_is_the_stub_and_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = FakeCloseLLM()
    eng = await _engine(monkeypatch, llm)
    try:
        await _send(eng, "s1", _turns(3))
        out = await eng.close_session("a", "s1")
        assert out["hook"]["status"] == "stub"
        assert llm.prompts == [] and await _derived(eng) == []
        assert len(await _markers(eng, "session_closed")) == 1
        assert await _markers(eng, "profile_card_v2") == []
    finally:
        await eng.stop()


async def test_flag_on_writes_summary_and_episode_with_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = FakeCloseLLM()
    eng = await _engine(monkeypatch, llm, session_close_v2=True, lineage_table=True)
    try:
        await _send(eng, "s1", _turns(3))
        out = await eng.close_session("a", "s1")
        hook = out["hook"]
        assert hook["status"] == "ok" and hook["summary"] == 1 and hook["episodes"] == 1
        assert hook["written"] == 2 and hook["profile"]["status"] == "off"
        assert len(llm.prompts) == 1
        # the prompt is the library's: a stable prefix without examples, then DATA with turns
        system, user = llm.prompts[0]
        assert "language of the conversation" in system and "e.g." not in system
        assert user.startswith("DATA") and "[1]" in user and "Mira" in user
        recs = await _derived(eng)
        kinds = sorted(next(t for t in r.tags if t.startswith("kind:")) for r in recs)
        assert kinds == ["kind:episode", "kind:summary"]
        storage = eng._require_started()
        turn_ids = {r.record_id for r in await storage.list_records("a", "episodic")}
        for rec in recs:
            assert set(rec.source.parents) == turn_ids
            edges = await lineage_store.parents_of(storage._client.engine, [rec.record_id])
            assert {e.parent for e in edges[rec.record_id]} == turn_ids
            parents = [await storage.get_record(p) for p in rec.source.parents]
            assert rec.trust <= min(p.trust for p in parents if p is not None)
        # the stub marker is still left, and the stage marker makes a second close a no-op
        assert len(await _markers(eng, "session_closed")) == 1
        again = await eng.close_session("a", "s1")
        assert again["hook"]["status"] == "skipped" or again["status"] == "skipped"
        assert len(llm.prompts) == 1 and len(await _derived(eng)) == 2
    finally:
        await eng.stop()


async def test_invalid_items_are_dropped_by_reason_and_siblings_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = FakeCloseLLM()
    llm.reply = {
        "summary": {"text": "A short chat."},
        "episodes": [
            {"text": "Uncited narrative."},
            {"text": "Cites a line that does not exist.", "lines": [99]},
            {"text": "Good one.", "lines": [1, 2]},
        ],
    }
    eng = await _engine(monkeypatch, llm, session_close_v2=True)
    try:
        await _send(eng, "s1", _turns(2))
        hook = (await eng.close_session("a", "s1"))["hook"]
        assert hook["status"] == "ok" and hook["episodes"] == 1 and hook["rejected"] == 2
        assert hook["reasons"] == {"cites_outside_session": 1, "uncited": 1}
        assert len(await _derived(eng)) == 2
    finally:
        await eng.stop()


async def test_parse_error_and_model_error_leave_no_marker_and_can_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = FakeCloseLLM()
    eng = await _engine(monkeypatch, llm, session_close_v2=True)
    try:
        await _send(eng, "s1", _turns(2))
        llm.raise_once = True
        hook = (await eng.close_session("a", "s1"))["hook"]
        assert hook["status"] == "error" and await _derived(eng) == []
        llm.reply = "this is not json at all"
        hook = (await eng.close_session("a", "s1"))["hook"]
        assert hook["status"] == "parse_error" and await _derived(eng) == []
        llm.reply = None
        hook = (await eng.close_session("a", "s1"))["hook"]
        assert hook["status"] == "ok" and len(await _derived(eng)) == 2
    finally:
        await eng.stop()


async def test_summarize_role_is_the_fallback_and_missing_role_is_the_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = FakeCloseLLM()
    eng = await _engine(monkeypatch, llm, role="summarize", session_close_v2=True)
    try:
        await _send(eng, "s1", _turns(2))
        assert (await eng.close_session("a", "s1"))["hook"]["status"] == "ok"
    finally:
        await eng.stop()
    eng = await _engine(monkeypatch, llm, role="judge", session_close_v2=True)
    try:
        await _send(eng, "s2", _turns(2))
        hook = (await eng.close_session("a", "s2"))["hook"]
        assert hook["status"] == "stub" and hook["v2"]["status"] == "skipped"
    finally:
        await eng.stop()


async def test_a_late_turn_is_a_new_version_and_stitches_onto_the_earlier_episode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = FakeCloseLLM()
    llm.episode_texts = [
        "Mira discussed her orchid, a kayak tour and a recital.",
        "Mira also discussed her orchid again and the kayak tour, then said she "
        "prefers tea without sugar in the evening.",
    ]
    eng = await _engine(monkeypatch, llm, session_close_v2=True)
    try:
        await _send(eng, "s1", _turns(2))
        await eng.close_session("a", "s1")
        first = [r for r in await _derived(eng) if "kind:episode" in r.tags]
        assert len(first) == 1
        await _send(eng, "s1", _turns(1, start=2))
        hook = (await eng.close_session("a", "s1"))["hook"]
        assert hook["status"] == "ok"
        # the new episode shares participants, topic words and time with the first: it extends it
        assert hook["stitched"] == 1
        episodes = [r for r in await _derived(eng) if "kind:episode" in r.tags]
        assert len(episodes) == 2
        ext = next(r for r in episodes if any(t.startswith("extends:") for t in r.tags))
        assert f"extends:{first[0].record_id}" in ext.tags
        assert first[0].record_id in ext.source.parents  # nothing deleted; the stub stays parent
        assert first[0].content in ext.content
    finally:
        await eng.stop()


async def test_profile_ops_flow_into_the_guarded_card(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = FakeCloseLLM()
    eng = await _engine(monkeypatch, llm, session_close_v2=True, profile_card_v2=True)
    try:
        await _send(eng, "s1", _turns(4))
        hook = (await eng.close_session("a", "s1"))["hook"]
        prof = hook["profile"]
        assert prof["applied"] == 1 and prof["rejected"] == 0 and prof["entries"] == 1
        ctx = eng._pipeline_ctx()
        card: ProfileCard = await profile_card.load_card(ctx, "a")
        entry = card.get("preferences", "tea")
        assert entry is not None and entry.value == "prefers tea without sugar"
        assert entry.status == "active" and len(entry.support) == 1
        assert len(await _markers(eng, "profile_card_v2")) == 1
    finally:
        await eng.stop()


async def test_guard_rejects_inferred_uncited_and_foreign_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = FakeCloseLLM()
    llm.ops = [
        {
            "op": "add",
            "entry_type": "attribute",
            "key": "mood",
            "value": "seems happy",
            "stance": "inferred",
            "cites": [1],
        },
        {
            "op": "add",
            "entry_type": "attribute",
            "key": "tea",
            "value": "likes tea",
            "stance": "stated",
        },
        {
            "op": "delete",
            "entry_type": "attribute",
            "key": "coffee",
            "value": "",
            "stance": "stated",
            "cites": [1],
        },
    ]
    eng = await _engine(monkeypatch, llm, session_close_v2=True, profile_card_v2=True)
    try:
        await _send(eng, "s1", _turns(3))
        prof = (await eng.close_session("a", "s1"))["hook"]["profile"]
        # inferred is off by default; the uncited op never reaches the guard (call-2 validation)
        assert prof["applied"] == 0 and prof["rejected"] >= 1
        assert prof["reasons"].get("inferred_disabled") == 1
        card = await profile_card.load_card(eng._pipeline_ctx(), "a")
        assert card.entries == ()
    finally:
        await eng.stop()


async def test_kappa_p_bound_evicts_and_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = FakeCloseLLM()
    llm.ops = [
        {
            "op": "add",
            "entry_type": "attribute",
            "key": f"k{i}",
            "value": f"value number {i}",
            "stance": "stated",
            "cites": [1 + (i % 3)],
        }
        for i in range(5)
    ]
    eng = await _engine(
        monkeypatch, llm, session_close_v2=True, profile_card_v2=True, profile_kappa_p=2
    )
    try:
        await _send(eng, "s1", _turns(3))
        prof = (await eng.close_session("a", "s1"))["hook"]["profile"]
        assert prof["applied"] == 5 and prof["evicted"] == 3 and prof["entries"] == 2
        card = await profile_card.load_card(eng._pipeline_ctx(), "a")
        assert len(card.entries) == 2
    finally:
        await eng.stop()


async def test_profile_ops_are_only_counted_with_the_card_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = FakeCloseLLM()
    eng = await _engine(monkeypatch, llm, session_close_v2=True)
    try:
        await _send(eng, "s1", _turns(3))
        prof = (await eng.close_session("a", "s1"))["hook"]["profile"]
        assert prof == {"status": "off", "proposed": 1}
        assert await _markers(eng, "profile_card_v2") == []
    finally:
        await eng.stop()


async def test_sleep_invalidates_entries_whose_support_was_forgotten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = FakeCloseLLM()
    eng = await _engine(monkeypatch, llm, session_close_v2=True, profile_card_v2=True)
    try:
        await _send(eng, "s1", _turns(4))
        await eng.close_session("a", "s1")
        card = await profile_card.load_card(eng._pipeline_ctx(), "a")
        support = card.get("preferences", "tea").support_ids  # type: ignore[union-attr]
        await eng.forget(support[0], namespace="a", hard=True)
        # FU-CLOSE: the forget already took the entry off the card; sleep finds nothing left
        assert (await profile_card.load_card(eng._pipeline_ctx(), "a")).entries == ()
        out = await eng.sleep()
        stats = out["reflect_profile"]["profile_card"]
        assert stats["status"] == "ok" and stats["expired"] == 0
        card = await profile_card.load_card(eng._pipeline_ctx(), "a")
        assert card.entries == ()
    finally:
        await eng.stop()


async def test_sleep_reports_the_reflection_plan_for_a_tentative_or_stale_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = FakeCloseLLM()
    eng = await _engine(monkeypatch, llm, session_close_v2=True, profile_card_v2=True)
    try:
        await _send(eng, "s1", _turns(4))
        await eng.close_session("a", "s1")
        # the benchmark-era turn dates (2023) are far past the stale limit relative to now
        out = await eng.sleep()
        stats = out["reflect_profile"]["profile_card"]
        assert stats["reflection_items"] == 1 and "stale" in stats["reflection_reasons"]
    finally:
        await eng.stop()


async def test_sleep_without_the_card_flag_has_no_card_stats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = FakeCloseLLM()
    eng = await _engine(monkeypatch, llm, session_close_v2=True)
    try:
        await _send(eng, "s1", _turns(2))
        await eng.close_session("a", "s1")
        out = await eng.sleep()
        assert "profile_card" not in out["reflect_profile"]
    finally:
        await eng.stop()


def test_card_serialisation_round_trips() -> None:
    from memspine.derive.profile import ProfileEntry, SupportRef

    t = T0
    card = ProfileCard(
        "a",
        "a",
        (
            ProfileEntry(
                section="preferences",
                key="tea",
                value="tea",
                support=(SupportRef("r1", 0.8),),
                restated=2,
                first_confirmed=t,
                last_confirmed=t,
            ),
        ),
    )
    assert profile_card.card_from_dict(profile_card.card_to_dict(card), "a", "a") == card


async def test_a_repeat_of_the_same_texts_is_not_merged_away(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FU-CLOSE: session_close records take the plain WRITE route (no semantic M5 dedup)."""
    llm = FakeCloseLLM()
    eng = await _engine(monkeypatch, llm, session_close_v2=True)
    try:
        await _send(eng, "s1", _turns(2))
        await eng.close_session("a", "s1")
        await _send(eng, "s1", _turns(1, start=2))
        hook = (await eng.close_session("a", "s1"))["hook"]  # the fake repeats its texts
        assert hook["status"] == "ok" and hook["not_written"] == 0
        assert len(await _derived(eng)) == 4  # both versions kept, nothing merged away
    finally:
        await eng.stop()


def _events(traces: list[dict[str, Any]], stage: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in traces:
        out += [e for e in t["events"] if e["stage"] == stage]
        out += _events(t["children"], stage)
    return out


async def test_tap_events_for_call2_and_profile_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    from memspine.config.schema.observability import TapConfig
    from memspine.observability import tap

    tap.shutdown()
    tap.configure(TapConfig(enabled=True, sink="memory"))
    llm = FakeCloseLLM()
    eng = await _engine(monkeypatch, llm, session_close_v2=True, profile_card_v2=True)
    try:
        await _send(eng, "s1", _turns(3))
        await eng.close_session("a", "s1")
        await eng.sleep()
        traces = tap.memory_traces()
    finally:
        await eng.stop()
        tap.shutdown()
    call2 = _events(traces, "job.close.call2")
    assert len(call2) == 1
    f = call2[0]["fields"]
    assert f["status"] == "ok" and f["episodes"] == 1 and f["written"] == 2 and f["rejected"] == 0
    ops = _events(traces, "job.profile.ops")
    assert ops and ops[0]["fields"]["accepted"] == 1 and ops[0]["fields"]["evicted"] == 0
    assert _events(traces, "job.profile.invalidate")
    # no entry text in any event
    assert not any("sugar" in str(e) for t in traces for e in t["events"])
