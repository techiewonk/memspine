"""Gold-leak lint primitives (pure functions; no deny-list lives here).

``lint_text`` takes text plus the caller's deny-list and returns the hits, so CI,
a pre-commit hook and the unit tests share one matcher. Three collectors feed it:
Jinja partials, prompts rendered through the real loader with neutral
placeholders, and long string constants in Python source.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Iterator
from pathlib import Path

from jinja2 import meta

from memspine.prompts.base import Prompt
from memspine.prompts.env import default_environment, referenced_partial_sources

__all__ = [
    "INLINE_MIN_CHARS",
    "inline_strings",
    "lint_text",
    "neutral_context",
    "partial_files",
    "render_prompt_texts",
]

#: A Python string constant at least this long is treated as potential prompt text.
INLINE_MIN_CHARS = 80


def lint_text(
    text: str,
    *,
    answers: Iterable[str] = (),
    names: Iterable[str] = (),
    allowed: Iterable[str] = (),
) -> list[str]:
    """Hits of ``answers`` (case-insensitive substring, whitespace-normalised) and
    ``names`` (case-sensitive whole word) in ``text``; ``allowed`` answers are skipped."""
    flat = " ".join(text.split())
    low = flat.lower()
    skip = {a.lower() for a in allowed}
    hits = [
        f"answer {a!r}" for a in sorted(set(answers)) if a.lower() in low and a.lower() not in skip
    ]
    hits += [
        f"speaker {n!r}" for n in sorted(set(names)) if re.search(rf"\b{re.escape(n)}\b", flat)
    ]
    return hits


def partial_files(partials_dir: Path) -> list[Path]:
    return sorted(partials_dir.glob("*.j2"))


class _Placeholder(str):
    """A neutral stand-in usable as a scalar, a sequence, a mapping or an object."""

    def __new__(cls, name: str) -> _Placeholder:
        return super().__new__(cls, f"<{name}>")

    def __iter__(self) -> Iterator[_Placeholder]:
        yield _Placeholder(str(self)[1:-1] + "_item")

    def __getitem__(self, key: object) -> _Placeholder:
        return _Placeholder(str(self)[1:-1] + "_item")

    def __getattr__(self, attr: str) -> _Placeholder:
        if attr.startswith("__"):
            raise AttributeError(attr)
        return _Placeholder(f"{str(self)[1:-1]}_{attr}")


def neutral_context(prompt: Prompt) -> dict[str, object]:
    """Placeholder values for every variable the prompt's system/body (and the
    partials they include) reference."""
    env = prompt._env or default_environment()
    sources = [s for s in (prompt.system, prompt.body) if s]
    sources += list(referenced_partial_sources(env, prompt.system, prompt.body).values())
    names: set[str] = set()
    for src in sources:
        names |= meta.find_undeclared_variables(env.parse(src))
    return {n: _Placeholder(n) for n in sorted(names)}


def render_prompt_texts(prompts: Iterable[Prompt]) -> dict[str, str]:
    """``prompt_version`` -> all rendered message text (system + user), with
    includes resolved by the prompt's own loader."""
    out: dict[str, str] = {}
    for prompt in prompts:
        messages = prompt.render(neutral_context(prompt))
        out[prompt.prompt_version] = "\n".join(m["content"] for m in messages)
    return out


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def inline_strings(source: str, *, min_chars: int = INLINE_MIN_CHARS) -> list[tuple[int, str]]:
    """``(line, text)`` of string constants (f-strings joined, docstrings skipped)
    of at least ``min_chars`` characters."""
    tree = ast.parse(source)
    docs = _docstring_ids(tree)
    nested: set[int] = set()
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                if isinstance(v, ast.Constant):
                    parts.append(str(v.value))
                    nested.add(id(v))
                else:
                    parts.append("{}")
            text = "".join(parts)
            if len(text) >= min_chars:
                found.append((node.lineno, text))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docs
            and id(node) not in nested
            and len(node.value) >= min_chars
        ):
            found.append((node.lineno, node.value))
    return sorted(found)
