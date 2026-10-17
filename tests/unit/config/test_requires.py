"""FU-REQS: every flag prerequisite is a check (audit 2026-10-13, finding 1)."""

from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest

from memspine.config import flags
from memspine.config.requires import REQUIRES, Need, check_requires, is_active, resolve_path
from memspine.config.schema import MemspineConfig
from memspine.exceptions import ConfigError

USAGE = Path(__file__).resolve().parents[3] / "docs" / "USAGE.md"


def _nest(path: str, value: object) -> dict:
    out: dict = {}
    node = out
    parts = path.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
    return out


def _merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _on_value(key: str) -> object:
    """A value that switches ``key`` on (a flag -> True; a mode -> its first non-off choice)."""
    spec = flags.registry().get(key)
    if spec is not None and spec.kind == "mode":
        return next(c for c in spec.choices if c not in ("off", "none"))
    if key == "economy.budget":
        return {"max_calls": 10}
    return True


def _satisfy(need: Need) -> dict:
    if need.values is not None:
        return _nest(need.path, need.values[0])
    if need.path.startswith("llm.roles."):
        return _nest(need.path, {"model": "fake/model"})
    return _nest(need.path, _on_value(need.path))


def _is_on(raw: dict, path: str, values: tuple | None = None) -> bool:
    node: object = raw
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return node in values if values is not None else is_active(node)


def _close(raw: dict, forbidden: str) -> dict:
    """Satisfy, transitively, the hard prerequisites of every key that is on in ``raw`` except the
    one path under test (``forbidden``)."""
    for _ in range(8):
        changed = False
        for key, needs in REQUIRES.items():
            if not _is_on(raw, key):
                continue
            for n in needs:
                if n.soft or n.path == forbidden or _is_on(raw, n.path, n.values):
                    continue
                raw = _merge(raw, _satisfy(n))
                changed = True
        if not changed:
            return raw
    raise AssertionError("prerequisite cycle")


def test_defaults_are_valid_and_every_required_flag_is_off() -> None:
    cfg = MemspineConfig()
    check_requires(cfg, strict=True)
    for key in REQUIRES:
        assert not is_active(resolve_path(cfg, key)), f"{key} must default to off"


#: count keys (0 = off): not in the flag registry, which lists bools and Literal modes only.
_COUNT_KEYS = {"economy.budget", "read.rerank_v2_search_pool"}


def test_every_declared_key_exists() -> None:
    reg = flags.registry()
    for key, needs in REQUIRES.items():
        assert key in reg or key in _COUNT_KEYS, f"{key} is not a flag of the schema"
        for need in needs:
            for path in (need.path, *need.alt):
                assert path in reg or path.startswith(("llm.roles.", "workers.", "economy.")), path


def test_the_registry_exposes_the_requirements() -> None:
    reg = flags.registry()
    assert reg["write.temporal_v2"].requires == ("write.envelope_v2",)
    assert reg["write.envelope_v2"].requires == (
        "write.window_extract: 'facts' or 'facts_graph' or 'all'",
    )
    assert reg["read.legs_v2"].requires == ()


def test_finding_1_dependencies_are_all_declared() -> None:
    for flag in (
        "temporal_v2",
        "integration_v2",
        "grounding_v2",
        "coverage_v2",
        "resolver_v2",
        "prompt_v2",
    ):
        assert [n.path for n in REQUIRES[f"write.{flag}"]] == ["write.envelope_v2"]
    assert [n.path for n in REQUIRES["read.graph_leg_v2"]] == [
        "read.legs_v2",
        "graph.edge_compiler_v2",
    ]
    assert [n.path for n in REQUIRES["read.episode_leg_v2"]] == ["read.legs_v2"]
    assert [n.path for n in REQUIRES["read.standing_v2"]] == ["offline.standing_v2"]
    assert [n.path for n in REQUIRES["offline.profile_card_v2"]] == ["offline.session_close_v2"]
    assert [n.path for n in REQUIRES["write.segmentation_v2"]] == ["write.window_extract"]


def _cases() -> list[tuple[str, Need]]:
    return [(key, need) for key, needs in REQUIRES.items() for need in needs]


@pytest.mark.parametrize(
    ("key", "need"), _cases(), ids=lambda v: v if isinstance(v, str) else v.path
)
def test_each_requirement_raises_naming_the_missing_key_and_holds_when_met(
    key: str, need: Need
) -> None:
    raw = _nest(key, _on_value(key))
    for other in REQUIRES[key]:
        if other is not need and not other.soft:
            raw = _merge(raw, _satisfy(other))
    raw = _close(raw, need.path)
    if need.values is None and is_active(resolve_path(MemspineConfig(), need.path)):
        raw = _merge(raw, _nest(need.path, False))  # default-on prerequisite: switch it off
    if need.soft:
        check_requires(MemspineConfig.model_validate(raw))  # a soft need is not a config error
        with pytest.raises(ConfigError, match=re.escape(need.path)):
            check_requires(MemspineConfig.model_validate(raw), strict=True)
    else:
        with pytest.raises(ConfigError) as err:
            MemspineConfig.model_validate(raw)
        assert key in str(err.value)
        assert need.path in str(err.value)
    met = _close(_merge(raw, _satisfy(need)), "")
    check_requires(MemspineConfig.model_validate(met), strict=True)


# --- USAGE.md rows that say "needs X" ----------------------------------------------------------
#: "needs <flag>" rows of USAGE.md that are not flag prerequisites (extras, env, data, or
#: behaviour that degrades safely), or that stay a comment for a stated reason.
EXEMPT: dict[str, str] = {
    "write.learned_persist": "persists kinds (envelope_v2) OR pre-pass aliases (prepass); either alone is valid",
    "write.window_extract": "needs an LLM role; checked at engine start (D-10)",
    "read.asset_evidence": "already a root validator (_asset_keys_agree)",
    "read.core_terms_leg": "needs the lexical store; read.hybrid is on by default",
    "embedding.migrate_profile": "versioned_spaces check lives in the embedding profile gate (MD-02)",
    "graph.neo4j_outbox": "needs storage.profile server OR graph.backend neo4j; an either/or the table cannot express yet",
    "graph.backend": "same either/or as graph.neo4j_outbox",
    "firewall.hearsay_trust_cap": "needs a perspective policy; inert tags only, degrades safely",
    "read.perspective_as_of_subject": "needs an as-of read at call time, not a config switch",
    "read.completeness_check": "needs a bound sufficiency or plan role; falls back (completeness_unbound)",
    "offline.budget_v2": "needs sleep_v2 only for the spend cap; the covers_seq stamping (O4-01b) works alone",
    "read.freshness_v2": "stamps come from budget_v2 OR freshness_v2 itself (engine.py tracker); works alone",
    "write.online_extractor_v2": "needs envelope_v2 only for the per-stage counters; the no-model skip works without",
    "runtime.scheduler_v2": "needs workers.outbox only for its backpressure part; the micro-batched executors do not",
}


def _usage_rows() -> list[tuple[str, str]]:
    rows = []
    for line in USAGE.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\|\s*`([a-z_.0-9]+)`\s*\|(.*)", line)
        if m:
            rows.append((m.group(1), m.group(2)))
    return rows


def test_every_usage_row_that_says_needs_a_flag_is_declared_or_exempt() -> None:
    reg = flags.registry()
    short: dict[str, list[str]] = {}
    for key in reg:
        short.setdefault(key.rsplit(".", 1)[-1], []).append(key)
    problems = []
    for key, text in _usage_rows():
        if key not in reg or key in EXEMPT:
            continue
        for ref in re.findall(r"[Nn]eeds `([a-z_.0-9]+)`", text):
            target = (
                ref
                if ref in reg
                else (short.get(ref, [None]) if len(short.get(ref, [])) == 1 else [None])[0]
            )
            if target is None or reg[target].kind != "flag":
                continue  # an extra, an env var, a mode value or an ambiguous name
            declared = {n.path for n in REQUIRES.get(key, ())}
            declared |= {a for n in REQUIRES.get(key, ()) for a in n.alt}
            if target not in declared:
                problems.append(
                    f"{key} says needs `{ref}` ({target}) but REQUIRES does not declare it"
                )
    assert not problems, "\n".join(problems)
