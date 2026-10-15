"""Typed registry of derive stages (MOD-02): stage name -> the callable that runs it.

Built-ins are the pure derive entry points; an enabled plug-in adds a stage under its own name.
The engine does not look stages up by name yet, so registration here changes no behaviour; it
makes the stages discoverable, listable and replaceable by pinned plug-ins.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from memspine.derive.compile import compile_items
from memspine.derive.coverage import coverage
from memspine.derive.ground import check_grounding
from memspine.derive.histories import value_history
from memspine.derive.integrate import integrate
from memspine.plugins import Registry

__all__ = ["DERIVE_STAGES"]

DERIVE_STAGES: Registry[Callable[..., Any]] = Registry("derive_stages")

DERIVE_STAGES.register("compile", compile_items)
DERIVE_STAGES.register("ground", check_grounding)
DERIVE_STAGES.register("integrate", integrate)
DERIVE_STAGES.register("coverage", coverage)
DERIVE_STAGES.register("histories", value_history)
