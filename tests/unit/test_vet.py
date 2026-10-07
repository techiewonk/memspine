"""G26 (plan v3.2): ``Engine.vet(draft)`` flags sentences that contradict current facts."""

from __future__ import annotations

from datetime import UTC, datetime

from memspine import Engine

JAN = datetime(2023, 1, 10, tzinfo=UTC)
JUN = datetime(2023, 6, 10, tzinfo=UTC)


async def _engine() -> Engine:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"semantic": {"enabled": True}},
    )
    await eng.start()
    for text, when in (("Alice city: Berlin", JAN), ("Alice city: Munich", JUN)):
        await eng.write(
            text,
            namespace="a",
            memory_type="semantic",
            entity="Alice",
            attribute="city",
            valid_from=when,
        )
    return eng


async def test_vet_flags_an_old_value_and_a_negation() -> None:
    eng = await _engine()
    try:
        flags = await eng.vet(
            "Alice moved to a new city last year. Alice no longer lives in Munich. "
            "The weather is nice.",
            namespace="a",
        )
    finally:
        await eng.stop()
    reasons = {(f.sentence.split(".")[0], f.reason) for f in flags}
    assert ("Alice moved to a new city last year", "different_value") in reasons
    assert ("Alice no longer lives in Munich", "negated_value") in reasons
    assert all("weather" not in f.sentence for f in flags)
    assert flags[0].history == ["Alice city: Berlin"]


async def test_vet_passes_a_consistent_draft() -> None:
    eng = await _engine()
    try:
        assert await eng.vet("Alice lives in Munich now, in the city centre.", namespace="a") == []
    finally:
        await eng.stop()
