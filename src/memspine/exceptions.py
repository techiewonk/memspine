"""memspine exception hierarchy.

Every error raised by the public surface derives from :class:`MemspineError`
so callers can catch one base. Names follow the structure plan §1.
"""

from __future__ import annotations

__all__ = [
    "ConfigError",
    "ConflictError",
    "LLMError",
    "MemspineError",
    "MissingServiceError",
    "NamespaceError",
    "RebuildUnavailableError",
    "RollbackUnavailableError",
    "StorageError",
    "WeightIntegrityError",
]


class MemspineError(Exception):
    """Base class for all memspine errors."""


class ConfigError(MemspineError):
    """Invalid, unresolvable, or contradictory configuration (D-11/D-12)."""


class NamespaceError(MemspineError):
    """Invalid namespace path or a grant violation (M8.5)."""


class MissingServiceError(MemspineError):
    """A required service is not available (D-10).

    Always names the extra that provides it, so the fix is one pip install away.
    """

    def __init__(self, service: str, extra: str | None = None, *, hint: str | None = None) -> None:
        self.service = service
        self.extra = extra
        self.hint = hint
        detail = f" — install with `pip install memspine[{extra}]`" if extra else ""
        if hint:
            detail += f" — {hint}"
        super().__init__(f"required service {service!r} is not available{detail}")


class WeightIntegrityError(MissingServiceError):
    """A pinned model file differs from its catalogue digest (ADR-093); it is never loaded."""

    def __init__(self, service: str, detail: str, *, hint: str | None = None) -> None:
        self.detail = detail
        super().__init__(service, hint=f"{detail}; {hint}" if hint else detail)


class ConflictError(MemspineError):
    """Unresolvable write conflict surfaced by the conflict ladder (M4)."""


class StorageError(MemspineError):
    """Persistence-layer failure in the storage service (D-36)."""


class LLMError(MemspineError):
    """An LLM provider call failed or returned an unusable response (D-07/D-39)."""


class RebuildUnavailableError(MemspineError):
    """Projector rebuild requested but the event log cannot serve it (D-45).

    Raised when ``event_log.mode`` is ``ephemeral`` (no events persisted) or when
    a ``rolling`` window no longer contains the events required for a full rebuild.
    """


class RollbackUnavailableError(RebuildUnavailableError):
    """A taint rollback/repair asked for history the event log no longer holds (#64).

    Raised only with ``strict=True``: the seed's origin WRITE is not in the log
    (``event_log.mode: ephemeral`` persists none; a ``rolling`` window pruned it), so
    its descendants cannot be traced. Without ``strict`` the engine warns and falls
    back to archiving the seed alone, closing its ``valid_to``.
    """
