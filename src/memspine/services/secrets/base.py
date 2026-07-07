"""Secrets port. Resolved in bootstrap phase 1, before config resolution (D-22)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["SecretsService"]


@runtime_checkable
class SecretsService(Protocol):
    """Synchronous by design: secrets resolve before the event loop owns anything."""

    def get(self, name: str) -> str | None: ...
