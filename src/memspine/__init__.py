"""memspine — a cognitive memory engine for AI agents.

Public surface is intentionally tiny (D-01): the ``Engine`` facade and
``__version__``. Everything else is implementation detail; import stability is
only promised for this module's ``__all__`` (D-21 semver discipline).
"""

from __future__ import annotations

from memspine.engine import Engine

__version__ = "0.0.1"

__all__ = ["Engine", "__version__"]
