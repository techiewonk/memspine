"""Chained secrets resolver (D-22): first backend with a value wins.

Order encodes precedence — env/.env first, then AWS — so local development
never needs cloud credentials and a locally-set value always overrides the
remote one (the D-11 "closer to runtime wins" intuition, extended to secrets).
"""

from __future__ import annotations

from memspine.services.secrets.base import SecretsService

__all__ = ["ChainedSecrets"]


class ChainedSecrets:
    def __init__(self, *backends: SecretsService) -> None:
        self._backends = backends

    def get(self, name: str) -> str | None:
        for backend in self._backends:
            value = backend.get(name)
            if value is not None:
                return value
        return None
