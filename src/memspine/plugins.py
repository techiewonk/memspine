"""Typed registries and entry-point plug-ins (MOD-02; schema session 12, ER-33/ER-34).

One generic :class:`Registry` per extension kind (``embedders``, ``rerankers``, ...). Each
package owns its kind's registry module (``<pkg>/registry.py``) and registers its built-ins there
at import, under the same names the config uses today.

Discovery is not enablement. Third-party providers advertise themselves through the entry-point
group ``memspine.<kind>``; :func:`discover` lists them and imports nothing. A provider is used only
when the config ``plugins:`` list names it with an exact version pin (and, optionally, a
``sha256:`` hash of the distribution's ``RECORD`` file) -- see :func:`enable`.

Slot rule: a plug-in named ``acme`` found in group ``memspine.embedders`` claims the slot
``embedders:acme``, the registry key it adds. The manifest's ``slot`` must say so. Two enabled
plug-ins on one slot, a slot that collides with a built-in, a version or hash that does not match
the installed distribution, or a declared lifetime that differs from the pinned one, is a startup
error. A plug-in object may declare its lifetime with a ``__memspine_lifetime__`` attribute
(``app`` | ``job`` | ``request``, default ``app``).

Imports: stdlib, ``memspine.schemas`` and ``memspine.exceptions`` only.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from memspine.exceptions import ConfigError
from memspine.schemas.public.plugins import (
    CapabilityManifest,
    DuplicateSlotError,
    LifetimeViolation,
    check_enabled,
)

__all__ = [
    "KINDS",
    "DiscoveredPlugin",
    "EnabledPlugin",
    "PluginError",
    "PluginHashError",
    "PluginLifetimeError",
    "PluginNotFoundError",
    "PluginVersionError",
    "Registry",
    "UnknownProviderError",
    "disable_all",
    "discover",
    "enable",
    "enabled_manifest",
    "get_registry",
    "load_registries",
]

#: The extension kinds and the module that owns each kind's registry.
KINDS: dict[str, str] = {
    "llm_providers": "memspine.models.registry",
    "embedders": "memspine.models.registry",
    "rerankers": "memspine.models.registry",
    "vector_stores": "memspine.store.registry",
    "lexical_indexes": "memspine.store.registry",
    "graph_stores": "memspine.store.registry",
    "read_legs": "memspine.read.registry",
    "derive_stages": "memspine.derive.registry",
    "memory_types": "memspine.memory.registry",
    "prompt_sets": "memspine.prompts.registry",
}

_GROUP_PREFIX = "memspine."
_LIFETIMES = ("app", "job", "request")


class PluginError(ConfigError):
    """A plug-in could not be enabled (startup error)."""


class PluginNotFoundError(PluginError):
    """The config names a plug-in no installed distribution advertises."""


class PluginVersionError(PluginError):
    """The installed distribution's version differs from the pin."""


class PluginHashError(PluginError):
    """The distribution's RECORD hash differs from the pin, or cannot be computed."""


class PluginLifetimeError(PluginError, LifetimeViolation):
    """The pinned lifetime differs from the lifetime the plug-in declares."""


class UnknownProviderError(ConfigError):
    """No provider is registered under that name for the kind."""


class Registry[T]:
    """A typed name -> provider map for one extension kind.

    Built-ins are registered explicitly by the owning package; enabled plug-ins are added by
    :func:`enable`. Names are unique; re-registering a built-in name raises unless
    ``replace=True`` (the pre-existing ``register_reranker`` seam used replace semantics).
    """

    def __init__(self, kind: str) -> None:
        if kind not in KINDS:
            raise ValueError(f"unknown registry kind {kind!r} (known: {sorted(KINDS)})")
        self.kind = kind
        self._items: dict[str, T] = {}
        self._builtin: set[str] = set()
        self._lifetime: dict[str, str] = {}
        _REGISTRIES[kind] = self

    def register(self, name: str, provider: T, *, builtin: bool = True, replace: bool = False) -> T:
        if name in self._items and not replace:
            raise PluginError(f"{self.kind}: {name!r} is already registered")
        self._items[name] = provider
        if builtin:
            self._builtin.add(name)
        else:
            self._builtin.discard(name)
        return provider

    def unregister(self, name: str) -> None:
        self._items.pop(name, None)
        self._builtin.discard(name)
        self._lifetime.pop(name, None)

    def get(self, name: str, *default: Any) -> T:
        """The provider for ``name``; unknown raises (dict-style ``default`` accepted)."""
        try:
            return self._items[name]
        except KeyError:
            if default:
                return default[0]  # type: ignore[no-any-return]
            raise UnknownProviderError(
                f"unknown {self.kind} provider {name!r} (valid: {', '.join(self._items)})"
            ) from None

    def find(self, name: str) -> T | None:
        return self._items.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(self._items)

    def builtin_names(self) -> tuple[str, ...]:
        return tuple(n for n in self._items if n in self._builtin)

    def is_builtin(self, name: str) -> bool:
        return name in self._builtin

    def lifetime_of(self, name: str) -> str:
        return self._lifetime.get(name, "app")

    # dict-style access, kept so code and tests that used the old plain-dict registries still work.
    def __getitem__(self, name: str) -> T:
        return self._items[name]

    def __setitem__(self, name: str, provider: T) -> None:
        self.register(name, provider, builtin=False, replace=True)

    def __delitem__(self, name: str) -> None:
        if name not in self._items:
            raise KeyError(name)
        self.unregister(name)

    def pop(self, name: str, *default: T | None) -> T | None:
        if name in self._items:
            value = self._items[name]
            self.unregister(name)
            return value
        if default:
            return default[0]
        raise KeyError(name)

    def __contains__(self, name: object) -> bool:
        return name in self._items

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def items(self) -> Iterable[tuple[str, T]]:
        return self._items.items()


_REGISTRIES: dict[str, Registry[Any]] = {}


def load_registries() -> None:
    """Import every package registry module so all kinds exist (built-ins register at import)."""
    for module in dict.fromkeys(KINDS.values()):
        importlib.import_module(module)


def get_registry(kind: str) -> Registry[Any]:
    load_registries()
    try:
        return _REGISTRIES[kind]
    except KeyError:
        raise PluginError(f"unknown plug-in kind {kind!r} (known: {sorted(KINDS)})") from None


@dataclass(frozen=True)
class DiscoveredPlugin:
    """An advertised, not enabled, entry point."""

    kind: str
    name: str
    value: str
    version: str | None
    dist_name: str | None


@dataclass(frozen=True)
class EnabledPlugin:
    manifest: CapabilityManifest
    kind: str
    dist_name: str | None
    installed_version: str


#: Entry-point lookup, indirected so tests can monkeypatch ``importlib.metadata.entry_points``.
def _entry_points(group: str) -> list[Any]:
    return list(importlib.metadata.entry_points(group=group))


def _dist_of(ep: Any) -> Any | None:
    return getattr(ep, "dist", None)


def discover(kinds: Iterable[str] | None = None) -> list[DiscoveredPlugin]:
    """List advertised plug-ins. Nothing is imported and nothing is enabled."""
    found: list[DiscoveredPlugin] = []
    for kind in kinds if kinds is not None else KINDS:
        for ep in _entry_points(_GROUP_PREFIX + kind):
            dist = _dist_of(ep)
            found.append(
                DiscoveredPlugin(
                    kind=kind,
                    name=ep.name,
                    value=ep.value,
                    version=getattr(dist, "version", None),
                    dist_name=(getattr(dist, "metadata", None) or {}).get("Name")
                    if dist is not None
                    else None,
                )
            )
    return found


_ENABLED: list[EnabledPlugin] = []


def _pin(version: str) -> str:
    return version[2:] if version.startswith("==") else version


def _record_hash(dist: Any) -> str:
    """``sha256:`` of the distribution's ``RECORD`` file (the installed-file manifest).

    Offline and stdlib only: the RECORD lists every installed file with its own hash, so pinning
    it pins the installed contents. A wheel file hash is not recomputable once installed.
    """
    text = dist.read_text("RECORD") if dist is not None else None
    if text is None:
        raise PluginHashError("distribution has no RECORD file to hash")
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _coerce(m: CapabilityManifest | Mapping[str, Any]) -> CapabilityManifest:
    return m if isinstance(m, CapabilityManifest) else CapabilityManifest.model_validate(dict(m))


def enable(
    pins: Iterable[CapabilityManifest | Mapping[str, Any]],
    *,
    load: Callable[[Any], Any] | None = None,
) -> list[EnabledPlugin]:
    """Enable the pinned plug-ins: validate every pin, then load and register them.

    Validation of all pins happens before any plug-in code is imported, so a bad config fails
    without side effects. Returns the plug-ins enabled by this call (also recorded for
    :func:`enabled_manifest`). An empty ``pins`` is a no-op.
    """
    manifests = [_coerce(p) for p in pins]
    if not manifests:
        return []
    check_enabled(manifests)  # duplicate name / duplicate slot -> DuplicateSlotError
    # A second engine in the same process re-lists the same pins: those are already enabled.
    already = {e.manifest.model_dump_json() for e in _ENABLED}
    manifests = [m for m in manifests if m.model_dump_json() not in already]
    if not manifests:
        return []
    load_registries()
    plan: list[tuple[CapabilityManifest, str, Any, Any]] = []
    for m in manifests:
        kind, sep, key = m.slot.partition(":")
        if not sep or kind not in KINDS or not key:
            raise PluginError(
                f"plug-in {m.name!r}: slot {m.slot!r} must be '<kind>:<name>' with kind in "
                f"{sorted(KINDS)}"
            )
        eps = [ep for ep in _entry_points(_GROUP_PREFIX + kind) if ep.name == m.name]
        if not eps:
            raise PluginNotFoundError(
                f"plug-in {m.name!r} is not advertised in entry-point group "
                f"'{_GROUP_PREFIX}{kind}' (discovered plug-ins are listed, not enabled)"
            )
        if len(eps) > 1:
            raise DuplicateSlotError(
                f"slot {m.slot!r} has two providers: " + " and ".join(repr(e.value) for e in eps)
            )
        ep = eps[0]
        registry = _REGISTRIES[kind]
        if key in registry:
            owner = "a built-in" if registry.is_builtin(key) else "an enabled plug-in"
            raise DuplicateSlotError(f"slot {m.slot!r} has two providers: {owner} and {m.name!r}")
        dist = _dist_of(ep)
        installed = getattr(dist, "version", None)
        if installed != _pin(m.version):
            raise PluginVersionError(
                f"plug-in {m.name!r} is pinned to {_pin(m.version)!r} "
                f"but {installed!r} is installed"
            )
        if m.hash is not None:
            actual = _record_hash(dist)
            if actual != m.hash:
                raise PluginHashError(
                    f"plug-in {m.name!r}: RECORD hash {actual} does not match the pinned {m.hash}"
                )
        plan.append((m, kind, ep, dist))

    done: list[EnabledPlugin] = []
    for m, kind, ep, _dist in plan:
        obj = load(ep) if load is not None else ep.load()
        declared = getattr(obj, "__memspine_lifetime__", "app")
        if declared not in _LIFETIMES or declared != m.lifetime:
            for e in done:  # leave nothing half-enabled
                _REGISTRIES[e.kind].unregister(e.manifest.slot.partition(":")[2])
            raise PluginLifetimeError(
                f"plug-in {m.name!r} declares lifetime {declared!r} but is pinned as {m.lifetime!r}"
            )
        key = m.slot.partition(":")[2]
        _REGISTRIES[kind].register(key, obj, builtin=False)
        _REGISTRIES[kind]._lifetime[key] = m.lifetime
        done.append(EnabledPlugin(m, kind, m.name, _pin(m.version)))
    _ENABLED.extend(done)
    return done


def disable_all() -> None:
    """Unregister every enabled plug-in (tests, engine shutdown)."""
    for e in _ENABLED:
        _REGISTRIES[e.kind].unregister(e.manifest.slot.partition(":")[2])
    _ENABLED.clear()


def enabled_manifest() -> list[dict[str, Any]]:
    """The enabled plug-ins as run-manifest rows: name, version, hash, slot, lifetime."""
    return [
        {
            "name": e.manifest.name,
            "version": e.installed_version,
            "hash": e.manifest.hash,
            "slot": e.manifest.slot,
            "lifetime": e.manifest.lifetime,
        }
        for e in _ENABLED
    ]
