"""Client ABC (D-24): settings + connect/close/health for one external system."""

from __future__ import annotations

from abc import ABC, abstractmethod

__all__ = ["Client"]


class Client(ABC):
    """Owns one connection to one external system.

    Retry/timeout/backoff policy belongs here (transport concern), never in
    services. The lifecycle manager connects clients before services start and
    closes them centrally after services stop.
    """

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    async def health(self) -> bool:
        return True
