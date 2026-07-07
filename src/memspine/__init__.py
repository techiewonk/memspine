"""memspine — a cognitive memory engine for AI agents.

Public surface is intentionally tiny (D-01): the ``Engine`` facade and ``__version__``.
Implementation lands phase by phase (see ``docs/memspine-structure-plan.md`` §5).
"""

from __future__ import annotations

__version__ = "0.0.1"

__all__ = ["__version__"]

# NOTE: `from memspine import Engine` becomes available once `engine.py` lands in P0/P1.
# Until then this package only exposes the version so tooling (build, import checks) works.
