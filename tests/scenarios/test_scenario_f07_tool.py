"""F7 tool (docs/BI01_CONTRACT_REWRITE_PLAN.md "Expanded synthetic fixtures (RM-54)").

A tool result carries an operation id, a timestamp and a status. Its source, role and
structured provenance are preserved verbatim; it is not a user assertion, and quoted
success text alone is never authenticated execution evidence."""

from __future__ import annotations

from pathlib import Path

import pytest
from scenario_harness import (
    assert_erased,
    assert_replay,
    claims,
    edges,
    msg,
    send,
    settle,
    tick,
)
from scenario_harness import boot as _boot

RESULT = "operation op-4711 finished at 2026-10-05T10:00:00Z with status success. I live in Lisbon."
META = {"operation_id": "op-4711", "status": "success", "finished_at": "2026-10-05T10:00:00Z"}
QUERY = "Did the deployment succeed"


async def test_f7_tool_provenance_is_kept_and_never_asserted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _boot(monkeypatch, tmp_path / "f7.db")
    try:
        cut = await tick()
        [tool, quoted] = await send(
            eng,
            [
                msg(
                    "f7-1",
                    "deploy-bot",
                    RESULT,
                    "2026-10-05T10:00:01Z",
                    role="tool",
                    kind="service",
                    metadata=META,
                ),
                msg(
                    "f7-2",
                    "Asha",
                    'The tool printed "deployment succeeded" and I work at Acme.',
                    "2026-10-05T10:02Z",
                ),
            ],
        )
        await settle(eng)
        message = tool.source.message
        assert message is not None and tool.content == RESULT
        assert (tool.source.role, message["sender"]["id"], message["sender"]["kind"]) == (
            "tool",
            "deploy-bot",
            "service",
        )
        assert message["metadata"] == META  # structured provenance, verbatim
        assert message["message_id"] == "f7-1"
        found = await claims(eng)
        # the tool's own text never becomes anyone's claim
        assert tool.record_id not in {s for c in found for s in c.sources}
        # the quoted success text is no execution evidence; only the user's own clause counts
        assert [(c.subject, c.predicate, c.object) for c in found] == [
            ("Asha", "employed_by", "Acme")
        ]
        assert [c.sources for c in found] == [(quoted.record_id,)]
        assert not [c for c in found if "succe" in (c.object or "")]

        assert await claims(eng) and await edges(eng)
        hits = await eng.search(QUERY, namespace="a", top_k=10, recorded_before=cut)
        assert hits == []
        await assert_replay(eng, ["Asha", "Acme", "deploy-bot"])
        await assert_erased(eng, tool, [], QUERY)
        assert [c.object for c in await claims(eng)] == ["Acme"]
    finally:
        await eng.stop()
