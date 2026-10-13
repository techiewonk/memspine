"""Model-call concurrency hook (RT-02; P-20): the seam a global model gate plugs into.

``models`` sits below ``runtime``, so the gate itself lives in ``runtime.scheduler`` and installs
itself here. Every model call (LLM chat, embedding, rerank) enters ``model_slot()``; with no gate
installed (flag ``runtime.model_concurrency`` = 0, the default) that is a no-op context manager, so
behaviour and cost are unchanged.

Priority classes (lower number = served first): foreground read, foreground write, background
derive. The class of the current task rides a ``ContextVar``: ``priority_scope`` sets it for a block
and ``prioritised`` decorates an async function. Unset means foreground read.
"""

from __future__ import annotations

import contextlib
import functools
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextvars import ContextVar
from enum import IntEnum
from typing import Any, Protocol, TypeVar

__all__ = [
    "ModelGate",
    "Priority",
    "current_priority",
    "install_gate",
    "installed_gate",
    "model_slot",
    "prioritised",
    "priority_scope",
]

_F = TypeVar("_F", bound=Callable[..., Awaitable[Any]])


class Priority(IntEnum):
    """Scheduling classes: foreground read > foreground write > background derive."""

    READ = 0
    WRITE = 1
    DERIVE = 2


class ModelGate(Protocol):
    """What ``model_slot`` needs from an installed gate."""

    def slot(
        self, priority: Priority, kind: str
    ) -> contextlib.AbstractAsyncContextManager[None]: ...


_PRIORITY: ContextVar[Priority] = ContextVar("memspine_model_priority", default=Priority.READ)
_GATE: ModelGate | None = None


def current_priority() -> Priority:
    return _PRIORITY.get()


@contextlib.contextmanager
def priority_scope(priority: Priority) -> Iterator[None]:
    """Run a block (and the tasks it spawns) under ``priority``."""
    token = _PRIORITY.set(priority)
    try:
        yield
    finally:
        _PRIORITY.reset(token)


def prioritised(priority: Priority) -> Callable[[_F], _F]:
    """Decorator: the async function (and what it awaits) runs under ``priority``."""

    def wrap(fn: _F) -> _F:
        @functools.wraps(fn)
        async def inner(*args: Any, **kwargs: Any) -> Any:
            with priority_scope(priority):
                return await fn(*args, **kwargs)

        return inner  # type: ignore[return-value]

    return wrap


def install_gate(gate: ModelGate | None) -> ModelGate | None:
    """Install (or, with None, remove) the process-wide gate. Returns the previous one."""
    global _GATE
    previous, _GATE = _GATE, gate
    return previous


def installed_gate() -> ModelGate | None:
    return _GATE


@contextlib.asynccontextmanager
async def model_slot(kind: str = "llm") -> AsyncIterator[None]:
    """Hold one model-concurrency slot for the duration of a model call (no-op without a gate)."""
    gate = _GATE
    if gate is None:
        yield
        return
    async with gate.slot(_PRIORITY.get(), kind):
        yield
