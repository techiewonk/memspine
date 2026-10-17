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
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from memspine.exceptions import ConfigError
from memspine.providers.registry import (
    KIND_TO_CATEGORY,
    KINDS,
    ProviderDescriptor,
    ProviderRegistryError,
    Registry,
    UnknownProviderError,
)
from memspine.providers.registry import (
    get_registry as central_get_registry,
)
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
    "PluginContractError",
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


class PluginContractError(PluginError):
    """A provider entry point does not implement the supported descriptor contract."""


def load_registries() -> None:
    """Import every package registry module so all kinds exist (built-ins register at import)."""
    for module in dict.fromkeys(KINDS.values()):
        importlib.import_module(module)


def get_registry(kind: str) -> Registry[Any]:
    load_registries()
    try:
        return central_get_registry(kind)
    except ProviderRegistryError as exc:
        raise PluginError(str(exc)) from None


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
        registry = central_get_registry(kind)
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
                central_get_registry(e.kind).unregister(e.manifest.slot.partition(":")[2])
            raise PluginLifetimeError(
                f"plug-in {m.name!r} declares lifetime {declared!r} but is pinned as {m.lifetime!r}"
            )
        key = m.slot.partition(":")[2]
        if kind in KIND_TO_CATEGORY:
            try:
                _validate_provider_plugin(kind, key, obj, m)
            except PluginContractError:
                for enabled in done:
                    central_get_registry(enabled.kind).unregister(
                        enabled.manifest.slot.partition(":")[2]
                    )
                raise
        central_get_registry(kind).register(key, obj, builtin=False)
        central_get_registry(kind)._lifetime[key] = m.lifetime
        done.append(EnabledPlugin(m, kind, m.name, _pin(m.version)))
    _ENABLED.extend(done)
    return done


def _validate_provider_plugin(
    kind: str,
    key: str,
    value: Any,
    manifest: CapabilityManifest,
) -> None:
    """Reject callable-era factories; provider plugins must export a typed descriptor."""
    if not isinstance(value, ProviderDescriptor):
        raise PluginContractError(
            f"plug-in {manifest.name!r} for {kind}:{key} must export a ProviderDescriptor "
            "with an async build(settings, ctx) method (provider contract v1)"
        )
    expected_category = KIND_TO_CATEGORY[kind]
    if value.ref.category != expected_category or value.ref.name != key:
        raise PluginContractError(
            f"plug-in {manifest.name!r} descriptor identity must be {expected_category}:{key}"
        )
    if value.ref.contract_version != 1:
        raise PluginContractError(
            f"plug-in {manifest.name!r} uses unsupported {expected_category} provider "
            f"contract version {value.ref.contract_version}; supported version is 1"
        )


def disable_all() -> None:
    """Unregister every enabled plug-in (tests, engine shutdown)."""
    for e in _ENABLED:
        central_get_registry(e.kind).unregister(e.manifest.slot.partition(":")[2])
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
