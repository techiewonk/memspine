"""Per-Engine binding of the process seams (ADR-079 decision 5, RM-75).

The model gate (``models.concurrency``), the SLO ledger (``economy.slo``), the write cost ledger
(``economy.ledger``), the ops ledger (``ops.ledger``) and the wiretap sink (``observability.tap``)
are read through module-level hooks by code that has no Engine handle (model seams, verb
decorators, pipeline steps). Each Engine owns one :class:`EngineBinding` holding the instances
for its own configuration (shared through the resource registry when the configuration is
identical). Every public Engine call runs with that binding in a ``ContextVar``; tasks the call
spawns copy it, so background work started by an Engine stays attributed to it.

Resolution for a seam (``resolve``):

1. the binding of the current context, when it carries the seam;
2. otherwise the module's own process default (set only by code outside an Engine, e.g. a
   unit test calling ``tap.configure``);
3. code running outside any binding (a raw thread, a direct pipeline call in a test) uses the
   only live Engine's binding when exactly one Engine is started in the process, which is
   exactly the single-Engine behaviour; with several live Engines such code gets nothing,
   rather than another Engine's instance. Such code runs as one Engine with
   ``with Engine.bound():`` (or :func:`bound`); work an Engine hands to an executor thread keeps
   the binding through :func:`run_in_executor` (``asyncio.to_thread`` already copies it).

Every coroutine method of the Engine (private ones too, so the pipeline context callbacks and
the facade helpers are covered) and of the scoped ``Memory`` handle and the agent tool-set runs
bound; the REST app binds every request to its Engine.

Stdlib only: every layer may import it.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import inspect
import os
import threading
import weakref
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures import Executor
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "EngineBinding",
    "bind_methods",
    "bind_public_methods",
    "bound",
    "current",
    "live",
    "register",
    "resolve",
    "run_in_executor",
    "unregister",
]


@dataclass(slots=True, eq=False)
class EngineBinding:
    """One Engine's seam instances (None = the seam is off for that Engine)."""

    gate: Any = None
    slo_ledger: Any = None
    cost_ledger: Any = None
    ops_ledger: Any = None
    tap: Any = None
    tap_sinks: list[Any] = field(default_factory=list)
    scheduler: Any = None
    #: ``(observability.inspect, observability.inspect_content)`` of this Engine (TI-1)
    inspect: tuple[bool, bool] | None = None
    #: TI-1 inline sleep cycle counter per namespace (one per Engine, never shared)
    inspect_cycles: dict[str, int] = field(default_factory=dict)

    def clear(self) -> None:
        self.gate = self.slo_ledger = self.cost_ledger = self.ops_ledger = None
        self.tap = self.scheduler = self.inspect = None
        self.tap_sinks = []
        self.inspect_cycles = {}


_CURRENT: ContextVar[EngineBinding | None] = ContextVar("memspine_engine_binding", default=None)
#: started Engines: (binding, weak reference to the Engine), so an Engine dropped without
#: ``stop()`` stops counting once it is collected
_LIVE: list[tuple[EngineBinding, weakref.ref[Any]]] = []
_LOCK = threading.Lock()


def current() -> EngineBinding | None:
    """The binding of this context, else the only live binding (None when ambiguous)."""
    binding = _CURRENT.get()
    if binding is not None:
        return binding
    alive = live()
    return alive[0] if len(alive) == 1 else None


def resolve(attr: str, default: Any) -> Any:
    """The seam ``attr`` of the current binding, falling back to the module ``default``."""
    binding = current()
    if binding is not None:
        value = getattr(binding, attr)
        if value is not None:
            return value
    return default


@contextlib.contextmanager
def bound(binding: EngineBinding) -> Iterator[None]:
    """Run a block (and every task it spawns) with ``binding`` as the current binding."""
    token = _CURRENT.set(binding)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def register(binding: EngineBinding, owner: object) -> None:
    """A started Engine's binding (idempotent); ``owner`` is the Engine (held weakly)."""
    global _LIVE
    with _LOCK:
        if not any(b is binding for b, _ in _LIVE):
            _LIVE = [*((b, r) for b, r in _LIVE if r() is not None), (binding, weakref.ref(owner))]


def unregister(binding: EngineBinding) -> None:
    global _LIVE
    with _LOCK:
        _LIVE = [(b, r) for b, r in _LIVE if b is not binding and r() is not None]


def live() -> tuple[EngineBinding, ...]:
    """The bindings of the started Engines that are still alive."""
    return tuple(b for b, r in _LIVE if r() is not None)


def run_in_executor[T](
    executor: Executor | None, fn: Callable[..., T], *args: Any
) -> asyncio.Future[T]:
    """``loop.run_in_executor`` that keeps the caller's context (and so its Engine binding);
    the plain loop method runs ``fn`` with the worker thread's empty context."""
    ctx = contextvars.copy_context()
    return asyncio.get_running_loop().run_in_executor(
        executor, functools.partial(ctx.run, fn, *args)
    )


def bind_public_methods(cls: type, attr: str = "_binding") -> None:
    """Make every public coroutine method of ``cls`` (and ``__aenter__`` / ``__aexit__``) run
    with the instance's binding (``getattr(self, attr)``) as the current binding."""
    bind_methods(cls, attr, private=False)


def bind_methods(cls: type, attr: str = "_binding", *, private: bool = True) -> None:
    """Like :func:`bind_public_methods`; ``private=True`` also binds the ``_`` methods (dunder
    methods other than ``__aenter__`` / ``__aexit__`` never)."""
    for name, fn in list(vars(cls).items()):
        dunder = name.startswith("__") and name.endswith("__")
        if dunder and name not in ("__aenter__", "__aexit__"):
            continue
        if not private and name.startswith("_") and not dunder:
            continue
        if inspect.iscoroutinefunction(fn) and not getattr(fn, "_memspine_bound", False):
            setattr(cls, name, _bound_method(fn, attr))


def _bound_method(fn: Callable[..., Awaitable[Any]], attr: str) -> Callable[..., Awaitable[Any]]:
    @functools.wraps(fn)
    async def inner(self: Any, *args: Any, **kwargs: Any) -> Any:
        binding = getattr(self, attr, None)
        if binding is None or _CURRENT.get() is binding:
            return await fn(self, *args, **kwargs)
        token = _CURRENT.set(binding)
        try:
            return await fn(self, *args, **kwargs)
        finally:
            _CURRENT.reset(token)

    inner._memspine_bound = True  # type: ignore[attr-defined]
    return inner


def _reset_after_fork() -> None:
    global _LIVE, _LOCK
    _LOCK = threading.Lock()
    _LIVE = []


if hasattr(os, "register_at_fork"):  # POSIX only; no Engine crosses a fork
    os.register_at_fork(after_in_child=_reset_after_fork)
