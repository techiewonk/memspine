"""E04 attachment identity through the loader and the adapter, E03 in the reader wrappers,
I61 premise-tolerant clause. Fakes only: no network, no model."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from memspine_evals.contracts import ReaderAnswer, Turn
from memspine_evals.datasets import LoCoMoDataset
from memspine_evals.no_record import NO_RECORD_NOTE, NoRecordHintReader, strip_public_knowledge
from memspine_evals.premise import PREMISE_NOTE, PremiseTolerantReader
from memspine_evals.systems.memspine_system import MemspineSystem

URL = "https://i.example/pic.jpg"

FIXTURE = [
    {
        "sample_id": "c1",
        "conversation": {
            "speaker_a": "Ana",
            "speaker_b": "Ben",
            "session_1_date_time": "2:00 pm on 8 May, 2023",
            "session_1": [
                {"speaker": "Ana", "dia_id": "D1:1", "text": "No picture here."},
                {
                    "speaker": "Ana",
                    "dia_id": "D1:2",
                    "text": "Look at this.",
                    "img_url": [URL, "https://i.example/two.jpg"],
                    "blip_caption": "a photo of a book",
                    "query": "children's classic cover",
                    "re-download": True,
                },
            ],
        },
        "qa": [{"question": "What book?", "answer": "x", "evidence": ["D1:2"], "category": 1}],
    }
]


def test_loader_preserves_attachment_identity_and_keeps_text_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "locomo.json"
    path.write_text(json.dumps(FIXTURE), encoding="utf-8")
    item = next(LoCoMoDataset(path, revision_id="fixture").items())
    plain, pic = item.history
    assert plain.meta == {}
    assert pic.text == "Look at this. [image: a photo of a book]"  # the caption text is as before
    att = pic.meta["attachments"]
    assert [a["uri"] for a in att] == [URL, "https://i.example/two.jpg"]
    assert att[0]["caption"] == "a photo of a book"
    assert att[0]["search_hint"] == "children's classic cover"  # audit only
    assert att[0]["re_download"] is True


_HASH = {"embedding": {"provider": "hash"}, "dotenv_path": None}


def _pic_turn() -> Turn:
    return Turn(
        turn_id="D1:2",
        session_id="s1",
        speaker="Ana",
        text="Look at this.",
        meta={"attachments": [{"uri": URL, "caption": "c", "search_hint": "h"}]},
    )


def test_message_carries_attachments_only_when_ingest_assets_is_on() -> None:
    off = MemspineSystem(config=_HASH)._message(_pic_turn(), "Ana: Look at this.")
    assert off == {"role": "user", "content": "Ana: Look at this."}
    on = MemspineSystem(config={**_HASH, "ingest": {"assets": "on"}})._message(
        _pic_turn(), "Ana: Look at this."
    )
    assert on["turn_id"] == "D1:2" and on["attachments"][0]["uri"] == URL
    plain = Turn(turn_id="D1:1", session_id="s1", speaker="Ana", text="hi")
    assert MemspineSystem(config={**_HASH, "ingest": {"assets": "on"}})._message(
        plain, "Ana: hi"
    ) == {"role": "user", "content": "Ana: hi"}


class _Echo:
    reader_id = "echo"
    model = "none"
    makes_model_calls = False

    def __init__(self) -> None:
        self.seen: list[str] = []

    def describe(self) -> dict[str, Any]:
        return {"reader_id": "echo"}

    async def answer(
        self, question: str, context: str, question_date: str | None = None
    ) -> ReaderAnswer:
        self.seen.append(context)
        return ReaderAnswer(text="ok")


# ── E03: public knowledge never supports a remembered event ──────────────────

PUBLIC = (
    "[2023-05-08] [public knowledge] General background. It never shows that any person did "
    "anything.\n- Lisbon: a city in Portugal that many tourists visit (https://x.test)"
)


def test_strip_public_knowledge_removes_only_the_block() -> None:
    ctx = f"[2023-05-08] Ana: I like tea\n{PUBLIC}\n[2023-05-09] Ben: hello"
    out = strip_public_knowledge(ctx)
    assert "Lisbon" not in out and "public knowledge" not in out
    assert "Ana: I like tea" in out and "Ben: hello" in out
    assert strip_public_knowledge("plain context") == "plain context"


def test_public_text_cannot_make_an_asserted_past_event_look_supported() -> None:
    inner = _Echo()
    reader = NoRecordHintReader(inner)
    q = "Do you remember when I visited Lisbon?"
    asyncio.run(reader.answer(q, f"[2023-05-09] Ben: hello\n{PUBLIC}"))
    assert inner.seen[-1].startswith(NO_RECORD_NOTE)  # the Lisbon background did not count
    asyncio.run(reader.answer(q, "[2023-05-09] Ana: my visit to Lisbon was lovely"))
    assert NO_RECORD_NOTE not in inner.seen[-1]


# ── I61 ──────────────────────────────────────────────────────────────────────


def test_premise_clause_is_added_to_a_nonempty_context_only() -> None:
    inner = _Echo()
    reader = PremiseTolerantReader(inner)
    out = asyncio.run(
        reader.answer("When did Ana adopt the dog in 2021?", "[2022-01-01] Ana adopted")
    )
    assert inner.seen[-1].startswith(PREMISE_NOTE) and out.extra_meta["premise_tolerant"]
    asyncio.run(reader.answer("Who?", "  "))
    assert PREMISE_NOTE not in inner.seen[-1]
    assert reader.applied == 1
    assert reader.describe()["premise_tolerant"] is True
    assert reader.reader_id == "echo+premise"


def test_premise_clause_keeps_both_halves_and_no_benchmark_words() -> None:
    low = PREMISE_NOTE.lower()
    assert "do not simply confirm" in low and "do not refuse because one detail differs" in low
    assert "not the one in the memories" in low  # a wrong subject is still reported as absent
    for name in ("locomo", "longmemeval", "op-bench", "cat 5", "category"):
        assert name not in low


def test_premise_and_no_record_compose() -> None:
    inner = _Echo()
    reader = PremiseTolerantReader(NoRecordHintReader(inner))
    asyncio.run(reader.answer("Do you remember when I climbed Everest?", "[2024-01-02] I like tea"))
    assert PREMISE_NOTE in inner.seen[0] and NO_RECORD_NOTE in inner.seen[0]
    assert reader.describe()["no_record_hint"] is True
