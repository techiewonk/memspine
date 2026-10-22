"""CI gate (X-02, G-22): every default-on config flag carries evidence.

The register is ``memspine.config.flags`` (F-07; it was ``scripts/ci_flag_evidence_register.py``).
See its docstring for the rules.
"""

from __future__ import annotations

import re

import pytest

from memspine.config import flags

# a commit hash or an ADR id counts as evidence; so does a named run id ("run <id>")
_EVIDENCE_REF = re.compile(r"\b(?:[0-9a-f]{7,40}|ADR-\d{3}|run[ _][\w.-]+)\b", re.IGNORECASE)


def default_on_flags() -> set[str]:
    """``Class.field`` for every boolean config field whose default is True."""
    return flags.default_on_flags()


def all_fields() -> set[str]:
    return {f"{name}.{f}" for name, cls in flags.config_classes() for f in cls.model_fields}


def check(
    on: set[str], fields: set[str], register: dict, unknown_ok: frozenset, marker: str
) -> list[str]:
    """Return the list of problems (empty = pass)."""
    problems: list[str] = []
    for flag in sorted(on - set(register)):
        problems.append(f"default-on flag {flag} has no entry in the evidence register")
    for flag in sorted(set(register) - fields):
        problems.append(f"register entry {flag} names a flag that no longer exists (retire it)")
    for flag in sorted((set(register) & fields) - on):
        problems.append(f"register entry {flag} is no longer default-on (retire the entry)")
    for flag, entry in sorted(register.items()):
        ev = entry.get("evidence", "")
        if ev == marker:
            if flag not in unknown_ok:
                problems.append(f"{flag}: UNKNOWN evidence is allowed only for the frozen list")
        elif not _EVIDENCE_REF.search(ev):
            problems.append(f"{flag}: evidence {ev!r} names no commit, run id or ADR")
    for flag in sorted(unknown_ok - set(register)):
        problems.append(f"PRE_G22_UNKNOWN lists {flag} but it has no register entry (shrink it)")
    for flag in sorted(unknown_ok & set(register)):
        if register[flag].get("evidence") != marker:
            problems.append(f"{flag} now has evidence: remove it from PRE_G22_UNKNOWN")
    return problems


def test_default_on_flags_have_evidence():
    problems = check(
        default_on_flags(),
        all_fields(),
        flags.FLAG_EVIDENCE,
        flags.PRE_G22_UNKNOWN,
        flags.UNKNOWN,
    )
    assert not problems, "flag-evidence violations (G-22):\n  " + "\n  ".join(problems)


def test_registry_agrees_with_class_discovery():
    """The schema-walk registry sees every default-on bool the class scan sees."""
    reg = flags.registry()
    assert {s.owner for s in reg.values() if s.default_on} == default_on_flags()
    assert {s.key for s in reg.values()} >= {"read.hybrid", "firewall.enabled", "strict_services"}
    modes = {k for k, s in reg.items() if s.kind == "mode"}
    assert modes
    assert all(reg[k].choices for k in modes)


def test_registry_areas_are_schema_modules():
    areas = {s.area for s in flags.registry().values()}
    assert areas <= {
        "storage", "llm", "read", "write", "firewall", "integrity",
        "privacy", "observability", "rest", "root", "offline",
        "isolation", "runtime", "economy", "settings",
    }  # fmt: skip


def test_removed_fields_use_strict_schema_errors():
    from memspine.config.loader import load_config
    from memspine.exceptions import ConfigError

    with pytest.raises(ConfigError, match=r"storage\.path \(extra_forbidden\)"):
        load_config(template="base", overrides={"storage": {"path": ":memory:"}})


def test_future_lexical_provider_name_is_not_reserved_by_the_schema():
    from memspine.config.loader import load_config

    config = load_config(
        template="base", overrides={"storage": {"lexical": {"provider": "opensearch"}}}
    )
    assert config.config.storage.lexical.provider == "opensearch"


def test_removed_relevance_gate_mode_is_rejected_by_its_enum():
    from memspine.config.loader import load_config
    from memspine.exceptions import ConfigError

    with pytest.raises(ConfigError, match="relevance_gate"):
        load_config(template="base", overrides={"read": {"relevance_gate": "decider"}})


def test_discovery_finds_known_flags():
    assert {"ReadConfig.hybrid", "FirewallConfig.enabled"} <= default_on_flags()


def test_native_provider_settings_are_not_pipeline_feature_flags():
    from memspine.providers.configuration import provider_settings_reference

    assert "OpenSearchSettings.verify_certs" not in default_on_flags()
    assert "PgvectorSettings.namespace_index" not in default_on_flags()
    assert provider_settings_reference("lexical", "opensearch")["defaults"]["verify_certs"] is True
    assert provider_settings_reference("vector", "pgvector")["defaults"]["namespace_index"] is True


# --- checker self-tests -----------------------------------------------------------------

M = "UNKNOWN (pre-G-22)"


def test_missing_entry_fails():
    assert check({"A.x"}, {"A.x"}, {}, frozenset(), M)


def test_stale_entry_fails():
    reg = {"A.x": {"evidence": "ADR-001"}}
    assert check(set(), set(), reg, frozenset(), M)  # flag gone
    assert check(set(), {"A.x"}, reg, frozenset(), M)  # flag no longer default-on


def test_unknown_only_for_frozen_list():
    reg = {"A.x": {"evidence": M}}
    assert check({"A.x"}, {"A.x"}, reg, frozenset(), M)
    assert not check({"A.x"}, {"A.x"}, reg, frozenset({"A.x"}), M)


def test_frozen_list_must_shrink_when_evidenced():
    reg = {"A.x": {"evidence": "commit abcdef1"}}
    assert check({"A.x"}, {"A.x"}, reg, frozenset({"A.x"}), M)


def test_evidence_must_name_a_reference():
    assert check({"A.x"}, {"A.x"}, {"A.x": {"evidence": "looks fine"}}, frozenset(), M)
    assert not check({"A.x"}, {"A.x"}, {"A.x": {"evidence": "ADR-019"}}, frozenset(), M)
    assert not check({"A.x"}, {"A.x"}, {"A.x": {"evidence": "commit 011ef4a"}}, frozenset(), M)
    assert not check({"A.x"}, {"A.x"}, {"A.x": {"evidence": "run f-chain-i2"}}, frozenset(), M)


@pytest.mark.parametrize("bad", ["", "n/a"])
def test_empty_evidence_fails(bad):
    assert check({"A.x"}, {"A.x"}, {"A.x": {"evidence": bad}}, frozenset(), M)
