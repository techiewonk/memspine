"""RG-only prompt self-optimization hook (D-43): no-op by default.

The ``[promptopt]`` extra (langmem-style metaprompt/gradient tuning) may plug
in here; core never optimizes prompts on its own.
"""

from __future__ import annotations

from memspine.prompts.base import Prompt

__all__ = ["optimize_prompt"]


def optimize_prompt(prompt: Prompt) -> Prompt:
    """Identity by default — the research-grade hook point (E7-tier)."""
    return prompt
