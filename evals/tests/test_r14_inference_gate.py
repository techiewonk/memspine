"""R14-M-9 / OBS-06: ``read.inference_mode = gated`` (the typed commit route).

The commit clause reaches the reader only for a question the rules contract types as inference;
every other shape keeps the base prompt byte for byte; the clause keeps "not mentioned" open
for a question no memory line bears on (category 5); the refusal retry uses the inference
instruction on gated questions only. Invented names, no dataset text.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from memspine_evals.contracts import ReaderAnswer
from memspine_evals.experiments import C01Config, build_reader_and_judge, inference_gated
from memspine_evals.readers import (
    GROUNDED_GENERIC_QA_PROMPT,
    INFERENCE_COMMIT_CLAUSE,
    INFERENCE_GATE_VERSION,
    INFERENCE_RETRY_INSTRUCTION,
    QA_PROMPTS,
    RoutedQAPrompt,
    gated_inference_prompt,
    is_inference_question,
    prompt_variant,
    with_inference_clause,
)
from memspine_evals.refusal import RETRY_INSTRUCTION_NEUTRAL, RefusalRetryReader

INFER = [
    "Would Ilse enjoy a trip to the mountains?",
    "Is Bram likely to take up painting?",
    "What might Noor do if the shop closes?",
]
OTHER = [
    "Where did Tomasz move last spring?",
    "When did Ilse visit the museum?",
    "How many books did Bram read?",
    "What is Noor's job?",
]


@pytest.mark.parametrize("q", INFER)
def test_gate_fires_on_judgement_questions(q: str) -> None:
    assert is_inference_question(q)


@pytest.mark.parametrize("q", OTHER)
def test_gate_stays_off_for_other_shapes(q: str) -> None:
    assert not is_inference_question(q)


@pytest.mark.parametrize("name", sorted(QA_PROMPTS))
def test_clause_lands_before_the_context_paragraph(name: str) -> None:
    base = QA_PROMPTS[name]
    if "{context}" not in base:
        pytest.skip("no context field")
    out = with_inference_clause(base)
    assert INFERENCE_COMMIT_CLAUSE in out
    assert out.index(INFERENCE_COMMIT_CLAUSE) < out.index("{context}")
    out.format(context="c", question="q", question_date="d")  # still a valid template


def test_other_shapes_get_the_unchanged_prompt() -> None:
    p = gated_inference_prompt(GROUNDED_GENERIC_QA_PROMPT, "grounded_generic")
    assert isinstance(p, RoutedQAPrompt) and p.router_version == INFERENCE_GATE_VERSION
    for q in OTHER:
        assert p.format(context="ctx", question=q) == GROUNDED_GENERIC_QA_PROMPT.format(
            context="ctx", question=q, question_date="unknown"
        )
        assert prompt_variant(p, q) == "plain"
    for q in INFER:
        out = p.format(context="ctx", question=q)
        assert INFERENCE_COMMIT_CLAUSE in out and prompt_variant(p, q) == "inference"


def test_describe_hashes_both_variants() -> None:
    d = gated_inference_prompt(GROUNDED_GENERIC_QA_PROMPT, "g").describe()
    assert d["qa_router"] == INFERENCE_GATE_VERSION
    assert set(d["prompt_variants_sha256"]) == {"plain", "inference"}


def test_cat5_abstention_is_still_allowed() -> None:
    """The clause must not push an unanswerable question to commit: it names the 'not
    mentioned' reply for the case that no memory line bears on the question."""
    low = INFERENCE_COMMIT_CLAUSE.lower()
    assert "no memory line bears" in low and "not mentioned" in low and "instead of guessing" in low
    assert "no line bears" in INFERENCE_RETRY_INSTRUCTION and "Not mentioned" in INFERENCE_RETRY_INSTRUCTION
    # and the gated variant still carries the base prompt's own refusal rule
    full = gated_inference_prompt(GROUNDED_GENERIC_QA_PROMPT).format(context="x", question=INFER[0])
    assert "Not mentioned in the conversation" in full


class _Reader:
    reader_id = "stub"
    model = "stub-model"
    makes_model_calls = True

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.questions: list[str] = []

    def describe(self) -> dict[str, Any]:
        return {"reader_id": self.reader_id}

    async def answer(self, question: str, context: str, question_date: str | None = None) -> ReaderAnswer:
        self.questions.append(question)
        return ReaderAnswer(text=self.replies.pop(0) if self.replies else "Not mentioned.", model_calls=1)


CTX = "[2023-05-25] Ilse: I love hiking in the Alps"


def _ask(reader: RefusalRetryReader, q: str) -> ReaderAnswer:
    return asyncio.run(reader.answer(q, CTX))


def test_retry_uses_the_inference_instruction_on_gated_questions() -> None:
    inner = _Reader("Not mentioned.", "Likely yes, she loves hiking in the Alps.")
    out = _ask(RefusalRetryReader(inner, inference_gate=is_inference_question), INFER[0])
    assert inner.questions[1].endswith(INFERENCE_RETRY_INSTRUCTION)
    assert out.extra_meta["retry_inference"] is True and out.extra_meta["retry_accepted"] is True
    assert out.model_calls == 2  # same one retry call as before, nothing extra


def test_retry_keeps_the_neutral_instruction_for_other_shapes_and_when_off() -> None:
    inner = _Reader("Not mentioned.", "Denver")
    out = _ask(RefusalRetryReader(inner, inference_gate=is_inference_question), OTHER[0])
    assert inner.questions[1].endswith(RETRY_INSTRUCTION_NEUTRAL)
    assert "retry_inference" not in out.extra_meta
    off = _Reader("Not mentioned.", "Denver")
    reader = RefusalRetryReader(off)
    _ask(reader, INFER[0])
    assert off.questions[1].endswith(RETRY_INSTRUCTION_NEUTRAL)
    assert reader.reader_id == "stub+retry-neutral" and "retry_inference_gate" not in reader.describe()


def test_flag_off_builds_the_plain_prompt() -> None:
    cfg = C01Config(mode="qa", qa_prompt="grounded_generic", memspine_config={"read": {"inference_mode": "off"}})
    assert not inference_gated(cfg)
    reader, _, _ = build_reader_and_judge(cfg)
    assert reader.prompt == QA_PROMPTS["grounded_generic"]


def test_flag_on_builds_the_gated_prompt() -> None:
    cfg = C01Config(mode="qa", qa_prompt="grounded_generic", memspine_config={"read": {"inference_mode": "gated"}})
    assert inference_gated(cfg)
    reader, _, _ = build_reader_and_judge(cfg)
    assert isinstance(reader.prompt, RoutedQAPrompt)
    assert reader.prompt.variants["plain"] == QA_PROMPTS["grounded_generic"]


def test_flag_on_rejects_a_routed_prompt() -> None:
    cfg = C01Config(mode="qa", qa_prompt="routed", memspine_config={"read": {"inference_mode": "gated"}})
    with pytest.raises(ValueError, match="fixed"):
        build_reader_and_judge(cfg)


def test_config_default_is_off() -> None:
    from memspine.config.schema.read import ReadConfig

    assert ReadConfig().inference_mode == "off"


def test_arm_is_reference_plus_the_flag_only() -> None:
    import json
    from pathlib import Path

    from memspine_evals.rehearsal import validate_engine_config

    arms = Path(__file__).resolve().parents[1] / "arms"
    arm = json.loads((arms / "v2-R14-INFER-inference_mode.json").read_text(encoding="utf-8"))
    base = json.loads((arms / "scr-pool-protect-jina.json").read_text(encoding="utf-8"))
    assert arm["read"].pop("inference_mode") == "gated"
    assert arm == base
    validate_engine_config(json.loads((arms / "v2-R14-INFER-inference_mode.json").read_text(encoding="utf-8")))


# ---------------------------------------------------------------- R14-INFER2: gated_v2 (widened shape)
from memspine_evals.experiments import inference_gated_v2  # noqa: E402
from memspine_evals.readers import (  # noqa: E402
    INFERENCE_GATE_VERSION_V2,
    inference_shape,
    is_inference_question_v2,
)

V2_FIRES = [
    "Do you think Ilse would stay in the city?",
    "Is it possible that Bram quit the choir?",
    "What kind of person is Noor?",
    "Would Tomasz be considered a morning person?",
    "What would Ilse think of the new rule?",
    "What does Bram value most in a friend?",
    "If the bakery closed, Noor would move away, right?",
    "Based on her hobbies, what shop would Ilse like?",
    "Does Tomasz live near a river or a hill?",
    "Which job is Bram probably suited to?",
    "Wouldn't Noor prefer tea?",
]
V2_QUIET = [
    "When did Ilse start the choir?",
    "Where did Bram go last week?",
    "Who gave Noor the lamp?",
    "How many cats does Tomasz have?",
    "How long did Ilse take to finish the quilt?",
    "What did Bram say would help the garden?",
    "Which year did Noor open the shop?",
    "What is Tomasz's favourite colour?",
    "Did Ilse visit the museum in May?",
    "",
]


@pytest.mark.parametrize("q", V2_FIRES)
def test_shape_fires_on_widened_judgement_questions(q: str) -> None:
    fires, why = inference_shape(q)
    assert fires and why and is_inference_question_v2(q)


@pytest.mark.parametrize("q", V2_QUIET)
def test_shape_keeps_factual_questions_plain(q: str) -> None:
    assert inference_shape(q) == (False, "")


@pytest.mark.parametrize("q", INFER)
def test_shape_is_a_superset_of_the_contract_gate(q: str) -> None:
    assert inference_shape(q)[0]


def test_v2_prompt_gates_on_the_widened_shape_and_v1_is_unchanged() -> None:
    v2 = gated_inference_prompt(GROUNDED_GENERIC_QA_PROMPT, "g", v2=True)
    v1 = gated_inference_prompt(GROUNDED_GENERIC_QA_PROMPT, "g")
    assert v2.router_version == INFERENCE_GATE_VERSION_V2 != v1.router_version
    q = "What kind of person is Noor?"
    assert prompt_variant(v2, q) == "inference" and INFERENCE_COMMIT_CLAUSE in v2.format(context="c", question=q)
    assert prompt_variant(v1, q) == "plain"
    for q in OTHER:
        assert prompt_variant(v2, q) == "plain"
    assert v1.router_version == INFERENCE_GATE_VERSION


def test_v2_keeps_the_cat5_abstention_wording() -> None:
    full = gated_inference_prompt(GROUNDED_GENERIC_QA_PROMPT, v2=True).format(context="x", question=V2_FIRES[0])
    assert "not mentioned" in INFERENCE_COMMIT_CLAUSE.lower() and "Not mentioned in the conversation" in full


def test_v2_retry_route_uses_the_widened_gate() -> None:
    q = "What kind of person is Noor?"
    inner = _Reader("Not mentioned.", "Warm and generous, per the line about the lamp.")
    out = _ask(RefusalRetryReader(inner, inference_gate=is_inference_question_v2), q)
    assert inner.questions[1].endswith(INFERENCE_RETRY_INSTRUCTION) and out.extra_meta["retry_inference"] is True
    old = _Reader("Not mentioned.", "x")
    _ask(RefusalRetryReader(old, inference_gate=is_inference_question), q)  # v1 gate: neutral retry
    assert old.questions[1].endswith(RETRY_INSTRUCTION_NEUTRAL)
    plain = _Reader("Not mentioned.", "Denver")
    _ask(RefusalRetryReader(plain, inference_gate=is_inference_question_v2), OTHER[0])
    assert plain.questions[1].endswith(RETRY_INSTRUCTION_NEUTRAL)


def test_v2_flag_wiring() -> None:
    cfg = C01Config(mode="qa", qa_prompt="grounded_generic", memspine_config={"read": {"inference_mode": "gated_v2"}})
    assert inference_gated(cfg) and inference_gated_v2(cfg)
    reader, _, _ = build_reader_and_judge(cfg)
    assert reader.prompt.router_version == INFERENCE_GATE_VERSION_V2
    v1 = C01Config(mode="qa", qa_prompt="grounded_generic", memspine_config={"read": {"inference_mode": "gated"}})
    assert inference_gated(v1) and not inference_gated_v2(v1)
    off = C01Config(mode="qa", qa_prompt="grounded_generic", memspine_config={"read": {"inference_mode": "off"}})
    assert not inference_gated_v2(off)
    reader, _, _ = build_reader_and_judge(off)
    assert reader.prompt == QA_PROMPTS["grounded_generic"]


def test_v2_arm_is_reference_plus_the_flag_only() -> None:
    import json
    from pathlib import Path

    from memspine_evals.rehearsal import validate_engine_config

    arms = Path(__file__).resolve().parents[1] / "arms"
    path = arms / "v2-R14-INFER2-inference_mode_v2.json"
    arm = json.loads(path.read_text(encoding="utf-8"))
    base = json.loads((arms / "scr-pool-protect-jina.json").read_text(encoding="utf-8"))
    assert arm["read"].pop("inference_mode") == "gated_v2"
    assert arm == base
    validate_engine_config(json.loads(path.read_text(encoding="utf-8")))
