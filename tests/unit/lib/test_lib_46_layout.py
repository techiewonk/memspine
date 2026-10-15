"""LIB-46: prompt layout builder and checker (P-07, P-08, P-10, W-18)."""

from __future__ import annotations

import pytest

from memspine.prompts.base import Prompt
from memspine.prompts.layout import (
    SAME_LANGUAGE_RULE,
    LayoutError,
    PromptLayout,
    Section,
    assert_prefix_stable,
    build,
    check,
    report_prompts,
)
from memspine.prompts.registry import PromptRegistry


def _layout() -> PromptLayout:
    return PromptLayout.standard(
        role="You extract atomic facts for a memory system.",
        rules="Never invent facts.",
        schema="Emit a list under the key facts.",
        suffix=[
            Section(
                name="context",
                text="{% for c in context %}[{{ loop.index }}] {{ c }}\n{% endfor %}",
            ),
            Section(name="question", text="{{ question }}"),
        ],
    )


VARS_A = {
    "context": ["Ilse moved to Bram's street", "Noor owns a cello"],
    "question": "Where does Ilse live?",
}
VARS_B = {"context": ["Tomasz plays chess"], "question": "Who plays chess?"}


def rules(vs):
    return {v.rule for v in vs}


def test_build_splits_prefix_and_suffix():
    r = build(_layout(), VARS_A)
    assert "Ilse" not in r.prefix and "Ilse" in r.suffix
    assert r.prefix.startswith("You extract")
    assert r.text.startswith(r.prefix)
    assert [m["role"] for m in r.messages()] == ["system", "user"]


def test_prefix_stability_across_variable_sets():
    h = assert_prefix_stable(_layout(), VARS_A, VARS_B)
    a, b = build(_layout(), VARS_A), build(_layout(), VARS_B)
    assert a.prefix == b.prefix and a.prefix_hash == b.prefix_hash == h
    assert a.suffix != b.suffix


def test_prefix_with_template_syntax_rejected():
    with pytest.raises(ValueError):
        PromptLayout(prefix=(Section(name="role", text="You help {{ user }}."),))


def test_variable_content_in_prefix_raises():
    with pytest.raises(LayoutError):
        build(_layout(), {**VARS_A, "question": "Never invent facts."})


def test_missing_variable_raises():
    with pytest.raises(LayoutError):
        build(_layout(), {"context": []})


def test_token_counts():
    r = build(_layout(), VARS_A)
    tc = r.token_counts(lambda s: len(s.split()))
    assert tc["total"] == tc["prefix"] + tc["suffix"] > 0


def test_standard_layout_clean():
    assert check(build(_layout(), VARS_A)) == []
    assert check(_layout()) == []


@pytest.mark.parametrize(
    "text",
    [
        'Rules.\nExample: the user says "I like tea" and you reply with a fact.',
        'Extract facts, e.g. "Aiko lives in Ghent".',
        "Input: Aiko lives in Ghent.\nOutput: entity: Aiko",
        'Reply as {"entity": "Aiko", "confidence": 0.9}.',
    ],
)
def test_seeded_examples_detected(text):
    assert "example_block" in rules(check(text, require_language=False))


@pytest.mark.parametrize(
    "text",
    [
        'Reply as {"entity": "string", "confidence": number, "note": "..."}.',
        "Use the schema. Never invent facts, not guesses.",
        "entity: ...\nvalue: ...",
    ],
)
def test_shape_only_is_not_an_example(text):
    assert "example_block" not in rules(check(text, require_language=False))


def test_seeded_example_in_layout_detected():
    lay = PromptLayout.standard(role="Role.", rules='Example: Noor said "hello".')
    assert "example_block" in rules(check(lay))


def test_variable_in_prefix_string():
    flagged = check("Answer about {{ topic }}.", prefix=True, require_language=False)
    assert "variable_in_prefix" in rules(flagged)
    plain = check("Answer about {{ topic }}.", require_language=False)
    assert "variable_in_prefix" not in rules(plain)


def test_same_language_check():
    assert "missing_language_rule" in rules(check("Extract facts."))
    assert "missing_language_rule" in rules(check("Write in the same language as the text."))
    assert check(SAME_LANGUAGE_RULE) == []
    assert "missing_language_rule" not in rules(check("Extract facts.", require_language=False))


def test_gold_token_via_lint():
    vs = check(SAME_LANGUAGE_RULE + " Remember Lake Eder.", answers=["lake eder"], names=["Ilse"])
    assert [v.message for v in vs if v.rule == "gold_token"] == ["answer 'lake eder'"]
    vs = check(SAME_LANGUAGE_RULE + " Ask Ilse.", names=["Ilse"])
    assert any(v.rule == "gold_token" and "Ilse" in v.message for v in vs)
    vs = check(SAME_LANGUAGE_RULE + " likely yes", answers=["likely yes"], allowed=["likely yes"])
    assert not any(v.rule == "gold_token" for v in vs)


def _prompt(pid: str, system: str, body: str) -> Prompt:
    return Prompt(id=pid, role=pid, system=system, body=body)


def test_check_prompt_variable_in_system_and_examples():
    p = _prompt("t", 'You help {{ user }}. Example: "hi"', "Answer {{ q }}")
    got = rules(check(p))
    assert {"variable_in_prefix", "prefix_unstable", "example_block"} <= got


def test_check_prompt_clean():
    p = _prompt("t", SAME_LANGUAGE_RULE, "Answer {{ q }}")
    assert check(p) == []


def test_determinism():
    p = _prompt("t", "Do it.\nInput: a\nOutput: b", "{{ q }}")
    assert check(p) == check(p)
    assert build(_layout(), VARS_A) == build(_layout(), VARS_A)
    reg = PromptRegistry()
    assert report_prompts(reg.list()).render() == report_prompts(reg.list()).render()


def test_report_over_registered_prompts_records_numbers():
    reg = PromptRegistry()
    before = [(p.prompt_version, p.system, p.body) for p in reg.list()]
    report = report_prompts(reg.list())
    assert [(p.prompt_version, p.system, p.body) for p in reg.list()] == before
    assert report.prompts_checked == len(before) > 0
    print(
        f"LIB-46 TODAY: prompts={report.prompts_checked} "
        f"with_violations={report.prompts_with_violations} total={report.total} "
        f"by_rule={report.by_rule}"
    )
    print(report.render())


def test_report_fails_only_on_seeded_violation():
    clean = _prompt("c", SAME_LANGUAGE_RULE, "{{ q }}")
    dirty = _prompt("d", SAME_LANGUAGE_RULE + ' Example: "x"', "{{ q }}")
    rep = report_prompts([clean, dirty])
    assert list(rep.violations) == [dirty.prompt_version]
    assert rep.by_rule == {"example_block": 1}
