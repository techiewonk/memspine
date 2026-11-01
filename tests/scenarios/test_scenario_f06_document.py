"""F6 document (docs/BI01_CONTRACT_REWRITE_PLAN.md "Expanded synthetic fixtures (RM-54)").

A release note states that project X requires version 4. The statement is the document's,
pinned to its source; it is never a user assertion and never demonstrated compatibility. A
user who reports what the note says asserts nothing either."""

from __future__ import annotations

from pathlib import Path

import pytest
from scenario_harness import (
    assert_erased,
    assert_replay,
    by_predicate,
    claims,
    msg,
    semantic_hits,
    send,
    settle,
    tick,
    unresolved,
)
from scenario_harness import boot as _boot

NOTE = "Release 4.0: Project X requires version 4 of the runtime."
QUERY = "What does project X require"


async def test_f6_document_statement_is_never_a_user_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    note_path = tmp_path / "RELEASE-4.0.md"
    note_path.write_text(NOTE, encoding="utf-8")
    eng = await _boot(monkeypatch, tmp_path / "f6.db", resource=True)
    try:
        cut = await tick()
        [chunk] = await eng.ingest_data(note_path, namespace="a")
        [report] = await send(
            eng,
            [
                msg(
                    "f6-2",
                    "Asha",
                    "The release note says project X requires version 4.",
                    "2026-10-05T10:01Z",
                )
            ],
        )
        await settle(eng)
        # the statement is pinned to its document chunk, verbatim
        assert chunk.memory_type == "resource" and chunk.content.strip() == NOTE
        assert chunk.source.doc_path is not None
        assert chunk.source.doc_path.endswith("RELEASE-4.0.md")
        assert (chunk.source.channel, chunk.source.message_id) == ("ingest", "chunk:0")
        # no user assertion and no demonstrated compatibility from either source
        assert await claims(eng) == []
        assert report.record_id not in {u.source.record_id for u in unresolved(eng)}
        assert await semantic_hits(eng, QUERY) == []
        assert await semantic_hits(eng, QUERY, recorded_before=cut) == []

        await assert_replay(eng, ["release-notes", "Project X"])
        await assert_erased(eng, chunk, [], QUERY)
        hits = await eng.search(QUERY, namespace="a", top_k=10)
        assert [r.record_id for r, _ in hits] == [report.record_id]  # only the user's turn
    finally:
        await eng.stop()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "RM-54: documents are not derived by the rules-only path (ingest_data writes resource "
        "chunks only, and source_ineligible in derive/non_generative_facts.py drops every "
        "non-user source), so the note never yields a reported 'requires' claim attributed "
        "to the document chunk"
    ),
)
async def test_f6_document_claim_is_reported_and_attributed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    note_path = tmp_path / "RELEASE-4.0.md"
    note_path.write_text(NOTE, encoding="utf-8")
    eng = await _boot(monkeypatch, tmp_path / "f6x.db", resource=True)
    try:
        [chunk] = await eng.ingest_data(note_path, namespace="a")
        await settle(eng)
        [claim] = by_predicate(await claims(eng), "requires")
        assert claim.modality == "reported" and claim.principal != "user"
        assert claim.sources == (chunk.record_id,)
    finally:
        await eng.stop()
