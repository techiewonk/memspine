"""CI gate (X-02, G-22): every default-on config flag carries evidence.

The register is ``scripts/ci_flag_evidence_register.py``. See its docstring for the rules.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
from pydantic import BaseModel

from memspine.config import schema

REPO = Path(__file__).resolve().parents[2]
REGISTER_PATH = REPO / "scripts" / "ci_flag_evidence_register.py"

# a commit hash or an ADR id counts as evidence; so does a named run id ("run <id>")
_EVIDENCE_REF = re.compile(r"\b(?:[0-9a-f]{7,40}|ADR-\d{3}|run[ _][\w.-]+)\b", re.IGNORECASE)


def _load_register():
    spec = importlib.util.spec_from_file_location("ci_flag_evidence_register", REGISTER_PATH)
    assert spec
    assert spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _config_classes():
    for name, cls in vars(schema).items():
        if (
            isinstance(cls, type)
            and issubclass(cls, BaseModel)
            and cls.__module__ == schema.__name__
        ):
            yield name, cls


def default_on_flags() -> set[str]:
    """``Class.field`` for every boolean config field whose default is True."""
    return {
        f"{name}.{fname}"
        for name, cls in _config_classes()
        for fname, field in cls.model_fields.items()
        if field.default is True
    }


def all_fields() -> set[str]:
    return {f"{name}.{f}" for name, cls in _config_classes() for f in cls.model_fields}


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
    reg = _load_register()
    problems = check(
        default_on_flags(), all_fields(), reg.FLAG_EVIDENCE, reg.PRE_G22_UNKNOWN, reg.UNKNOWN
    )
    assert not problems, "flag-evidence violations (G-22):\n  " + "\n  ".join(problems)


def test_discovery_finds_known_flags():
    assert {"ReadConfig.hybrid", "FirewallConfig.enabled"} <= default_on_flags()


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
