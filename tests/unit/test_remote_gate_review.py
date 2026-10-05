"""#50 privacy review (finding 4): the remote-LLM tier gate matches withheld text
in any whitespace, case or JSON escaping, knows the shapes prompt builders render
(entity name or fact key dropped), and the builders that hold context records
withhold by record before rendering. Fake providers only; no model is called.
"""

from __future__ import annotations

from typing import Any

import orjson

from memspine import Engine
from memspine.core.policies.assembly import AssembledContext
from memspine.core.records import MemoryRecord, PiiTier
from memspine.services.llm import tier_gate
from memspine.services.llm.tier_gate import WITHHELD_MARKER, TierGatedLLM

SECRET = "Diagnosis: HIV positive.\nStarted ART in March."


class _Fake:
    provider_id = "fake"
    model = "fake/model"

    def __init__(self, reply: str = "{}") -> None:
        self.reply = reply
        self.seen: list[list[dict[str, str]]] = []

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        self.seen.append(messages)
        return self.reply


def _gated(fake: _Fake, texts: list[str]) -> TierGatedLLM:
    async def withheld() -> list[str]:
        return texts

    return TierGatedLLM(fake, withheld)  # type: ignore[arg-type]


async def test_normalised_and_dated_rendering_is_withheld() -> None:
    fake = _Fake()
    notes = "- " + " ".join(("[2026-10-05 Mon] " + SECRET).split())
    await _gated(fake, [SECRET]).chat([{"role": "user", "content": notes}])
    sent = fake.seen[-1][0]["content"]
    assert "HIV" not in sent and "ART" not in sent
    assert sent == f"- [2026-10-05 Mon] {WITHHELD_MARKER}"


async def test_recased_and_json_escaped_forms_are_withheld() -> None:
    fake = _Fake()
    prompt = "\n".join(
        [
            SECRET.upper(),
            orjson.dumps(SECRET).decode(),
            "DIAGNOSIS:   hiv positive.\t\tstarted art in march.",
            "Ana likes tea a lot",
        ]
    )
    await _gated(fake, [SECRET]).chat([{"role": "user", "content": prompt}])
    sent = fake.seen[-1][0]["content"]
    assert "hiv" not in sent.lower()
    assert sent.count(WITHHELD_MARKER) == 3
    assert "Ana likes tea a lot" in sent


def test_pattern_skips_short_texts() -> None:
    assert tier_gate.withheld_pattern(["short", "  a  b "]) is None
    assert tier_gate.withheld_pattern([]) is None


def _engine() -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        read={"hybrid": False},
        memories={"semantic": {"enabled": True}},
        consent={"remote_llm_max_tier": "low"},
        llm={
            "roles": {
                "sufficiency": {"model": "openai/gpt-4o"},
                "verify_answer": {"model": "openai/gpt-4o"},
                "summarize": {"model": "ollama/llama3"},
            }
        },
    )


def _swap_inner(eng: Engine, role: str, fake: _Fake) -> None:
    assert eng._llm is not None
    provider = eng._llm.provider(role)
    assert isinstance(provider, TierGatedLLM)
    provider._inner = fake  # type: ignore[assignment]


async def test_entity_stripped_timeline_line_is_withheld() -> None:
    eng = await _engine().start()
    try:
        fake = _Fake()
        _swap_inner(eng, "sufficiency", fake)
        await eng.write(
            "Alice lives in Paris SECRETTL1",
            namespace="u",
            entity="Alice",
            attribute="city",
            pii_tier=PiiTier.HIGH,
        )
        timeline = "- 2026-10-05: lives in Paris SECRETTL1"
        await eng.llm("sufficiency").chat([{"role": "user", "content": timeline}])
        assert "SECRETTL1" not in fake.seen[-1][0]["content"]
    finally:
        await eng.stop()


async def test_context_records_are_withheld_by_tier_before_rendering() -> None:
    eng = await _engine().start()
    try:
        fake = _Fake('{"complete": true}')
        _swap_inner(eng, "sufficiency", fake)
        # A paraphrase no text match can catch: only the record's tier says so.
        paraphrase = MemoryRecord(
            namespace="u",
            memory_type="semantic",
            content="the patient began antiretroviral therapy in spring",
            pii_tier=PiiTier.HIGH,
        )
        plain = MemoryRecord(namespace="u", memory_type="semantic", content="Ana likes tea a lot")
        context = AssembledContext(records=[paraphrase, plain])
        assert await eng._missing_info_queries("what about Ana?", context) == []
        sent = fake.seen[-1][-1]["content"]
        assert "antiretroviral" not in sent and WITHHELD_MARKER in sent
        assert "Ana likes tea a lot" in sent

        verifier = _Fake('{"supported": true, "evidence": [2]}')
        _swap_inner(eng, "verify_answer", verifier)
        verdict = await eng.verify_answer("q", "tea", [paraphrase, plain])
        assert verdict["supported"] and verdict["evidence_ids"] == [plain.record_id]
        sent = verifier.seen[-1][-1]["content"]
        assert "antiretroviral" not in sent and WITHHELD_MARKER in sent
    finally:
        await eng.stop()


async def test_local_roles_see_context_records_unchanged() -> None:
    eng = await _engine().start()
    try:
        record = MemoryRecord(
            namespace="u", memory_type="semantic", content="private text", pii_tier="high"
        )
        assert eng._remote_view("summarize", [record])[0].content == "private text"
        assert eng._remote_view("sufficiency", [record])[0].content == WITHHELD_MARKER
    finally:
        await eng.stop()
