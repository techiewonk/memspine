"""B1: Jinja partials loader + fingerprinted prompt_version (D-43).

The shared ``_partials/`` fragments fold repeated boilerplate (anti-injection
block, YAML-only footer) out of the individual prompts. Because an included
fragment is part of the rendered prompt, editing it must shift the prompt's
identity — otherwise E3 caches would serve stale extractions and E1 provenance
would point at the wrong text. These tests pin that invariant.
"""

from __future__ import annotations

import pytest

from memspine.exceptions import ConfigError
from memspine.prompts.env import build_environment, partials_fingerprint
from memspine.prompts.registry import PromptRegistry


def test_prompts_including_partials_carry_a_version_suffix() -> None:
    reg = PromptRegistry()
    # extract includes both shared partials; query_rewrite includes none.
    assert "+" in reg.get("extract").prompt_version
    assert reg.get("query_rewrite").prompt_version == "query_rewrite@1"


def test_prompts_sharing_partials_share_the_same_digest() -> None:
    reg = PromptRegistry()
    # extract, judge and firewall_flag all include no_injection + yaml_only.
    suffixes = {reg.get(pid).prompt_version.split("+", 1)[1] for pid in ("extract", "judge")}
    assert len(suffixes) == 1, "same partial set must yield the same fingerprint"


def test_render_resolves_the_included_partial_text() -> None:
    reg = PromptRegistry()
    messages = reg.get("extract").render({"content": "x"})
    system = next(m["content"] for m in messages if m["role"] == "system")
    # The anti-injection partial's wording is now present via {% include %}.
    assert "untrusted DATA" in system
    assert "valid YAML" in system


def test_partial_override_changes_the_fingerprint_and_version() -> None:
    default = PromptRegistry()
    overridden = PromptRegistry(partials={"yaml_only.j2": "YAML ONLY."})
    # Same prompt, different partial text => different identity (E3/E1).
    assert default.get("extract").prompt_version != overridden.get("extract").prompt_version
    # A prompt that doesn't include the overridden partial is unaffected... but
    # extract does, so its digest must move while summarize (no partials) stays.
    assert default.get("summarize").prompt_version == overridden.get("summarize").prompt_version


def test_override_partial_text_reaches_the_rendered_prompt() -> None:
    reg = PromptRegistry(partials={"yaml_only.j2": "STRICT-YAML-SENTINEL"})
    system = reg.get("extract").render({"content": "x"})[0]["content"]
    assert "STRICT-YAML-SENTINEL" in system


def test_unresolved_partial_name_fails_loudly_at_render() -> None:
    reg = PromptRegistry(overrides={"extract": {"body": '{% include "nope.j2" %}'}})
    with pytest.raises(ConfigError, match="failed to render"):
        reg.get("extract").render({"content": "x"})


def test_fingerprint_is_empty_when_no_partials_are_referenced() -> None:
    env = build_environment(None)
    assert partials_fingerprint(env, "no includes here", None) == ""
