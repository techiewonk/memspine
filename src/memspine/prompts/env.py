"""Jinja environment + partials loader for prompts (D-43, v0.2 B1).

Prompt bodies are Jinja templates that may ``{% include %}`` shared fragments
from ``defaults/_partials/`` — the anti-injection safety block, the YAML-only
output footer, format instructions — so the boilerplate lives once instead of
being copy-pasted into every prompt (and drifting).

Because an included partial's text is part of the rendered prompt, a change to
a partial changes the effective prompt even though the frontmatter ``version``
did not. :func:`partials_fingerprint` folds a digest of the referenced partials
into :pyattr:`~memspine.prompts.base.Prompt.prompt_version`, so E3 caches
invalidate and E1 provenance shifts on a fragment edit exactly as they do on a
body edit.

The ``prompts.partials.<name>`` config layer supplies override fragments: a
:class:`~jinja2.DictLoader` is consulted before the shipped filesystem loader,
so a deployment can retune the shared safety/format language without forking
every prompt.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import xxhash
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader, StrictUndefined, meta

__all__ = [
    "build_environment",
    "default_environment",
    "partials_dir",
    "partials_fingerprint",
    "referenced_partial_sources",
]


def partials_dir() -> Path:
    return Path(__file__).parent / "defaults" / "_partials"


def build_environment(partial_overrides: dict[str, str] | None = None) -> Environment:
    """A Jinja environment whose loader resolves ``{% include %}`` names against
    the shipped ``_partials/`` directory, with optional override fragments
    (``prompts.partials.<name>``) taking precedence."""
    fs_loader = FileSystemLoader(str(partials_dir()))
    if partial_overrides:
        loader: FileSystemLoader | ChoiceLoader = ChoiceLoader(
            [DictLoader(dict(partial_overrides)), fs_loader]
        )
    else:
        loader = fs_loader
    return Environment(loader=loader, undefined=StrictUndefined, autoescape=False)


@lru_cache(maxsize=1)
def default_environment() -> Environment:
    """The override-free environment (shipped partials only). Cached: the
    partials directory is fixed and read-only, so one instance is shared by
    every prompt that has no ``prompts.partials`` overrides bound."""
    return build_environment(None)


def _referenced_names(env: Environment, body: str) -> set[str]:
    """Literal partial names an already-parsed body references (one level)."""
    ast = env.parse(body)
    return {name for name in meta.find_referenced_templates(ast) if name is not None}


def referenced_partial_sources(env: Environment, *bodies: str | None) -> dict[str, str]:
    """Transitive closure of the partials the given template ``bodies`` include
    (system + body of a prompt), mapped to their source text. Partials may
    themselves include partials, so this walks the graph; a name the loader
    cannot resolve is skipped (Jinja raises the real error at render time, where
    the message points at the prompt)."""
    assert env.loader is not None
    resolved: dict[str, str] = {}
    frontier: list[str] = []
    for body in bodies:
        if body:
            frontier.extend(_referenced_names(env, body))
    while frontier:
        name = frontier.pop()
        if name in resolved:
            continue
        try:
            source, _, _ = env.loader.get_source(env, name)
        except Exception:  # unresolved name defers to the render-time error
            continue
        resolved[name] = source
        frontier.extend(_referenced_names(env, source))
    return resolved


def partials_fingerprint(env: Environment, *bodies: str | None) -> str:
    """A short digest over the (name, source) pairs of every partial the given
    template ``bodies`` transitively include, or ``""`` when they include none.
    Stable across runs (sorted) so the same content always yields the same
    prompt_version."""
    sources = referenced_partial_sources(env, *bodies)
    if not sources:
        return ""
    digest = xxhash.xxh64()
    for name in sorted(sources):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sources[name].encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:8]
