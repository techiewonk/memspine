"""C8': firewall-governed anticipatory cues (retrieval keys, never content)."""

from __future__ import annotations

from typing import Any

from memspine import Engine
from memspine.core.records import SourceInfo

FACT = "The quarterly offsite is booked at the lakeside lodge"
CUE = "where are we going for the team retreat trip"
FILLER = [f"note {i}: routine standup about ticket backlog" for i in range(15)]


def _engine(**read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"semantic": {"enabled": True}},
        read={"hybrid": False, **read},
    )


async def _seed(eng: Engine, cue_source: SourceInfo | None = None) -> tuple[str, list[Any]]:
    fact = await eng.write(FACT, namespace="a")
    for text in FILLER:
        await eng.write(text, namespace="a")
    cues = await eng.add_cues(fact.record_id, [CUE], namespace="a", source=cue_source)
    return fact.record_id, cues


async def test_cue_resolves_to_target_and_is_never_content() -> None:
    eng = _engine(anticipatory_cues=True)
    await eng.start()
    try:
        fact_id, _ = await _seed(eng)
        hits = await eng.search(CUE, namespace="a", top_k=2)
        assert hits[0][0].record_id == fact_id
        assert all("anticipatory_cue" not in r.tags for r, _ in hits)
        assert len({r.record_id for r, _ in hits}) == len(hits)
    finally:
        await eng.stop()


async def test_cues_off_are_invisible() -> None:
    eng = _engine()
    await eng.start()
    try:
        await _seed(eng)
        hits = await eng.search(CUE, namespace="a", top_k=5)
        assert all("anticipatory_cue" not in r.tags for r, _ in hits)
    finally:
        await eng.stop()


async def test_low_trust_cue_cannot_redirect_retrieval() -> None:
    eng = _engine(anticipatory_cues=True)
    await eng.start()
    try:
        _, cues = await _seed(eng, SourceInfo(role="tool", channel="web"))
        hits = await eng.search(CUE, namespace="a", top_k=2)
        # the external cue sits below cue_min_trust: plain ranking decides, and the
        # cue itself never surfaces
        assert all("anticipatory_cue" not in r.tags for r, _ in hits)
        assert cues and all(c.trust < 0.5 for c in cues)
    finally:
        await eng.stop()


async def test_cue_trust_capped_at_target() -> None:
    eng = _engine(anticipatory_cues=True)
    await eng.start()
    try:
        low = await eng.write(
            FACT, namespace="a", source=SourceInfo(role="assistant", channel="internal")
        )
        [cue] = await eng.add_cues(
            low.record_id, [CUE], namespace="a", source=SourceInfo(role="operator")
        )
        assert cue.trust <= low.trust
    finally:
        await eng.stop()
