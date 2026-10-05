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
    # An unmatched condition (no such shipped variant) falls back to base.
    assert reg.select("judge", condition="no_such_scenario").id == "judge"


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


def test_chat_infer_is_dated_plus_the_inference_rule() -> None:
    """G10: ``chat@infer`` (condition ``infer``) asks for an inference on would / might /
    likely questions; ``chat@dated``, the measured template prompt, does not."""
    from memspine.prompts.registry import PromptRegistry as _Registry

    registry = _Registry()
    infer = registry.select("chat", condition="infer")
    dated = registry.select("chat", condition="dated")
    assert infer.id == "chat@infer" and dated.id == "chat@dated"
    ctx = {"context": "[2023-05-07] Caroline: church", "message": "Is Caroline religious?"}
    infer_system = infer.render(ctx)[0]["content"]
    dated_system = dated.render(ctx)[0]["content"]
    assert "would, might, or is likely to do, be, or think" in infer_system
    assert "Likely yes, because" in infer_system
    assert "likely" not in dated_system
    assert infer.render(ctx)[-1] == dated.render(ctx)[-1]  # same user turn


def test_chat_dated2_is_dated_plus_the_said_vs_happened_rule() -> None:
    """G12: ``chat@dated2`` (condition ``dated2``) tells the reader that a line's leading
    date is when it was said and ``[= ...]`` is when the event happened; ``chat@dated``,
    the measured template prompt, is unchanged."""
    from memspine.prompts.registry import PromptRegistry as _Registry

    registry = _Registry()
    dated2 = registry.select("chat", condition="dated2")
    dated = registry.select("chat", condition="dated")
    assert dated2.id == "chat@dated2" and dated.id == "chat@dated"
    ctx = {"context": "[2023-05-08] I ran last week [= 2023-05-01..2023-05-07]", "message": "q"}
    dated2_system = dated2.render(ctx)[0]["content"]
    dated_system = dated.render(ctx)[0]["content"]
    rule = (
        "A line's leading [YYYY-MM-DD] is when it was said; a bracketed [= ...] after a "
        "relative phrase is the resolved date the event happened."
    )
    assert rule in dated2_system and rule not in dated_system
    assert "answer with the happened date (the [= ...] value when present)" in dated2_system
    assert (
        dated2_system.replace(rule, "")
        .replace(
            ' For "when did X happen", answer with the happened date (the [= ...] value when '
            "present), not the date it was said.",
            "",
        )
        .replace("  ", " ")
        == dated_system
    )
    assert dated2.render(ctx)[-1] == dated.render(ctx)[-1]  # same user turn


def test_no_template_selects_chat_dated2() -> None:
    """G12 ships opt-in: no shipped template's prompt selection names ``dated2`` yet."""
    from pathlib import Path

    import memspine.config

    templates = Path(memspine.config.__file__).parent / "templates"
    assert list(templates.glob("*.yaml"))
    for path in templates.glob("*.yaml"):
        assert "dated2" not in path.read_text(encoding="utf-8"), path.name
