"""R14-INDEXVIEW: ``write.index_view`` builds a deterministic index text per turn (model-free).

The text rides in the WRITE event payload; the record, ids, order and display text do not change,
and with the flag off the payload is byte-identical.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

from memspine import Engine
from memspine.ingest.index_view import (
    build_index_prefix,
    compose_index_text,
    elliptical_score,
    resolve_relative,
)
from memspine.store.events import EventKind

NS = "ix"
ANCHOR = date(2023, 5, 8)  # a Monday
TURNS = [
    {
        "role": "user",
        "speaker": "Ilse",
        "content": "Bram and I hiked the Tarn ridge yesterday and it was lovely",
        "timestamp": "2023-05-08T10:00:00",
    },
    {
        "role": "assistant",
        "speaker": "Bram",
        "content": "Yes, that one!",
        "timestamp": "2023-05-08T10:01:00",
    },
]


# ---- the rules


def test_relative_dates_resolve_from_the_anchor_not_the_clock() -> None:
    out = resolve_relative("we met yesterday, and again last Friday; two weeks ago too", ANCHOR)
    assert "yesterday 7 May 2023" in out
    assert "last friday 5 May 2023" in out
    assert "two weeks ago 24 April 2023" in out
    assert resolve_relative("last month", ANCHOR) == ["last month April 2023"]
    assert resolve_relative("last year", ANCHOR) == ["last year 2022"]
    assert resolve_relative("no time words here", ANCHOR) == []


def test_prefix_has_speaker_date_month_weekday_and_resolved_dates() -> None:
    prefix = build_index_prefix(
        speaker="Ilse",
        stamp=datetime(2023, 5, 8, 10),
        content="We went hiking yesterday",
        prev_content=None,
        prev_speaker=None,
        participants=["Ilse", "Bram"],
    )
    assert prefix.startswith("Ilse: 8 May 2023 May Monday")
    assert "yesterday 7 May 2023" in prefix
    assert "with Ilse Bram" in prefix  # "We": everyone


def test_undated_turn_gets_no_date_tokens() -> None:
    prefix = build_index_prefix(
        speaker="Ilse",
        stamp=None,
        content="We went hiking yesterday",
        prev_content=None,
        prev_speaker=None,
        participants=[],
    )
    assert prefix == "Ilse:"


def test_elliptical_gate_and_previous_turn_words() -> None:
    assert elliptical_score("Yes, that one!") >= 2
    assert elliptical_score("Bram painted the harbour lighthouse in watercolour last summer") == 0
    prefix = build_index_prefix(
        speaker="Bram",
        stamp=None,
        content="Yes, that one!",
        prev_content="Shall we visit the Tarn ridge lighthouse?",
        prev_speaker="Ilse",
        participants=[],
    )
    assert "re: Ilse Shall visit Tarn ridge lighthouse" in prefix
    full = build_index_prefix(
        speaker="Bram",
        stamp=None,
        content="I repaired the old harbour lighthouse lamp yesterday",
        prev_content="Shall we visit the Tarn ridge lighthouse?",
        prev_speaker="Ilse",
        participants=[],
    )
    assert "re:" not in full


def test_extra_tokens_are_bounded_and_previous_words_are_cut_first() -> None:
    prefix = build_index_prefix(
        speaker="Bram",
        stamp=datetime(2023, 5, 8),
        content="Yes, that one!",
        prev_content=" ".join(f"Wordnumber{i}" for i in range(60)),
        prev_speaker="Ilse",
        participants=[],
        max_extra_tokens=12,
    )
    assert len(prefix.split()) <= 12
    assert prefix.startswith("Bram: 8 May 2023")


def test_compose_adds_nothing_for_an_empty_prefix() -> None:
    assert compose_index_text("", "hello") is None
    assert compose_index_text("Ilse:", "hello") == "Ilse:\nhello"


# ---- the engine


def _engine(path: Path, **write: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": str(path / "m.db")},
        embedding={"provider": "hash"},
        read={"hybrid": True, "record_access": False},
        write=write,
    )


async def _payloads(eng: Engine) -> list[dict[str, Any]]:
    events = await eng._require_started().read_events(0, 100)
    return [e.payload for e in events if e.kind is EventKind.WRITE]


async def test_flag_on_stores_the_index_text_and_keeps_the_display_text(tmp_path: Path) -> None:
    (tmp_path / "off").mkdir()
    off = _engine(tmp_path / "off", index_view=False)
    on = _engine(tmp_path, index_view=True)
    await off.start()
    await on.start()
    try:
        rec_off = await off.write_messages(TURNS, namespace=NS, session_id="s1")
        rec_on = await on.write_messages(TURNS, namespace=NS, session_id="s1")
        pay_off = await _payloads(off)
        pay_on = await _payloads(on)
        assert all("index_text" not in p for p in pay_off)
        assert [r.content for r in rec_on] == [r.content for r in rec_off]
        assert [r.content for r in rec_on] == [t["content"] for t in TURNS]
        texts = [p.get("index_text") for p in pay_on]
        assert texts[0] is not None and texts[0].endswith("\n" + TURNS[0]["content"])
        assert "yesterday 7 May 2023" in texts[0] and texts[0].startswith("Ilse: 8 May 2023")
        assert texts[1] is not None and "re:" in texts[1] and "Tarn" in texts[1]  # elliptical reply
        # the stored record is the display text
        stored = await on._require_started().list_records(NS, "episodic")
        assert sorted(r.content for r in stored) == sorted(t["content"] for t in TURNS)
        # the lexical index sees the date words that the turn itself never said
        hits = await on._lexical.search(NS, "Monday 2023", top_k=5)  # type: ignore[union-attr]
        assert {h.record_id for h in hits} == {r.record_id for r in rec_on}
        none = await off._lexical.search(NS, "Monday", top_k=5)  # type: ignore[union-attr]
        assert none == []
    finally:
        await off.stop()
        await on.stop()
