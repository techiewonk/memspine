"""Wiretap: per-request trace of every pipeline stage (WT-01 fills this in).

API contract that wiring code may call today (no-op until WT-01 lands; off by default):

- ``event(stage, /, **fields)``: record one stage event in the current request trace. ``fields`` hold ids,
  counts, scores, decisions and drop reasons. Never pass free text unless it is wrapped with ``content(...)``,
  which is dropped unless the content opt-in is on.
- ``span(stage, /, **fields)``: context manager timing a stage; emits start/end events.
- ``request(kind, /, **fields)``: context manager opening a request trace (read, write, sleep job).
- ``enabled()``: cheap check so callers can skip building expensive payloads.
- ``content(text)``: marks text as content (truncated, redacted, opt-in only).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


def enabled() -> bool:
    return False


def event(stage: str, /, **fields: Any) -> None:
    return None


def content(text: str) -> str:
    return ""


@contextmanager
def span(stage: str, /, **fields: Any) -> Iterator[None]:
    yield


@contextmanager
def request(kind: str, /, **fields: Any) -> Iterator[None]:
    yield
