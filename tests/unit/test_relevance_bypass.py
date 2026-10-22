"""I74: the personal-reference bypass of the relevance gate (``read.relevance_gate_bypass``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from memspine import Engine
from memspine.compose.pack import AssembledContext
from memspine.config.schema import ReadConfig
from memspine.core.owner_check import first_person, referenced_names, store_vocab
from memspine.observability.trace import search_forensics
from memspine.read.pipeline.state import ReadResult

T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)


class EmptyingGate:
    """Stands in for the calibrated gate: always empties the context, and counts its runs."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, query: str, result: ReadResult, ns: str | None = None) -> ReadResult:
        self.calls += 1
        return ReadResult(result.mode, AssembledContext(abstained=True))


def _engine(**read: Any) -> tuple[Engine, EmptyingGate]:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        embedding={"provider": "hash", "settings": {}},
        memories={"episodic": {"enabled": True}},
        read={"record_access": False, "relevance_gate": "store_calibrated", **read},
    )
    fake = EmptyingGate()
    eng._calibrated_gate = fake  # type: ignore[method-assign]
    return eng, fake


async def _seed(eng: Engine) -> None:
    for i, text in enumerate(
        ["Caroline: I moved from my home country years ago", "Melanie: pottery on Tuesday"]
    ):
        await eng.write(
            text, namespace="a", memory_type="episodic", valid_from=T0 + timedelta(minutes=i)
        )


def test_default_is_named() -> None:
    assert ReadConfig().relevance_gate_bypass == "named"


def test_referenced_names_rules() -> None:
    vocab = store_vocab(["Caroline: I moved to Sweden", "Melanie: pottery"], ["Caroline"])
    # exact name, mid-question
    assert referenced_names("When did Caroline give a speech?", vocab) == ["Caroline"]
    # near-name: a 3+ letter prefix of a stored word counts ("Mel" / "Melanie") ...
    assert referenced_names("Where is Mel going?", vocab) == ["Mel"]
    # ... a distinct name does not
    assert referenced_names("Where is Oscar going?", vocab) == []
    # a generic request names nobody
    assert referenced_names("What skills should students learn in school?", vocab) == []
    # a first word that is a participant counts; a generic first word does not
    assert referenced_names("Caroline said what?", vocab, ["Caroline"]) == ["Caroline"]
    assert referenced_names("What did it say?", vocab, ["Caroline"]) == []
    # the generic speakers are not names
    assert referenced_names("User said hi", vocab, ["user"]) == []


def test_first_person_tokens() -> None:
    assert first_person("My cousin just got engaged") == "My"
    assert first_person("What should I cook?") == "I"
    assert first_person("Remind me later") == "me"
    assert first_person("Where did Caroline move from?") is None
    assert first_person("What is a mime?") is None


async def test_named_message_bypasses_the_gate() -> None:
    eng, fake = _engine()
    await eng.start()
    try:
        await _seed(eng)
        with search_forensics() as stages:
            out = await eng.read("Where did Caroline move from?", namespace="a", mode="replay")
        assert out.context.records and not out.context.abstained
        assert fake.calls == 0  # the gate never ran
        assert stages["relevance_bypass"]["fired"] is True
        assert stages["relevance_bypass"]["names"] == ["Caroline"]
    finally:
        await eng.stop()


async def test_generic_request_still_gated_and_logged() -> None:
    eng, fake = _engine()
    await eng.start()
    try:
        await _seed(eng)
        with search_forensics() as stages:
            out = await eng.read("Write me a poem about autumn", namespace="a", mode="replay")
        assert out.context.abstained and fake.calls == 1
        assert stages["relevance_bypass"]["fired"] is False
    finally:
        await eng.stop()


async def test_first_person_is_a_separate_option() -> None:
    query = "What should I cook tonight?"
    eng, fake = _engine()  # named: first person does not bypass
    await eng.start()
    try:
        await _seed(eng)
        assert (await eng.read(query, namespace="a", mode="replay")).context.abstained
    finally:
        await eng.stop()
    eng, fake = _engine(relevance_gate_bypass="named_or_first_person")
    await eng.start()
    try:
        await _seed(eng)
        with search_forensics() as stages:
            out = await eng.read(query, namespace="a", mode="replay")
        assert out.context.records and fake.calls == 0
        assert stages["relevance_bypass"]["kind"] == "first_person"
    finally:
        await eng.stop()


async def test_none_disables_the_bypass() -> None:
    eng, fake = _engine(relevance_gate_bypass="none")
    await eng.start()
    try:
        await _seed(eng)
        with search_forensics() as stages:
            out = await eng.read("Where did Caroline move from?", namespace="a", mode="replay")
        assert out.context.abstained and fake.calls == 1
        assert "relevance_bypass" not in stages
    finally:
        await eng.stop()
