"""N13 (plan v3.2): ``read.focused_excerpt`` in the read path."""

from __future__ import annotations

from typing import Any

from memspine import Engine
from memspine.config.schema import ReadConfig
from memspine.core.excerpt import ELLIPSIS

NOTE = "\n".join(
    [
        "Weekly team meeting notes",
        "Attendees: Ana, Ben, Chloe",
        "Budget review: marketing spend is up 10 percent",
        "The new office lease starts in March",
        "Lease deposit is 4,000 euros, paid by the company",
        "Hiring: two backend roles open",
        "Next meeting on Friday",
    ]
)


async def _read(query: str, **read: Any) -> tuple[str, str]:
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
        rec = await eng.write(NOTE, namespace="a", memory_type="episodic")
        out = await eng.read(query, namespace="a", mode="retrieve", top_k=1)
        stored = await eng._require_started().get_record(rec.record_id)
        assert stored is not None
        return out.context.records[0].content, stored.content
    finally:
        await eng.stop()


def test_focused_excerpt_defaults_off() -> None:
    assert ReadConfig().focused_excerpt is False


async def test_read_shows_an_excerpt_and_keeps_the_stored_record() -> None:
    shown, stored = await _read("When does the office lease start?", focused_excerpt=True)
    assert ELLIPSIS in shown and "lease starts in March" in shown
    assert stored == NOTE


async def test_off_or_verbatim_shows_the_whole_record() -> None:
    assert (await _read("When does the office lease start?"))[0] == NOTE
    verbatim = "What did the meeting notes say about the lease?"
    assert (await _read(verbatim, focused_excerpt=True))[0] == NOTE
