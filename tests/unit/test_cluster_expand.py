"""N06 (plan v3.2): ``read.cluster_expand`` pulls the neighbourhood of the top hits."""

from __future__ import annotations

from typing import Any

from memspine import Engine
from memspine.config.schema import ReadConfig

TURNS = [
    "Melanie: the pottery class on Friday was relaxing",
    "Melanie: my clay bowl from the studio cracked in the kiln glaze firing",
    "Melanie: kiln glaze firing takes all night at the studio",
    "Caroline: I joined a support group",
    "Caroline: the museum trip was fun",
]


async def _contents(**read: Any) -> list[str]:
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
        for text in TURNS:
            await eng.write(text, namespace="a", memory_type="episodic")
        out = await eng.read("How was the pottery class?", namespace="a", mode="retrieve", top_k=3)
        return [r.content for r in out.context.records]
    finally:
        await eng.stop()


def test_cluster_expand_defaults_off() -> None:
    assert ReadConfig().cluster_expand is False


async def test_cluster_expand_read_is_well_formed() -> None:
    on = await _contents(cluster_expand=True)
    assert TURNS[0] in on
    assert len(on) <= 3
