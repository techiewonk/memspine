"""F-16c: the engine-side viewer guard (G-12 B, M-05) on every multi-party read verb.

``viewer_guarded(verb)`` wraps an engine read verb (search, read, assemble, retrieve, shared_search,
get_record, brief, conversation, ...). With ``isolation.viewer_required`` on, a read of a
multi-party namespace without a viewer is refused (:class:`~memspine.scope.ScopeRefused`, reason
``viewer_required``) and a ``scope.refused`` tap event names the reason. A verb that has no native
viewer filter gets one: records the viewer may not see (``trust.visibility.visible_to``) are dropped
from its result (``scope.filtered``). With the flag off nothing is refused; an explicit ``viewer``
still filters the verbs that did not take one before.

A namespace is multi-party when it is named in ``isolation.multi_party_namespaces`` (or inside one),
was declared by a ``Memory(..., multi_party=True)`` handle, or holds a record that names more
than one participant or carries a ``visibility:`` tag (detected once per store generation).

Internal engine work that has no caller (erasure, procedural lookups) runs under
:func:`system_scope`; a verb that is already inside a checked verb is not checked again.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Coroutine
from typing import Any

from memspine.observability import tap
from memspine.scope import (
    CHECKED,
    Scope,
    ScopeRefused,
    ViewerPolicy,
    cross_scope_reason,
    system_scope,
)
from memspine.trust.visibility import VISIBILITY_PREFIX, participants_of, visible_to

__all__ = [
    "declare_multi_party",
    "is_multi_party",
    "record_in_scope",
    "refuse",
    "system_scope",
    "viewer_guarded",
    "viewer_required",
]


def _isolation(eng: Any) -> Any:
    resolved = getattr(eng, "_resolved", None)
    return getattr(getattr(resolved, "config", None), "isolation", None)


def viewer_required(eng: Any) -> bool:
    """True when ``isolation.viewer_required`` is on (False for an engine not started yet)."""
    return getattr(_isolation(eng), "viewer_required", False) is True


def _state(eng: Any) -> dict[str, Any]:
    d = eng.__dict__
    state = d.get("_scope_state")
    if state is None:
        state = {"declared": set(), "detected": set(), "clean": {}}
        d["_scope_state"] = state
    return state  # type: ignore[no-any-return]


def declare_multi_party(eng: Any, namespace: str) -> None:
    """Mark ``namespace`` multi-party (a handle built with ``multi_party=True`` calls this)."""
    _state(eng)["declared"].add(namespace)


def _is_multi_party_record(tags: list[str]) -> bool:
    names = {n.strip().casefold() for n in participants_of(tags)}
    return len(names) > 1 or any(t.startswith(VISIBILITY_PREFIX) for t in tags)


async def is_multi_party(eng: Any, namespace: str) -> bool:
    state = _state(eng)
    if namespace in state["declared"] or namespace in state["detected"]:
        return True
    for declared in _isolation(eng).multi_party_namespaces:
        if namespace == declared or namespace.startswith(declared.rstrip("/") + "/"):
            return True
    generation = eng.__dict__.get("_store_generation", 0)
    if state["clean"].get(namespace) == generation:
        return False
    storage = eng._storage
    if storage is None:
        return False
    for record in await storage.list_records(namespace):
        if _is_multi_party_record(list(record.tags)):
            state["detected"].add(namespace)
            tap.event("scope.multi_party", namespace=namespace, source="records")
            return True
    state["clean"][namespace] = generation
    return False


def refuse(verb: str, namespace: str, reason: str, *, viewer: str | None = None) -> ScopeRefused:
    """Emit ``scope.refused`` and build the error to raise."""
    tap.event(
        "scope.refused", verb=verb, namespace=namespace, reason=reason, viewer=viewer is not None
    )
    return ScopeRefused(reason, f"{verb} refused for {namespace!r}: {reason}")


def _record_of(item: Any) -> Any:
    return item[0] if isinstance(item, tuple) else item


def _filter(verb: str, namespace: str, result: Any, viewer: str) -> Any:
    if isinstance(result, list):
        kept = [
            item
            for item in result
            if not hasattr(_record_of(item), "tags") or visible_to(_record_of(item).tags, viewer)
        ]
        if len(kept) != len(result):
            tap.event(
                "scope.filtered",
                verb=verb,
                namespace=namespace,
                dropped=len(result) - len(kept),
                reason="not_visible",
            )
        return kept
    if hasattr(result, "tags") and not visible_to(result.tags, viewer):
        tap.event("scope.filtered", verb=verb, namespace=namespace, dropped=1, reason="not_visible")
        return None
    return result


def viewer_guarded[**P, R](
    verb: str, *, native: bool = False
) -> Callable[[Callable[P, Coroutine[Any, Any, R]]], Callable[P, Coroutine[Any, Any, R]]]:
    """Guard an engine read verb. ``native`` = the verb filters by ``viewer`` itself (search, read,
    assemble); otherwise the guard takes ``viewer=`` off the call and filters the result."""

    def deco(fn: Callable[P, Coroutine[Any, Any, R]]) -> Callable[P, Coroutine[Any, Any, R]]:
        signature = inspect.signature(fn)

        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            eng: Any = args[0]
            viewer = kwargs.get("viewer") if native else kwargs.pop("viewer", None)
            if isinstance(viewer, str):
                viewer = viewer.strip() or None
            if CHECKED.get():
                return await fn(*args, **kwargs)
            bound = signature.bind_partial(*args, **kwargs).arguments
            namespace = str(bound.get("namespace", "default"))
            if viewer_required(eng):
                multi = await is_multi_party(eng, namespace)
                reason = ViewerPolicy(multi_party=multi, viewer=viewer).check()
                if reason is not None:
                    raise refuse(verb, namespace, reason, viewer=viewer)
            token = CHECKED.set(True)
            try:
                result = await fn(*args, **kwargs)
            finally:
                CHECKED.reset(token)
            if viewer is not None and not native:
                return _filter(verb, namespace, result, viewer)  # type: ignore[no-any-return]
            return result

        return wrapper

    return deco


def record_in_scope(
    verb: str, reader_namespace: str, record_namespace: str, *, viewer: str | None = None
) -> bool:
    """A by-id read: the record must live in the reader's namespace (exact match). Emits
    ``scope.refused`` with ``cross_tenant`` / ``cross_owner`` otherwise."""
    reason = cross_scope_reason(Scope.from_namespace(reader_namespace), record_namespace)
    if reason is None:
        return True
    tap.event(
        "scope.refused",
        verb=verb,
        namespace=reader_namespace,
        reason=reason,
        viewer=viewer is not None,
    )
    return False
