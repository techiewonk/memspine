"""G31 (plan v3.2): ``Engine.bulk_read_alerts`` over the read audit."""

from __future__ import annotations

from memspine import Engine


async def test_a_principal_reading_too_much_is_reported() -> None:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        audit={"reads": True},
        read={"hybrid": False},
    )
    await eng.start()
    try:
        for i in range(12):
            await eng.write(
                f"note number {i} about topic {i}", namespace="a", memory_type="episodic"
            )
        for i in range(12):
            await eng.read(f"topic {i}", namespace="a", mode="retrieve", top_k=3)
        alerts = await eng.bulk_read_alerts("a", max_records=5)
        assert alerts and alerts[0]["principal"] == "anonymous"
        assert int(alerts[0]["distinct_records"]) > 5  # type: ignore[call-overload]
        assert await eng.bulk_read_alerts("a", max_records=10_000) == []
    finally:
        await eng.stop()
