"""Unit tests for ``memspine.prompts.lint`` (pure helpers; synthetic tokens only)."""

from __future__ import annotations

from memspine.prompts import lint
from memspine.prompts.base import parse_prompt_text
from memspine.prompts.registry import PromptRegistry


def test_lint_text_substring_whitespace_and_allowed() -> None:
    assert lint.lint_text("a  Zorp\n bar", answers=["zorp bar"]) == ["answer 'zorp bar'"]
    assert lint.lint_text("Zorp bar", answers=["zorp bar"], allowed=["Zorp Bar"]) == []


def test_lint_text_names_are_whole_word_case_sensitive() -> None:
    assert lint.lint_text("Quillon spoke", names=["Quillon"]) == ["speaker 'Quillon'"]
    assert lint.lint_text("Quillons spoke, quillon", names=["Quillon"]) == []


def test_neutral_context_covers_variables_and_renders_every_shipped_prompt() -> None:
    prompt = parse_prompt_text(
        "id: t\nversion: 1\nrole: t\nformat: text\nsystem: S {{ s }}\n---\n"
        "{% for x in items %}{{ x.name }} {% endfor %}{{ m['k'] }}"
    )
    assert set(lint.neutral_context(prompt)) == {"s", "items", "m"}
    assert lint.render_prompt_texts([prompt])
    reg = PromptRegistry()
    assert len(lint.render_prompt_texts(reg.list())) == len(reg.list())


def test_inline_strings_skips_docstrings_and_short_text_and_joins_fstrings() -> None:
    src = (
        '"""Module docstring ' + "x" * 100 + '"""\n'
        'SHORT = "tiny"\n'
        'LONG = "' + "y" * 90 + '"\n'
        'F = f"' + "z" * 50 + "{v}" + "w" * 50 + '"\n'
    )
    got = lint.inline_strings(src)
    assert sorted(t[0] for _, t in got) == ["y", "z"]
    assert any("{}" in t for _, t in got)
