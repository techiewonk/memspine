"""W14 (plan v3.2): ``verify_forget(probe=...)`` proves erasure on recall."""

from __future__ import annotations

from memspine import Engine

SECRET = "My locker code at the gym is 4417 and the spare key is under the mat"


def _engine() -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"hybrid": False},
    )


async def test_without_a_probe_nothing_changes() -> None:
    eng = _engine()
    await eng.start()
    try:
        rec = await eng.write(SECRET, namespace="a", memory_type="episodic")
        await eng.forget(rec.record_id, namespace="a", hard=True)
        report = await eng.verify_forget(rec.record_id, namespace="a")
        assert report["residual_recall"] is None
    finally:
        await eng.stop()


async def test_probe_finds_a_copy_the_erasure_left_behind() -> None:
    eng = _engine()
    await eng.start()
    try:
        rec = await eng.write(SECRET, namespace="a", memory_type="episodic")
        copy = await eng.write(
            "Note to self: " + SECRET.lower(), namespace="a", memory_type="episodic"
        )
        await eng.write("We talked about the weather", namespace="a", memory_type="episodic")
        await eng.forget(rec.record_id, namespace="a", hard=True)
        report = await eng.verify_forget(rec.record_id, namespace="a", probe=SECRET)
        assert report["residual_recall"] == [copy.record_id]
        assert report["clean"] is False
        await eng.forget(copy.record_id, namespace="a", hard=True)
        again = await eng.verify_forget(rec.record_id, namespace="a", probe=SECRET)
        assert again["residual_recall"] == []
    finally:
        await eng.stop()
