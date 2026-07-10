"""B2: scenario/conditional prompt selection (D-43).

A role can have scenario *variants* (``<role>@<scenario>`` with a ``when:``
block); ``select(role, ...)`` picks the most-specific match and falls back to
the role's base prompt. With no variants shipped the base always wins, so
``profile="simple"`` behavior is unchanged — the tests build variants in-memory
via ``extra_prompts`` to exercise the machinery without touching the pack.
"""

from __future__ import annotations

import pytest

from memspine.exceptions import ConfigError
from memspine.prompts.base import Prompt, PromptWhen
from memspine.prompts.registry import PromptRegistry


def _variant(scenario: str, **when: str) -> Prompt:
    return Prompt(
        id=f"judge@{scenario}",
        role="judge",
        body="variant body",
        when=PromptWhen(**when),
    )


def test_base_wins_when_no_scenario_is_requested() -> None:
    reg = PromptRegistry(extra_prompts=[_variant("arbiter", condition="arbiter")])
    # No condition passed -> the arbiter variant is ineligible, base judge wins.
    assert reg.select("judge").id == "judge"
    assert reg.for_role("judge").id == "judge"


def test_matching_condition_selects_the_variant() -> None:
    reg = PromptRegistry(extra_prompts=[_variant("arbiter", condition="arbiter")])
    assert reg.select("judge", condition="arbiter").id == "judge@arbiter"
    # An unmatched condition still falls back to base.
    assert reg.select("judge", condition="cheap").id == "judge"


def test_most_specific_variant_wins() -> None:
    one = _variant("ep", memory_type="episodic")
    two = _variant("ep_arb", memory_type="episodic", condition="arbiter")
    reg = PromptRegistry(extra_prompts=[one, two])
    # Both eligible; the two-constraint variant is more specific.
    chosen = reg.select("judge", memory_type="episodic", condition="arbiter")
    assert chosen.id == "judge@ep_arb"
    # Only memory_type matches -> the single-constraint variant wins.
    assert reg.select("judge", memory_type="episodic").id == "judge@ep"


def test_equally_specific_variants_are_ambiguous() -> None:
    a = _variant("a", condition="x")
    b = _variant("b", condition="x")
    reg = PromptRegistry(extra_prompts=[a, b])
    with pytest.raises(ConfigError, match="ambiguous prompt selection"):
        reg.select("judge", condition="x")


def test_selection_config_pins_a_default_scenario() -> None:
    reg = PromptRegistry(
        extra_prompts=[_variant("arbiter", condition="arbiter")],
        selection={"judge": {"condition": "arbiter"}},
    )
    # The config default makes select() resolve to the variant with no caller arg.
    assert reg.select("judge").id == "judge@arbiter"
    # An explicit caller arg still overrides the config default.
    assert reg.select("judge", condition="none").id == "judge"


def test_unknown_selection_key_is_rejected() -> None:
    with pytest.raises(ConfigError, match="unknown selector key"):
        PromptRegistry(selection={"judge": {"bogus": "x"}})


def test_select_unknown_role_raises() -> None:
    with pytest.raises(ConfigError, match="no prompt for role"):
        PromptRegistry().select("telepathy")
