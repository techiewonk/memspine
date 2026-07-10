"""A5 reinforcement-on-read: each retrieval bumps a record's utility (salience),
clamped, and the bump rides the RETRIEVE event so a rebuild reproduces it."""

from __future__ import annotations

import pytest

from memspine import Engine
from memspine.config import constants


def _engine() -> Engine:
    return Engine(
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"semantic": {"enabled": True}},
    )


async def test_retrieval_bumps_utility_and_clamps() -> None:
    eng = _engine()
    await eng.start()
    try:
        rec = await eng.write("the sky is blue today")
        storage = eng._storage
        assert storage is not None

        before = await storage.get_record(rec.record_id)
        assert before is not None and before.scoring.utility == 0.0

        await eng.search("the sky is blue today")  # RETRIEVE → utility += step
        after1 = await storage.get_record(rec.record_id)
        assert after1 is not None
        assert after1.scoring.utility == pytest.approx(constants.RETRIEVE_UTILITY_STEP)
        assert after1.scoring.access_count == 1  # existing access stat still advances

        await eng.search("the sky is blue today")  # second retrieval → 2*step
        after2 = await storage.get_record(rec.record_id)
        assert after2 is not None
        assert after2.scoring.utility == pytest.approx(2 * constants.RETRIEVE_UTILITY_STEP)

        # Clamp: many retrievals never exceed MAX.
        for _ in range(20):
            await eng.search("the sky is blue today")
        capped = await storage.get_record(rec.record_id)
        assert capped is not None
        assert capped.scoring.utility == pytest.approx(constants.RETRIEVE_UTILITY_MAX)
    finally:
        await eng.stop()


async def test_reinforcement_survives_rebuild() -> None:
    eng = _engine()
    await eng.start()
    try:
        rec = await eng.write("green grass grows")
        await eng.search("green grass grows")
        await eng.search("green grass grows")
        storage = eng._storage
        assert storage is not None
        pre = await storage.get_record(rec.record_id)
        assert pre is not None

        await eng.rebuild()  # replay the log from seq 0 — RETRIEVE bumps re-apply
        post = await storage.get_record(rec.record_id)
        assert post is not None
        assert post.scoring.utility == pytest.approx(pre.scoring.utility)
    finally:
        await eng.stop()
