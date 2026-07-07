"""Policy protocol + config-binding helper.

A policy is pure decision logic bound to validated options. Policies never do
I/O — memories and pipelines call them with data and act on the result. Options
models use ``extra="forbid"`` so a typo in a template fails at config time.
"""

from __future__ import annotations

from typing import ClassVar, Self

from pydantic import BaseModel, ConfigDict

from memspine.exceptions import ConfigError

__all__ = ["BindablePolicy", "PolicyOptions"]


class PolicyOptions(BaseModel):
    """Base for every policy's options; rejects unknown keys at bind time."""

    model_config = ConfigDict(extra="forbid")


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
