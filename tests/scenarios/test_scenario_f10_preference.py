"""F10 preference (docs/BI01_CONTRACT_REWRITE_PLAN.md "Expanded synthetic fixtures (RM-54)").

Initially likes tea; later explicitly dislikes it. Both claims keep their time and authority
(the history is preserved), and the later evidence is invisible to an earlier known-time or
as-of read."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from scenario_harness import (
    assert_erased,
    assert_replay,
    by_predicate,
    claims,
    msg,
    read_claims,
    semantic_hits,
    send,
    settle,
    tick,
    unresolved,
)
from scenario_harness import boot as _boot

QUERY = "Does Mira like tea"


async def test_f10_preference_history_known_time_replay_and_erasure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f10.db")
    try:
        [liked] = await send(eng, [msg("p-1", "Mira", "I like tea.", "2026-03-01T09:00Z")])
        cut = await tick()
        [disliked] = await send(eng, [msg("p-2", "Mira", "I dislike tea.", "2026-09-01T09:00Z")])
        await settle(eng)
        found = await claims(eng)
        [like] = by_predicate(found, "likes")
        [dislike] = by_predicate(found, "dislikes")
        assert (like.object, dislike.object) == ("tea", "tea")
        assert like.principal == dislike.principal == "Mira"
        assert like.sources == (liked.record_id,) and dislike.sources == (disliked.record_id,)
        assert like.valid_from == datetime(2026, 3, 1, 9, tzinfo=UTC)
        assert dislike.valid_from == datetime(2026, 9, 1, 9, tzinfo=UTC)
        assert like.known_at is not None and dislike.known_at is not None
        assert like.known_at < cut < dislike.known_at
        # the single-valued attitude slot is not a registered predicate: unresolved, unasserted
        assert {(u.attribute, u.reason) for u in unresolved(eng)} == {
            ("attitude:tea", "unsupported_predicate")
        }

        # an earlier known-time read never sees the later dislike
        assert await semantic_hits(eng, QUERY, recorded_before=cut) == ["Mira likes tea"]
        assert await read_claims(eng, QUERY, recorded_before=cut) == ["Mira likes tea"]
        june = datetime(2026, 6, 1, tzinfo=UTC)
        assert await read_claims(eng, QUERY, as_of=june) == ["Mira likes tea"]
        assert sorted(await semantic_hits(eng, QUERY)) == ["Mira dislikes tea", "Mira likes tea"]

        await assert_replay(eng, ["Mira", "tea"])
        await assert_erased(eng, disliked, [dislike.record_id], QUERY)
        assert [c.predicate for c in await claims(eng)] == ["likes"]
    finally:
        await eng.stop()
