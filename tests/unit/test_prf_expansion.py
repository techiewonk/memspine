"""N03 (plan v3.2): ``read.prf_expansion`` adds a feedback probe to the search."""

from __future__ import annotations

from typing import Any

from memspine import Engine
from memspine.config.schema import ReadConfig


async def _read(**read: Any) -> list[str]:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"hybrid": False, "record_access": False, **read},
    )
    await eng.start()
    try:
        for text in [
            "Melanie: we went camping at the lake with the tent",
            "Melanie: the tent leaked at the lake again",
            "Melanie: next summer the lake trip again, new tent",
            "Caroline: my painting class starts Monday",
        ]:
            await eng.write(text, namespace="a", memory_type="episodic")
        out = await eng.read(
            "Where did Melanie go camping?", namespace="a", mode="retrieve", top_k=3
        )
        return [r.content for r in out.context.records]
    finally:
        await eng.stop()


def test_prf_expansion_defaults_off() -> None:
    assert ReadConfig().prf_expansion is False


async def test_prf_expansion_runs_and_keeps_the_read_well_formed() -> None:
    on = await _read(prf_expansion=True)
    assert on and len(on) <= 3
    assert any("tent" in c for c in on)
