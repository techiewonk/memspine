from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Job:
    namespace: str
    operation_id: str
    items: list[str]  # Bounded synthetic data; use artifact references for real memory content.
    max_rounds: int = 3


@dataclass(frozen=True)
class Result:
    namespace: str
    operation_id: str
    items: list[str]
    digest: str
    rounds: int
