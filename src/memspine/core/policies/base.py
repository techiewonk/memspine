"""Policy protocol + config-binding helper.

A policy is pure decision logic bound to validated options. Policies never do
I/O — memories and pipelines call them with data and act on the result. Options
models use ``extra="forbid"`` so a typo in a template fails at config time.
"""

from __future__ import annotations

from typing import ClassVar, Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict

from memspine.exceptions import ConfigError

__all__ = ["Policy", "PolicyOptions", "bind_policy"]


class PolicyOptions(BaseModel):
    """Base for every policy's options; rejects unknown keys at bind time."""

    model_config = ConfigDict(extra="forbid")


@runtime_checkable
class Policy(Protocol):
    """Minimal shape shared by all policies."""

    name: ClassVar[str]


class BindablePolicy:
    """Base class wiring an ``Options`` model to the D-14 policy-override channel."""

    name: ClassVar[str] = ""
    Options: ClassVar[type[PolicyOptions]] = PolicyOptions

    def __init__(self, options: PolicyOptions) -> None:
        self.options = options

    @classmethod
    def bind(cls, raw_options: dict[str, object] | None = None) -> Self:
        """Validate raw config options and construct the policy."""
        try:
            options = cls.Options.model_validate(raw_options or {})
        except Exception as exc:
            raise ConfigError(f"invalid options for policy {cls.name!r}: {exc}") from exc
        return cls(options)


def bind_policy(policy_cls: type[BindablePolicy], raw: dict[str, object] | None) -> BindablePolicy:
    return policy_cls.bind(raw)
