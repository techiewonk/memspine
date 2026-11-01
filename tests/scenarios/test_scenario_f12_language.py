"""F12 language (docs/BI01_CONTRACT_REWRITE_PLAN.md "Expanded synthetic fixtures (RM-54)").

Non-English text with stable message ids and explicit replies keeps its original text, ids,
exact spans and reply links. The rules are English-only: an unsupported language stays
unresolved (no claim), never a mistranslated fact; an English clause inside mixed text is
cited by its exact code-point span in the original string."""

from __future__ import annotations

from pathlib import Path

import pytest
from scenario_harness import (
    assert_erased,
    assert_replay,
    claims,
    edges,
    msg,
    semantic_hits,
    send,
    settle,
    tick,
)
from scenario_harness import boot as _boot

PT = "Eu moro em Lisboa desde 2020, perto do Tejo."
PT_REPLY = "Que ótimo! Também gosto muito de café."
MIXED = "Olá a todos! I live in Évora."
QUERY = "Onde mora a Inês"


async def test_f12_original_text_ids_spans_and_links(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f12.db")
    try:
        cut = await tick()
        pt, reply, mixed = await send(
            eng,
            [
                msg("pt-1", "Inês", PT, "2026-10-01T09:00:00+01:00"),
                msg("pt-2", "João", PT_REPLY, "2026-10-01T09:01:00+01:00", reply_to="pt-1"),
                msg("pt-3", "Inês", MIXED, "2026-10-01T09:02:00+01:00", reply_to="pt-2"),
            ],
        )
        await settle(eng)
        # original text, stable ids and explicit reply links, byte for byte
        for rec, text, mid, parent in (
            (pt, PT, "pt-1", None),
            (reply, PT_REPLY, "pt-2", "pt-1"),
            (mixed, MIXED, "pt-3", "pt-2"),
        ):
            assert rec.content == text and rec.source.message is not None
            assert rec.source.message["message_id"] == mid
            assert rec.source.message.get("reply_to") == parent
        found = await claims(eng)
        # Portuguese clauses are not translated into facts (no residence in Lisboa, no likes)
        assert [(c.subject, c.predicate, c.object) for c in found] == [
            ("Inês", "resides_in", "Évora")
        ]
        [(source_id, start, end, quote)] = found[0].evidence
        assert source_id == mixed.record_id
        assert MIXED[start:end] == quote == "I live in Évora."
        assert {c.sources for c in found} == {(mixed.record_id,)}

        assert await semantic_hits(eng, QUERY, recorded_before=cut) == []
        await assert_replay(eng, ["Inês", "Évora", "Lisboa"])
        await assert_erased(eng, mixed, [found[0].record_id], QUERY)
        assert await claims(eng) == [] and await edges(eng) == []
        # the Portuguese sources and their thread stay intact
        assert [r.content for r in await eng.list_records("a", "episodic")] == [PT, PT_REPLY]
    finally:
        await eng.stop()
