"""Read-path fixes from the second gap pass (N3 durable rollback mark, N4 rendered budget)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from memspine import Engine
from memspine.core.policies.assembly import estimate_tokens
from memspine.core.records import RecordStatus

T0 = datetime(2023, 5, 7, 10, 0, tzinfo=UTC)


def _engine(read: dict[str, Any] | None = None, **extra: Any) -> Engine:
    memories = extra.pop("memories", {"semantic": {"enabled": True}, "episodic": {"enabled": True}})
    return Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories=memories,
        read={"hybrid": False, **(read or {})},
        **extra,
    )


# N3 --------------------------------------------------------------------------------


async def test_state_view_omits_rolled_back_poison_in_ephemeral_log_mode() -> None:
    eng = _engine(
        {"current_state_view": True},
        memories={"semantic": {"enabled": True}},
        event_log={"mode": "ephemeral"},
    )
    await eng.start()
    try:
        poison = await eng.write(
            "user lives in the city of Zorgville",
            namespace="a",
            entity="user",
            attribute="city",
            valid_from=T0,
        )
        await eng.rollback_taint(poison.record_id, namespace="a")
        archived = await eng._require_started().get_record(poison.record_id)
        assert archived is not None and archived.status is RecordStatus.ARCHIVED
        assert archived.valid_to is not None  # would qualify as HISTORY by valid_to alone
        await eng.write(
            "user lives in the city of Lyon",
            namespace="a",
            entity="user",
            attribute="city",
            valid_from=T0 + timedelta(days=3),
        )
        ctx = await eng.assemble("which city does the user live in", namespace="a")
        text = "\n".join(r.content for r in ctx.records)
        assert "Lyon" in text
        assert "Zorgville" not in text
    finally:
        await eng.stop()


# N4 --------------------------------------------------------------------------------


async def _hikes(eng: Engine, n: int = 6) -> None:
    for i in range(n):
        await eng.write(
            f"went hiking on trail number {i} with friends",
            namespace="a",
            memory_type="episodic",
            valid_from=T0 + timedelta(days=21 * i),
        )


async def test_dated_render_and_gap_markers_are_counted_and_kept_in_budget() -> None:
    reserve = 10
    budget = 50
    eng = _engine({"render": "dated", "gap_markers": True, "reply_reserve_tokens": reserve})
    await eng.start()
    try:
        await _hikes(eng)
        ctx = await eng.assemble("hiking trail", namespace="a", budget_tokens=budget, top_k=6)
        assert ctx.records
        assert any(r.content.startswith("[3 weeks later]") for r in ctx.records)
        rendered = sum(estimate_tokens(r.content) for r in ctx.records)
        assert ctx.tokens_used == rendered
        assert ctx.tokens_used <= budget - reserve
        for mode in ("retrieve", "replay", "compose"):
            out = await eng.read("hiking trail", namespace="a", mode=mode, budget_tokens=budget)
            used = sum(estimate_tokens(r.content) for r in out.context.records)
            assert out.context.tokens_used == used, mode
            assert used <= budget - reserve, mode
    finally:
        await eng.stop()


async def test_full_mode_falls_back_when_the_dated_render_overflows() -> None:
    eng = _engine({"render": "dated", "gap_markers": True})
    await eng.start()
    try:
        await _hikes(eng, n=3)
        plain = sum(
            estimate_tokens(f"went hiking on trail number {i} with friends") for i in range(3)
        )
        out = await eng.read("hiking trail", namespace="a", mode="auto", budget_tokens=plain)
        assert out.mode != "full"
        used = sum(estimate_tokens(r.content) for r in out.context.records)
        assert out.context.tokens_used == used <= plain
    finally:
        await eng.stop()


async def test_plain_render_budget_accounting_is_unchanged() -> None:
    eng = _engine({"reply_reserve_tokens": 10})
    await eng.start()
    try:
        await _hikes(eng)
        ctx = await eng.assemble("hiking trail", namespace="a", budget_tokens=50, top_k=6)
        assert all(not r.content.startswith("[") for r in ctx.records)
        assert ctx.tokens_used == sum(estimate_tokens(r.content) for r in ctx.records)
        assert ctx.tokens_used <= 40
    finally:
        await eng.stop()
