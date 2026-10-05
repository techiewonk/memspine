"""#45: the server templates ship secure defaults; the others are untouched."""

from __future__ import annotations

import pytest

from memspine.config.loader import load_config
from memspine.config.schema import MemspineConfig


def _config(template: str) -> MemspineConfig:
    return load_config(template=template, env={}).config


@pytest.mark.parametrize("template", ["multi_agent", "regulated_financial"])
def test_server_templates_turn_on_gate_redaction_and_wrapper(template: str) -> None:
    config = _config(template)
    assert config.integrity.enabled
    assert config.integrity.untrusted_wrap_below == 0.5
    assert config.firewall.redact_secrets
    assert config.firewall.pii == "redact"


@pytest.mark.parametrize("template", ["core", "base", "assistant", "personal", "coding", "voice"])
def test_other_templates_keep_the_schema_defaults(template: str) -> None:
    config = _config(template)
    defaults = MemspineConfig()
    assert config.integrity == defaults.integrity
    assert config.firewall == defaults.firewall
