"""I74: the personal-reference bypass of the relevance gate (``read.relevance_gate_bypass``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import ReadConfig
from memspine.core.owner_check import first_person, referenced_names, store_vocab
from memspine.observability.trace import search_forensics

T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)


def _engine(**read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"episodic": {"enabled": True}},
        read={"record_access": False, "gate": True, **read},
    )


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


async def test_named_message_bypasses_with_current_evidence() -> None:
    eng = _engine()
    await eng.start()
    try:
        await _seed(eng)
        with search_forensics() as stages:
            out = await eng.read("Where did Caroline move from?", namespace="a", mode="replay")
        assert out.context.records and not out.context.abstained
        entry = stages["relevance_gate"]
        assert entry["state"] == "bypass"
        assert entry["bypass_reason"] == "named_reference"
        assert entry["names"] == ["Caroline"]
    finally:
        await eng.stop()


async def test_generic_request_still_gated_and_logged() -> None:
    eng = _engine()
    await eng.start()
    try:
        await _seed(eng)
        with search_forensics() as stages:
            out = await eng.read("Write me a poem about autumn", namespace="a", mode="replay")
        assert out.context.abstained
        assert stages["relevance_gate"]["state"] == "missing"
    finally:
        await eng.stop()


async def test_first_person_without_evidence_does_not_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from memspine.read.pipeline import gate as gate_pipeline

    async def unsupported(
        _eng: Any, _ns: str, _query: str, records: Any, _leg: str
    ) -> dict[str, float]:
        return {r.record_id: -1.0 for r in records}

    monkeypatch.setattr(gate_pipeline, "_gate_scores", unsupported)
    eng = _engine(relevance_gate_bypass="named_or_first_person")
    await eng.start()
    try:
        await _seed(eng)
        with search_forensics() as stages:
            await eng.read("What should I cook tonight?", namespace="a", mode="replay")
        assert stages["relevance_gate"]["bypass_reason"] is None
    finally:
        await eng.stop()


async def test_none_disables_named_bypass() -> None:
    eng = _engine(relevance_gate_bypass="none")
    await eng.start()
    try:
        await _seed(eng)
        with search_forensics() as stages:
            await eng.read("Where did Caroline move from?", namespace="a", mode="replay")
        assert stages["relevance_gate"]["bypass_reason"] is None
        assert stages["relevance_gate"]["names"] == []
    finally:
        await eng.stop()
