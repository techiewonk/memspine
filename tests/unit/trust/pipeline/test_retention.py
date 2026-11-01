"""``memspine.trust.pipeline.retention``: retention planning and the expiry pass."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from memspine import Engine
from memspine.trust.pipeline import retention


def _engine(classes: list[dict[str, Any]], **extra: Any) -> Engine:
    base: dict[str, Any] = {
        "template": "core",
        "dotenv_path": None,
        "storage": {
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        "model": {"embedding": {"provider": "hash", "settings": {}}},
        "read": {"hybrid": False},
        "memories": {
            "semantic": {
                "enabled": True,
                "policies": {"retention": {"legal_hold_namespaces": ["tmp/held"]}},
            }
        },
        "retention": {"classes": classes},
    }
    base.update(extra)
    return Engine(**base)


def _later(days: float) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


async def test_boundary_is_none_before_the_first_pass() -> None:
    eng = await _engine([]).start()
    try:
        assert retention.boundary(eng, "tmp/a") is None
    finally:
        await eng.stop()


async def test_expire_forgets_records_past_their_class_ttl_and_keeps_the_rest() -> None:
    eng = await _engine([{"namespace": "tmp/*", "ttl_days": 7}]).start()
    try:
        old = await eng.write("scratch note", namespace="tmp/a")
        kept = await eng.write("durable note", namespace="keep")
        held = await eng.write("held note", namespace="tmp/held")
        stats = await retention.expire(eng, _later(8))
        assert stats["status"] == "ok"
        assert stats["expired"] == 1 and stats["tombstones"] == 1
        assert stats["held"] == 1 and stats["held_by_reason"] == {"legal_hold": 1}
        assert stats["profile"] == "config" and stats["rebuild"] == "full"
        assert "errors" not in stats
        storage = eng._require_started()
        assert await storage.get_record(old.record_id) is None
        assert await storage.get_record(kept.record_id) is not None
        assert await storage.get_record(held.record_id) is not None
        assert retention.boundary(eng, "tmp/a") is not None
    finally:
        await eng.stop()


async def test_nothing_expires_before_the_ttl() -> None:
    eng = await _engine([{"namespace": "tmp/*", "ttl_days": 7}]).start()
    try:
        rec = await eng.write("scratch note", namespace="tmp/a")
        stats = await retention.expire(eng, _later(6))
        assert stats["expired"] == 0
        assert await eng._require_started().get_record(rec.record_id) is not None
    finally:
        await eng.stop()


async def test_the_first_matching_class_sets_the_ttl() -> None:
    classes = [
        {"namespace": "tmp/*", "memory_type": "semantic", "ttl_days": 1},
        {"namespace": "*", "ttl_days": 30},
    ]
    eng = await _engine(classes).start()
    try:
        short = await eng.write("short lived", namespace="tmp/a")
        long = await eng.write("long lived", namespace="other")
        storage = eng._require_started()
        now = _later(2)
        plans, class_of = retention.plan_namespace(
            eng, "tmp/a", await storage.list_records("tmp/a"), now
        )
        assert class_of == {short.record_id: "class0"}
        assert [e.record_id for p in plans for e in p.expire] == [short.record_id]
        plans, class_of = retention.plan_namespace(
            eng, "other", await storage.list_records("other"), now
        )
        assert class_of == {}
        assert [r for p in plans for r in p.retained] == [long.record_id]
    finally:
        await eng.stop()


async def test_plan_namespace_without_records_still_plans_and_records_a_boundary() -> None:
    eng = await _engine([]).start()
    try:
        plans, class_of = retention.plan_namespace(eng, "empty", [], _later(0))
        assert len(plans) == 1 and class_of == {}
        assert not plans[0].expire
        assert retention.boundary(eng, "empty") == plans[0].boundary
    finally:
        await eng.stop()


async def test_shared_records_are_never_planned() -> None:
    eng = await _engine([{"namespace": "*", "ttl_days": 1}]).start()
    try:
        rec = await eng.write("a note", namespace="n")
        shared = rec.model_copy(update={"memory_type": "shared"})
        plans, class_of = retention.plan_namespace(eng, "n", [shared], _later(5))
        assert class_of == {}
        assert all(not p.expire and not p.retained and not p.held for p in plans)
    finally:
        await eng.stop()


async def test_turn_layer_is_protected_under_a_full_event_log() -> None:
    eng = await _engine([{"namespace": "*", "ttl_days": 1}]).start()
    try:
        rec = await eng.write("a turn", namespace="n")
        turn = rec.model_copy(update={"layer": "L0"})
        plans, class_of = retention.plan_namespace(eng, "n", [turn], _later(5))
        assert class_of == {}
        assert sum(p.l0_protected for p in plans) == 1
    finally:
        await eng.stop()
