"""Pure resolution helpers for named model profiles.

These functions validate and translate configuration data only. Provider SDKs and model
implementations remain owned by runtime wiring and are never imported here.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Literal

from memspine.config.schema.llm import GlinerSettings, LLMRoleConfig, ModelProfile
from memspine.config.schema.root import MemspineConfig

GlinerConsumerRole = Literal["gliner_decision", "gliner_semantic", "gliner_prepass"]

__all__ = [
    "GlinerConsumerRole",
    "bound_llm_roles",
    "resolve_gliner_settings",
    "resolve_llm_profile",
]


def resolve_llm_profile(config: MemspineConfig, name: str) -> LLMRoleConfig:
    """Resolve a named LLM profile to the existing provider-role config shape."""
    profile = _profile(config, name, "llm")
    return LLMRoleConfig(provider=profile.provider, settings=deepcopy(profile.settings))


def bound_llm_roles(config: MemspineConfig) -> dict[str, LLMRoleConfig]:
    """Return all effective inline and profile-bound LLM roles.

    Inline roles remain a supported explicit option. The config root rejects a role being set
    both inline and through a profile binding, so the merge is unambiguous.
    """
    resolved = dict(config.llm.roles)
    for role, profile_name in config.models.bindings.items():
        profile = config.models.profiles[profile_name]
        if profile.kind == "llm":
            resolved[role] = resolve_llm_profile(config, profile_name)
    return resolved


def resolve_gliner_settings(config: MemspineConfig, role: GlinerConsumerRole) -> GlinerSettings:
    """Resolve one GLiNER consumer binding to its strictly validated native settings."""
    profile_name = config.models.bindings.get(role)
    if profile_name is None:
        raise ValueError(f"no model profile is bound to GLiNER consumer {role!r}")
    profile = _profile(config, profile_name, "gliner")
    return GlinerSettings.model_validate(profile.settings)


def _profile(config: MemspineConfig, name: str, kind: Literal["llm", "gliner"]) -> ModelProfile:
    try:
        profile = config.models.profiles[name]
    except KeyError:
        raise ValueError(f"unknown model profile {name!r}") from None
    if profile.kind != kind:
        raise ValueError(f"model profile {name!r} must have kind {kind!r}")
    if kind == "gliner" and profile.provider != "gliner2":
        raise ValueError(f"GLiNER profile {name!r} requires provider 'gliner2'")
    return profile
