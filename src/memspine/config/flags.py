"""Flag registry (F-07, decisions G-21 / G-22), generated from the config schema.

Every boolean field (a *flag*) and every string-enum field (a *mode*) of
:class:`~memspine.config.schema.MemspineConfig` is listed with its dotted YAML key, default,
area (the ``memspine.config.schema.<area>`` module that defines it), and evidence pointer.
:func:`registry` walks the schema, so a new field appears here without edits; only the evidence
needs writing.

Rules (G-22, plan rule 10), enforced by ``tests/unit/test_flag_evidence.py``:

* a flag whose schema default is ON carries evidence for that default: a commit, a run id or
  an ADR, in :data:`FLAG_EVIDENCE` (keys are ``"<ConfigClass>.<field>"``);
* an entry names a field that exists and is still default-on (retire the entry when the flag
  is retired or flipped);
* ``UNKNOWN (pre-G-22)`` is allowed only for :data:`PRE_G22_UNKNOWN` (frozen; it may only shrink).

"""

from __future__ import annotations

import types
import typing
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from memspine.config import schema
from memspine.config.requires import REQUIRES
from memspine.config.schema.base import ConfigModel

__all__ = [
    "FLAG_EVIDENCE",
    "PRE_G22_UNKNOWN",
    "UNKNOWN",
    "FlagSpec",
    "config_classes",
    "default_on_flags",
    "registry",
]

UNKNOWN = "UNKNOWN (pre-G-22)"

# Frozen on 2026-10-13. Never add to this set: a new default-on flag needs real evidence.
# Remove an entry here in the same change that replaces its UNKNOWN with evidence.
PRE_G22_UNKNOWN: frozenset[str] = frozenset(
    {
        "ReadConfig.perspective_leg",
        "ReadConfig.record_access",
        "MemspineConfig.strict_services",
    }
)

FLAG_EVIDENCE: dict[str, dict[str, str]] = {
    # --- read ---
    "ReadConfig.hybrid": {
        "evidence": "commit 011ef4a (v0.2 A3 default-on flip); ADR-019",
    },
    "ReadConfig.perspective_leg": {
        "evidence": UNKNOWN,
        "note": "commit fa81ad8 (I75b) introduced the flag at the then-current behaviour; "
        "no paired verdict found for the default",
    },
    "ReadConfig.record_access": {
        "evidence": UNKNOWN,
        "note": "commit 9a7c7c2 added the switch; the ADR-032/033 templates set it false "
        "for eval; the True default has no recorded verdict",
    },
    # --- write ---
    "WriteConfig.window_async": {
        "evidence": "commit 0eb2fd7 (W1 adaptive-window write); ADR-066",
        "note": "timing only: result identical to window_async=false (schema comment)",
    },
    "WriteConfig.window_cache": {
        "evidence": "commit 0eb2fd7 (W1 adaptive-window write); ADR-066",
        "note": "cache only: a re-run on the same history is free",
    },
    # --- firewall (memory firewall, N1) ---
    "FirewallConfig.enabled": {
        "evidence": "commit 8d45cfe (B8 firewall on/off switch); ADR-013",
        "note": "True reproduces the pre-B8 firewall",
    },
    "FirewallSignalsConfig.instruction": {
        "evidence": "commit 8882208 (W2/N20/N22 extended instruction patterns); ADR-058",
    },
    "FirewallSignalsConfig.anomaly": {
        "evidence": "commit 8882208 (W2/N20/N22 signals); ADR-058",
    },
    "FirewallSignalsConfig.minja_bridge": {
        "evidence": "commit 8882208 (W2/N20/N22 signals); ADR-058",
    },
    # --- integrity (trust-horizon invariant; inert unless integrity.enabled) ---
    "IntegrityConfig.trust_weighted_ranking": {
        "evidence": "commit 6af7234 (monotone trust invariant); ADR-029 (proposed)",
        "note": "inert while IntegrityConfig.enabled is False",
    },
    "IntegrityConfig.principal_bound_corroboration": {
        "evidence": "commit 6af7234 (monotone trust invariant); ADR-029 (proposed)",
        "note": "inert while IntegrityConfig.enabled is False",
    },
    "IntegrityConfig.merge_reinforcement_gate": {
        "evidence": "commit 6af7234 (monotone trust invariant); ADR-029 (proposed)",
        "note": "inert while IntegrityConfig.enabled is False",
    },
    # --- top level ---
    "MemspineConfig.strict_services": {
        "evidence": UNKNOWN,
        "note": "commit 7d17b8a (P0.2 layered config) introduced it; ADR-031 mentions it; "
        "validation behaviour, no measured verdict",
    },
    # --- runtime.shared_resources (RM-78) ---
    "SharedResourcesConfig.keep_warm": {
        "evidence": "ADR-079 (addendum + RM-80 table: owner-fixed, keep warm, no TTL)",
        "note": "model weights only; connections still close on their last release",
    },
    "SharedSqlPoolConfig.pool_pre_ping": {
        "evidence": "ADR-079 (addendum settings table: sql.pool_pre_ping true)",
        "note": "SQLAlchemy pre-ping on checkout; Postgres and file SQLite pools",
    },
    # --- models.profiles.*.settings (GLiNER profile, RM-81) ---
    "GlinerSettings.local_files_only": {
        "evidence": "commit 47526490 (named GLiNER profiles); ADR-074 (explicit offline policy)",
        "note": "missing local artifacts fail; a download needs the profile to set it false",
    },
}

#: Strict native settings that live inside an opaque ``settings`` mapping but are validated
#: by the config schema itself (not by a provider descriptor). The registry walks them under
#: their wildcard key so their flags are visible and evidenced like any other.
_PROFILE_SETTINGS: tuple[tuple[str, type[BaseModel]], ...] = (
    ("models.profiles.*.settings.", schema.GlinerSettings),
)


@dataclass(frozen=True)
class FlagSpec:
    """One flag (``kind == "flag"``, a bool) or mode (``kind == "mode"``, a string enum)."""

    key: str  # dotted YAML key, e.g. "read.hybrid"
    owner: str  # "<ConfigClass>.<field>", the evidence-register key
    area: str  # schema area module, e.g. "read"
    kind: str  # "flag" | "mode"
    default: Any
    choices: tuple[str, ...] = ()  # modes only
    evidence: str = ""  # "" when none recorded (only default-on flags need it)
    note: str = ""
    #: FU-REQS: prerequisites that must hold while this flag is on (``config/requires.py``),
    #: as ``"<dotted key>"`` or ``"<dotted key>: <value>"``; a violation is a ConfigError.
    requires: tuple[str, ...] = ()

    @property
    def default_on(self) -> bool:
        return self.kind == "flag" and self.default is True

    @property
    def unknown(self) -> bool:
        return self.evidence == UNKNOWN


def config_classes() -> Iterator[tuple[str, type[BaseModel]]]:
    """Pipeline config models, excluding descriptor-owned native settings.

    Provider-native fields live inside opaque selected settings mappings and
    have their own defaults/schema catalog. They are not global feature flags
    and cannot be reached by the root flag registry's dotted-key walk.
    """
    for name, cls in vars(schema).items():
        if (
            isinstance(cls, type)
            and issubclass(cls, ConfigModel)
            and cls.__module__.startswith(schema.__name__ + ".")
        ):
            yield name, cls


def _model_of(annotation: Any) -> type[BaseModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None


def _literal_choices(annotation: Any) -> tuple[str, ...]:
    if typing.get_origin(annotation) is Literal:
        args = typing.get_args(annotation)
        if args and all(isinstance(a, str) for a in args):
            return tuple(args)
    return ()


def _is_bool(annotation: Any) -> bool:
    if annotation is bool:
        return True
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        return args == [bool]
    return False


def _walk(model: type[BaseModel], prefix: str) -> Iterator[FlagSpec]:
    area = model.__module__.rsplit(".", 1)[-1]
    for fname, field in model.model_fields.items():
        key = f"{prefix}{fname}"
        sub = _model_of(field.annotation)
        if sub is not None:
            yield from _walk(sub, key + ".")
            continue
        owner = f"{model.__name__}.{fname}"
        entry = FLAG_EVIDENCE.get(owner, {})
        if _is_bool(field.annotation):
            kind, choices = "flag", ()
        else:
            choices = _literal_choices(field.annotation)  # type: ignore[assignment]  # CI-GREEN: pre-existing typing debt
            if not choices:
                continue
            kind = "mode"
        yield FlagSpec(
            key=key,
            owner=owner,
            area=area,
            kind=kind,
            default=field.default,
            choices=choices,
            evidence=entry.get("evidence", ""),
            note=entry.get("note", ""),
            requires=tuple(n.describe() for n in REQUIRES.get(key, ())),
        )


def registry() -> dict[str, FlagSpec]:
    """Dotted YAML key -> :class:`FlagSpec` for every flag and mode of ``MemspineConfig``."""
    specs = list(_walk(schema.MemspineConfig, ""))
    for prefix, model in _PROFILE_SETTINGS:
        specs.extend(_walk(model, prefix))
    return {spec.key: spec for spec in specs}


def default_on_flags() -> set[str]:
    """``"<ConfigClass>.<field>"`` of every bool config field whose default is True, over all
    config classes (not only those reachable from the root)."""
    return {
        f"{name}.{fname}"
        for name, cls in config_classes()
        for fname, field in cls.model_fields.items()
        if field.default is True
    }
