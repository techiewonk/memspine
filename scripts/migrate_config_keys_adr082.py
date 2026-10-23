"""Codemod: move pre-ADR-082 model-selection keys into the ``model`` block.

ADR-082 / RM-117 WP4. There are no aliases (ADR-068), so every config shape in the tree must
move. The authoritative map is section 1 of ``docs/MODEL_SLOTS_MIGRATION_PLAN.md``::

    embedding.*                              -> model.embedding.* (profile -> preset, ...)
    models.embedder_profile (cpu|gpu)        -> model.embedding.preset (auto = unset)
    llm.roles.<role>                         -> model.llm.<role>
    llm.structured                           -> copied into every model.llm.<role>.structured
    models.bindings.<role> + models.profiles -> inline slots (model.llm / extraction / decider)
    models.thinking / escalation.<role>      -> model.llm.<role>.thinking / .escalation
    models.topic                             -> model.extraction.topic.{labels,min_confidence}
    decision.provider gliner2                -> model.decider.route (off = no slot)
    read.decider* (opendecider)              -> model.decider.read (heuristic = no slot)
    read.rerank* / rerank_v2*                -> model.rerank.{chain, pool, ...}
    ingest.asset_vision* (ollama)            -> model.vision (none = no slot)
    models.<planning keys>                   -> model.placement.*
    warm-up embedder / reranker / gliner     -> embedding / rerank / extraction

Input forms: Python sources (dict literals, ``Engine(...)`` / config keyword arguments, item
assignments and ``setdefault`` chains, attribute paths, dotted key strings, environment
variable names), YAML and JSON files, and ``.env`` files (variable names only).

Usage::

    python scripts/migrate_config_keys_adr082.py [--check | --dry-run] PATH...
    python scripts/migrate_config_keys_adr082.py --key read.rerank_blend --key MEMSPINE_LLM__ROLES

``--check`` writes nothing and exits 1 when a file would change; ``--dry-run`` writes nothing
and prints a unified diff. A file containing ``migrate_config_keys_adr082: skip`` is left
untouched (tests of the old-key rejections). A second run is a no-op.

Python entries that are not moved keep their source text and comments; rebuilt values (the
new ``model`` mapping) are rendered on one line (run ``ruff format`` afterwards). YAML files
are rewritten through PyYAML and therefore lose comments; they are listed. A key the map does
not know, and a site the codemod cannot prove (a config held in a variable, YAML inside a
string, a binding to a profile defined elsewhere), is left in place and listed as ``manual:``.
"""

from __future__ import annotations

import argparse
import ast
import copy
import difflib
import json
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: A file containing this marker is left untouched (it tests the old keys on purpose).
SKIP_MARKER = "migrate_config_keys_adr082: skip"

EMBEDDING_RENAME = {
    "provider": "provider",
    "settings": "settings",
    "batch_size": "batch_size",
    "versioned_spaces": "versioned_spaces",
    "profile": "preset",
    "pin_profile": "pin_preset",
    "migrate_profile": "migrate_preset",
}
PLACEMENT_KEYS = (
    "tiers_v2",
    "catalogue",
    "gpus",
    "detect_gpus",
    "ram_mb",
    "gpu_headroom_mb",
    "role_tier",
    "pins",
    "allow_hosted",
    "required_roles",
    "on_missing_model",
)
PLACEMENT_RENAME = {
    **{k: k for k in PLACEMENT_KEYS},
    "default_rerankers": "catalogue_rerankers",
    "gliner_max_models": "max_loaded_models",
}
MODELS_KNOWN = {
    *PLACEMENT_RENAME,
    "embedder_profile",
    "rerank",
    "profiles",
    "bindings",
    "escalation",
    "thinking",
    "topic",
}
#: GLiNER binding role -> (category, consumer) under ``model``.
GLINER_TARGET = {
    "gliner_semantic": ("extraction", "semantic"),
    "gliner_prepass": ("extraction", "prepass"),
    "gliner_topic": ("extraction", "topic"),
    "gliner_sensitivity": ("extraction", "sensitivity"),
    "gliner_decision": ("decider", "route"),
}
RERANK_SUFFIXES = (
    "max_top_k",
    "keep",
    "blend",
    "gate",
    "context",
    "date_prefix",
    "balanced",
    "chunk_chars",
    "chunk_overlap",
    "floor",
)
READ_RERANK_RENAME = {
    **{f"rerank_{k}": k for k in RERANK_SUFFIXES},
    "rerank_v2_pool": "pool",
    "rerank_v2_search_pool": "search_pool",
    "skip_rerank_for_ordering": "skip_for_ordering",
}
READ_RERANK_SELECTOR = ("rerank", "rerank_settings", "rerank_v2", "rerank_v2_chain")
DECIDER_SETTINGS = {
    "decider_model": "model",
    "decider_device": "device",
    "decider_threads": "threads",
    "decider_backend": "backend",
    "decider_dtype": "dtype",
}
DECIDER_TYPED = {
    "decider_tasks": "tasks",
    "decider_min_confidence": "min_confidence",
    "decider_workers": "workers",
}
READ_MOVED = {
    *READ_RERANK_RENAME,
    *READ_RERANK_SELECTOR,
    "decider",
    *DECIDER_SETTINGS,
    *DECIDER_TYPED,
}
VISION_KEYS = ("asset_vision", "asset_vision_model", "asset_vision_url", "asset_vision_timeout_s")
WARMUP_RENAME = {"embedder": "embedding", "reranker": "rerank", "gliner": "extraction"}
CATALOGUE_KIND = {"reranker": "rerank", "embedder": "embedding"}
#: ``read.rerank_v2_chain`` default (schema/read.py) when ``rerank_v2: true`` names none.
V2_DEFAULT_CHAIN = ("jina", "qwen3", "fastembed")
#: Old provider defaults written explicitly into ``settings`` so behaviour does not depend on
#: the new descriptor defaults (schema/read.py ``decider_model``, schema/write.py vision).
DECIDER_DEFAULT_MODEL = "manjunathshiva/opendecider-nano"
VISION_DEFAULT_MODEL = "qwen2.5vl:3b"
VISION_DEFAULT_URL = "http://localhost:11434"
MODEL_ORDER = ("embedding", "llm", "rerank", "extraction", "decider", "vision", "placement")
OLD_ROOTS = ("embedding", "llm", "models", "decision")
ROOT_TRIGGERS = (*OLD_ROOTS, "read", "ingest")
#: Config classes removed or narrowed by ADR-082 WP1: a direct call is reviewed by hand.
REMOVED_CLASSES = {
    "EmbeddingConfig",
    "LLMConfig",
    "LLMRoleConfig",
    "ModelsConfig",
    "ModelProfile",
    "DecisionConfig",
    "StructuredConfig",
    "EscalationPolicy",
    "TopicConfig",
}


# ── values ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Src:
    """An opaque Python value kept as its source text."""

    text: str


class Tup(list[Any]):
    """A Python tuple literal (rendered back with parentheses)."""


class _Unknown:
    def __repr__(self) -> str:
        return "<unknown>"


UNKNOWN: Any = _Unknown()


def lit(value: Any) -> Any:
    """The plain value of a leaf (``UNKNOWN`` for a Python expression that is not literal)."""
    if isinstance(value, Src):
        try:
            return ast.literal_eval(value.text)
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            return UNKNOWN
    if isinstance(value, list):
        items = [lit(v) for v in value]
        return UNKNOWN if any(i is UNKNOWN for i in items) else items
    return value


def render(value: Any, quote: str = '"') -> str:
    """Render a migrated value as Python source."""
    if isinstance(value, Src):
        return value.text
    if isinstance(value, dict):
        body = ", ".join(f"{_q(k, quote)}: {render(v, quote)}" for k, v in value.items())
        return "{" + body + "}"
    if isinstance(value, Tup):
        parts = [render(v, quote) for v in value]
        return "(" + ", ".join(parts) + ("," if len(parts) == 1 else "") + ")"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(render(v, quote) for v in value) + "]"
    if isinstance(value, str):
        return _q(value, quote)
    return repr(value)


def _q(text: str, quote: str) -> str:
    if quote == "'":
        return repr(text)
    return json.dumps(text, ensure_ascii=False)


# ── the mapping transform (shared by Python, YAML and JSON) ────────────────────────────


@dataclass
class Migration:
    """State of one root mapping's migration."""

    data: dict[str, Any]
    warmup: bool = True
    notes: list[str] = field(default_factory=list)
    model: dict[str, Any] = field(default_factory=dict)

    def note(self, text: str) -> None:
        self.notes.append(text)

    def put(self, path: tuple[str, ...], value: Any, origin: str) -> bool:
        """Set ``model.<path>``; an existing different value wins (reported)."""
        node = self.model
        for part in path[:-1]:
            child = node.get(part)
            if child is None:
                child = node[part] = {}
            if not isinstance(child, dict):
                self.note(f"{origin}: model.{'.'.join(path)} sits under a non-literal value")
                return False
            node = child
        last = path[-1]
        if last in node and node[last] != value:
            self.note(
                f"{origin}: model.{'.'.join(path)} is already set; kept the first value "
                "(conflict, check by hand)"
            )
            return False
        node[last] = value
        return True

    def role(self, role: str, origin: str) -> dict[str, Any] | None:
        """The ``model.llm.<role>`` slot mapping (created when absent)."""
        llm = self.model.setdefault("llm", {})
        if not isinstance(llm, dict):
            self.note(f"{origin}: model.llm is not a literal mapping")
            return None
        slot = llm.setdefault(role, {})
        if not isinstance(slot, dict):
            self.note(f"{origin}: model.llm.{role} is not a literal mapping")
            return None
        return slot


def migrate_config(data: dict[str, Any], *, warmup: bool = True) -> list[str]:
    """Rewrite one config mapping in place; returns the notes (conflicts, unknown keys)."""
    existing = data.get("model")
    if "model" in data and not isinstance(existing, dict):
        if any(isinstance(data.get(k), dict) for k in ROOT_TRIGGERS):
            return ["model is not a literal mapping: migrate this config by hand"]
        return []
    m = Migration(data, warmup=warmup, model=existing if isinstance(existing, dict) else {})
    for root in OLD_ROOTS:
        if isinstance(data.get(root), Src):
            m.note(f"{root} is not a literal ({data[root].text}): migrate it by hand")
    _embedding(m)
    _llm_roles(m)
    _models(m)
    _llm_structured(m)
    _llm_timeouts(m)
    _decision(m)
    _read(m)
    _ingest(m)
    if warmup:
        _warmup(m)
    for root in OLD_ROOTS:
        if isinstance(data.get(root), dict) and not data[root]:
            del data[root]
    if m.model and "model" not in data:
        data["model"] = {k: m.model[k] for k in MODEL_ORDER if k in m.model} | {
            k: v for k, v in m.model.items() if k not in MODEL_ORDER
        }
    return m.notes


def _embedding(m: Migration) -> None:
    emb = m.data.get("embedding")
    if not isinstance(emb, dict):
        return
    for key in list(emb):
        if key in EMBEDDING_RENAME:
            if m.put(("embedding", EMBEDDING_RENAME[key]), emb[key], f"embedding.{key}"):
                del emb[key]
        else:
            m.note(f"embedding.{key}: unknown key left in place")


def _llm_roles(m: Migration) -> None:
    llm = m.data.get("llm")
    if not isinstance(llm, dict):
        return
    roles = llm.get("roles")
    if isinstance(roles, dict):
        for role, slot in list(roles.items()):
            if m.put(("llm", role), slot, f"llm.roles.{role}"):
                del roles[role]
        if not roles:
            del llm["roles"]
    elif "roles" in llm:
        llm_slot = m.model.get("llm")
        if llm_slot is None or llm_slot == {}:
            m.model["llm"] = roles  # the role map moves as a whole
            del llm["roles"]
            m.note(
                "llm.roles is not a literal: moved as a whole; move each role's "
                "settings.timeout_seconds to timeout_s by hand"
            )
            if "structured" in llm:
                m.note("llm.roles is not a literal: copy llm.structured into each role by hand")
        else:
            m.note("llm.roles is not a literal and model.llm is set: merge by hand")
    for key in llm:
        if key not in {"roles", "structured"}:
            m.note(f"llm.{key}: unknown key left in place")


def _llm_structured(m: Migration) -> None:
    llm = m.data.get("llm")
    if not isinstance(llm, dict) or "structured" not in llm:
        return
    roles = m.model.get("llm")
    if not isinstance(roles, dict) or not roles:
        m.note("llm.structured: no model.llm role in this config to copy it into; left in place")
        return
    structured = llm["structured"]
    for role, slot in roles.items():
        if isinstance(slot, dict):
            if "structured" not in slot:
                slot["structured"] = copy.deepcopy(structured)
        else:
            m.note(f"llm.structured: model.llm.{role} is not a literal; add structured by hand")
    del llm["structured"]


def _hoist_timeout(slot: dict[str, Any], m: Migration, where: str) -> None:
    """``settings.timeout_seconds`` -> the slot's typed ``timeout_s`` (required by ADR-082)."""
    settings = slot.get("settings")
    if isinstance(settings, dict) and "timeout_seconds" in settings:
        value = settings.pop("timeout_seconds")
        if "timeout_s" in slot and slot["timeout_s"] != value:
            m.note(f"{where}: timeout_s and settings.timeout_seconds differ; kept timeout_s")
            return
        rebuilt = {k: slot[k] for k in ("provider",) if k in slot}
        rebuilt["timeout_s"] = slot.get("timeout_s", value)
        rebuilt.update((k, v) for k, v in slot.items() if k not in rebuilt)
        slot.clear()
        slot.update(rebuilt)
    elif "timeout_s" not in slot and (isinstance(settings, dict) or "settings" not in slot):
        m.note(f"{where}: no settings.timeout_seconds; add the required timeout_s by hand")
    elif "timeout_s" not in slot:
        m.note(f"{where}.settings is not a literal: move timeout_seconds to timeout_s by hand")


def _llm_timeouts(m: Migration) -> None:
    roles = m.model.get("llm")
    if not isinstance(roles, dict):
        return
    for role, slot in roles.items():
        if not isinstance(slot, dict):
            continue
        _hoist_timeout(slot, m, f"model.llm.{role}")
        escalation = slot.get("escalation")
        targets = escalation.get("targets") if isinstance(escalation, dict) else None
        if isinstance(targets, list):
            for i, target in enumerate(targets):
                if isinstance(target, dict):
                    _hoist_timeout(target, m, f"model.llm.{role}.escalation.targets[{i}]")


def _decision_provider(m: Migration) -> Any:
    decision = m.data.get("decision")
    if isinstance(decision, dict) and "provider" in decision:
        return lit(decision["provider"])
    return None


def _models(m: Migration) -> None:
    models = m.data.get("models")
    if not isinstance(models, dict):
        return
    profiles = models.get("profiles")
    profiles_d = profiles if isinstance(profiles, dict) else None
    used: set[str] = set()
    unresolved = "profiles" in models and profiles_d is None

    def expand(name_leaf: Any, origin: str) -> dict[str, Any] | None:
        nonlocal unresolved
        name = lit(name_leaf)
        if not isinstance(name, str):
            m.note(f"{origin}: profile name is not a literal; left in place")
            unresolved = True
            return None
        profile = profiles_d.get(name) if profiles_d is not None else None
        if not isinstance(profile, dict):
            m.note(f"{origin}: profile {name!r} is not defined in this config; left in place")
            unresolved = True
            return None
        used.add(name)
        for key in profile:
            if key not in {"kind", "provider", "settings"}:
                m.note(f"models.profiles.{name}.{key}: unknown profile key dropped")
        return {k: copy.deepcopy(profile[k]) for k in ("provider", "settings") if k in profile}

    for key in list(models):
        if key in PLACEMENT_RENAME:
            value = models[key]
            if key == "catalogue":
                value = _catalogue_kinds(value)
            if m.put(("placement", PLACEMENT_RENAME[key]), value, f"models.{key}"):
                del models[key]
        elif key == "rerank":
            del models[key]  # removed: model.rerank.chain [] is off
        elif key not in MODELS_KNOWN:
            m.note(f"models.{key}: unknown key left in place")

    decision = _decision_provider(m)
    topic = models.get("topic")
    topic_bound = False
    bindings = models.get("bindings")
    if isinstance(bindings, dict):
        for role, name in list(bindings.items()):
            origin = f"models.bindings.{role}"
            if role == "gliner_decision" and decision != "gliner2":
                if decision is UNKNOWN or decision is None:
                    # Absent here is not "off": a helper or another layer may set it.
                    why = "is not a literal" if decision is UNKNOWN else "is not in this config"
                    m.note(
                        f"{origin}: decision.provider {why}; left in place "
                        "(gliner2 = model.decider.route, off = drop the binding)"
                    )
                    unresolved = True
                    continue
                m.note(f"{origin}: decision.provider is off, so the binding was inert; dropped")
                if isinstance(lit(name), str):
                    used.add(lit(name))  # referenced here: its profile may go too
                del bindings[role]
                continue
            slot = expand(name, origin)
            if slot is None:
                if role == "gliner_topic":
                    topic_bound = True  # keep models.topic beside the unresolved binding
                continue
            if role == "gliner_topic":
                topic_bound = True
                if isinstance(topic, dict):
                    for tkey in ("labels", "min_confidence"):
                        if tkey in topic:
                            slot[tkey] = topic[tkey]
                    for tkey in topic:
                        if tkey not in {"labels", "min_confidence"}:
                            m.note(f"models.topic.{tkey}: unknown key dropped")
                    models.pop("topic", None)
                elif topic is not None:
                    m.note("models.topic is not a literal: merge it into model.extraction.topic")
            if role in GLINER_TARGET:
                ok = m.put(GLINER_TARGET[role], slot, origin)
            else:
                ok = m.put(("llm", role), slot, origin)
            if ok:
                del bindings[role]
            else:
                unresolved = True
        if not bindings:
            del models["bindings"]
    elif bindings is not None:
        m.note("models.bindings is not a literal; left in place")
        unresolved = True
    if "topic" in models and not topic_bound:
        m.note("models.topic: no gliner_topic binding in this config (inert); dropped")
        del models["topic"]

    escalation = models.get("escalation")
    if isinstance(escalation, dict):
        for role, policy in list(escalation.items()):
            origin = f"models.escalation.{role}"
            if not isinstance(policy, dict):
                m.note(f"{origin} is not a literal; left in place")
                unresolved = True
                continue
            new_policy = dict(policy)
            targets = policy.get("targets")
            if isinstance(targets, list):
                expanded = [expand(t, f"{origin}.targets") for t in targets]
                if any(t is None for t in expanded):
                    continue
                new_policy["targets"] = expanded
            elif targets is not None:
                m.note(f"{origin}.targets is not a literal; left in place")
                unresolved = True
                continue
            slot = m.role(role, origin)
            if slot is None or "escalation" in slot:
                m.note(f"{origin}: model.llm.{role}.escalation already set; left in place")
                continue
            slot["escalation"] = new_policy
            del escalation[role]
        if not escalation:
            del models["escalation"]
    elif escalation is not None:
        m.note("models.escalation is not a literal; left in place")

    thinking = models.get("thinking")
    if isinstance(thinking, dict):
        for role, budget in list(thinking.items()):
            slot = m.role(role, f"models.thinking.{role}")
            if slot is not None and "thinking" not in slot:
                slot["thinking"] = budget
                del thinking[role]
        if not thinking:
            del models["thinking"]
    elif thinking is not None:
        m.note("models.thinking is not a literal; left in place")

    if "embedder_profile" in models:
        value = lit(models["embedder_profile"])
        if value == "auto":
            del models["embedder_profile"]
        elif value is UNKNOWN:
            m.note("models.embedder_profile is not a literal; left in place")
        else:
            current = m.model.get("embedding", {})
            if isinstance(current, dict) and "preset" in current:
                m.note(
                    "models.embedder_profile: embedding.profile already sets "
                    "model.embedding.preset; dropped (conflict, check by hand)"
                )
                del models["embedder_profile"]
            elif m.put(("embedding", "preset"), models["embedder_profile"], "embedder_profile"):
                del models["embedder_profile"]

    if "profiles" in models and not used and not unresolved:
        m.note("models.profiles: no binding in this config references them; left in place")
    elif "profiles" in models and not unresolved:
        unused = sorted(set(profiles_d or {}) - used)
        if unused:
            m.note(f"models.profiles: unreferenced profile(s) {', '.join(unused)} dropped")
        del models["profiles"]
    elif "profiles" in models:
        m.note("models.profiles kept: a binding or escalation target could not be expanded")


def _catalogue_kinds(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    out = value.__class__(value)
    for i, decl in enumerate(out):
        if isinstance(decl, dict) and "kind" in decl:
            kind = lit(decl["kind"])
            if kind in CATALOGUE_KIND:
                out[i] = {**decl, "kind": CATALOGUE_KIND[kind]}
    return out


def _decision(m: Migration) -> None:
    decision = m.data.get("decision")
    if not isinstance(decision, dict):
        return
    provider = _decision_provider(m)
    if "provider" in decision:
        if provider == "gliner2":
            decider = m.model.get("decider")
            route = decider.get("route") if isinstance(decider, dict) else None
            if route is None:
                m.put(("decider", "route"), {"provider": "gliner2"}, "decision.provider")
                m.note(
                    "decision.provider gliner2 without a gliner_decision binding here: "
                    "model.decider.route needs settings (model) by hand"
                )
            del decision["provider"]
        elif provider == "off":
            del decision["provider"]
        else:
            m.note(f"decision.provider {provider!r}: not a known literal; left in place")
    for key in decision:
        if key != "provider":
            m.note(f"decision.{key}: unknown key left in place")


def _read(m: Migration) -> None:
    read = m.data.get("read")
    if not isinstance(read, dict) or not set(read) & READ_MOVED:
        return
    _read_decider(m, read)
    _read_rerank(m, read)
    for key, new in READ_RERANK_RENAME.items():
        if key in read and m.put(("rerank", new), read[key], f"read.{key}"):
            del read[key]
    if not read:
        del m.data["read"]


def _read_decider(m: Migration, read: dict[str, Any]) -> None:
    keys = ["decider", *DECIDER_SETTINGS, *DECIDER_TYPED]
    present = [k for k in keys if k in read]
    if not present:
        return
    if "decider" not in read:
        m.note(
            f"read.{', read.'.join(present)} without read.decider: the decider is chosen "
            "elsewhere; left in place"
        )
        return
    mode = lit(read["decider"])
    if mode == "heuristic":
        for key in present:
            del read[key]
        return
    if mode != "opendecider":
        m.note(f"read.decider {mode!r}: not a known literal; left in place")
        return
    settings: dict[str, Any] = {}
    for key, new in DECIDER_SETTINGS.items():
        if key in read:
            settings[new] = read[key]
    settings.setdefault("model", DECIDER_DEFAULT_MODEL)
    slot: dict[str, Any] = {"provider": read["decider"]}
    for key, new in DECIDER_TYPED.items():
        if key in read:
            slot[new] = read[key]
    order = ("model", "device", "threads", "backend", "dtype")
    slot["settings"] = {k: settings[k] for k in order if k in settings}
    if m.put(("decider", "read"), slot, "read.decider"):
        for key in present:
            del read[key]


def _read_rerank(m: Migration, read: dict[str, Any]) -> None:
    present = [k for k in READ_RERANK_SELECTOR if k in read]
    if not present:
        return
    v2 = lit(read["rerank_v2"]) if "rerank_v2" in read else None
    if v2 is UNKNOWN:
        m.note("read.rerank_v2 is not a literal; rerank selection left in place")
        return
    if v2 is True:
        names = lit(read["rerank_v2_chain"]) if "rerank_v2_chain" in read else V2_DEFAULT_CHAIN
        if names is UNKNOWN or not all(isinstance(n, str) for n in names):
            m.note("read.rerank_v2_chain is not a literal list; rerank selection left in place")
            return
        if "rerank" in read and lit(read["rerank"]) != "off":
            m.note(
                f"read.rerank {lit(read['rerank'])!r} and read.rerank_v2 both set: rerank_v2 "
                "wins (as at run time); read.rerank / rerank_settings dropped"
            )
        chain: list[Any] = [{"provider": n, "settings": {}} for n in names]
    else:
        if "rerank_v2_chain" in read and v2 is None:
            m.note(
                "read.rerank_v2_chain without read.rerank_v2 here: the v2 flag is set "
                "elsewhere; rerank selection left in place"
            )
            return
        if "rerank" not in read:
            if "rerank_settings" in read:
                m.note(
                    "read.rerank_settings without read.rerank: the provider is chosen "
                    "elsewhere; left in place"
                )
            else:
                read.pop("rerank_v2_chain", None)  # inert under rerank_v2: false
            read.pop("rerank_v2", None)
            return  # rerank_v2: false alone selects nothing
        provider = lit(read["rerank"])
        if provider == "off":
            chain = []
        else:
            if provider is UNKNOWN:
                m.note("read.rerank is not a literal: check it is never 'off' (chain [])")
            chain = [{"provider": read["rerank"], "settings": read.get("rerank_settings", {})}]
    if m.put(("rerank", "chain"), chain, "read.rerank"):
        for key in present:
            del read[key]


def _ingest(m: Migration) -> None:
    ingest = m.data.get("ingest")
    if not isinstance(ingest, dict):
        return
    present = [k for k in VISION_KEYS if k in ingest]
    if not present:
        return
    if "asset_vision" not in ingest:
        m.note(f"ingest.{', ingest.'.join(present)} without ingest.asset_vision: left in place")
        return
    mode = lit(ingest["asset_vision"])
    if mode == "none":
        for key in present:
            del ingest[key]
    elif mode == "ollama":
        slot: dict[str, Any] = {"provider": ingest["asset_vision"]}
        if "asset_vision_timeout_s" in ingest:
            slot["timeout_s"] = ingest["asset_vision_timeout_s"]
        slot["settings"] = {
            "model": ingest.get("asset_vision_model", VISION_DEFAULT_MODEL),
            "api_base": ingest.get("asset_vision_url", VISION_DEFAULT_URL),
        }
        if m.put(("vision",), slot, "ingest.asset_vision"):
            for key in present:
                del ingest[key]
    else:
        m.note(f"ingest.asset_vision {mode!r}: not a known literal; left in place")
        return
    if not ingest:
        del m.data["ingest"]


def _warmup(m: Migration) -> None:
    runtime = m.data.get("runtime")
    shared = runtime.get("shared_resources") if isinstance(runtime, dict) else None
    if not isinstance(shared, dict):
        return
    for key in ("warmup_include", "warmup_exclude"):
        values = shared.get(key)
        if isinstance(values, list) and any(v in WARMUP_RENAME for v in values):
            shared[key] = values.__class__(WARMUP_RENAME.get(v, v) for v in values)


# ── dotted keys and environment variable names ─────────────────────────────────────────


def migrate_path(segs: list[str]) -> tuple[list[str] | None, int, str | None]:
    """Map an old key path to its new prefix: (new prefix, segments consumed, note)."""
    s = [p.lower() for p in segs]
    n = len(s)
    head = s[0] if s else ""
    second = s[1] if n > 1 else ""
    if n < 2:
        return None, 0, None
    if head == "embedding" and second in EMBEDDING_RENAME:
        return ["model", "embedding", EMBEDDING_RENAME[second]], 2, None
    if head == "llm":
        if second == "roles":
            return ["model", "llm"], 2, None
        if second == "structured":
            return None, 0, "llm.structured moved into every model.llm.<role>.structured"
        return None, 0, None
    if head == "decision" and second == "provider":
        return ["model", "decider", "route", "provider"], 2, "off = model.decider.route absent"
    if head == "models":
        if second == "embedder_profile":
            return ["model", "embedding", "preset"], 2, None
        if second in PLACEMENT_RENAME:
            return ["model", "placement", PLACEMENT_RENAME[second]], 2, None
        if second == "topic":
            return ["model", "extraction", "topic"], 2, None
        if second in {"thinking", "escalation"} and n > 2:
            return ["model", "llm", segs[2], second], 3, None
        if second == "bindings" and n > 2:
            role = s[2]
            target = GLINER_TARGET.get(role)
            if target is not None:
                return ["model", *target], 3, "a binding (profile name) is now an inline slot"
            return ["model", "llm", segs[2]], 3, "a binding (profile name) is now an inline slot"
        if second in {"rerank", "profiles", "bindings", "thinking", "escalation"}:
            return None, 0, f"models.{second} was removed (ADR-082)"
        return None, 0, None
    if head == "read":
        if second == "decider":
            return ["model", "decider", "read", "provider"], 2, "heuristic = slot absent"
        if second in DECIDER_SETTINGS:
            return ["model", "decider", "read", "settings", DECIDER_SETTINGS[second]], 2, None
        if second in DECIDER_TYPED:
            return ["model", "decider", "read", DECIDER_TYPED[second]], 2, None
        if second in READ_RERANK_SELECTOR:
            return (
                ["model", "rerank", "chain"],
                2,
                "the value is now a [{provider, settings}] chain",
            )
        if second in READ_RERANK_RENAME:
            return ["model", "rerank", READ_RERANK_RENAME[second]], 2, None
        return None, 0, None
    if head == "ingest":
        vision = {
            "asset_vision": ["model", "vision", "provider"],
            "asset_vision_model": ["model", "vision", "settings", "model"],
            "asset_vision_url": ["model", "vision", "settings", "api_base"],
            "asset_vision_timeout_s": ["model", "vision", "timeout_s"],
        }
        if second in vision:
            return (
                vision[second],
                2,
                "none = model.vision null" if second == "asset_vision" else None,
            )
    return None, 0, None


#: Old keys that hold a mapping or list, so a longer path below them is still a config key.
#: Below any other key (a scalar) a longer path is a tap or log event name, never moved.
TAIL_OK = {
    ("embedding", "settings"),
    ("llm", "roles"),
    ("models", "escalation"),
    ("models", "topic"),
    ("models", "catalogue"),
    ("models", "gpus"),
    ("models", "role_tier"),
    ("models", "pins"),
}


def _migrate_segments(segs: list[str]) -> tuple[list[str] | None, str | None]:
    new, used, note = migrate_path(segs)
    if new is None:
        return None, note
    tail = segs[used:]
    if tail and tuple(p.lower() for p in segs[:2]) not in TAIL_OK:
        return None, None
    return [*new, *tail], note


def migrate_dotted(key: str) -> tuple[str | None, str | None]:
    """``read.rerank_blend`` -> ``model.rerank.blend`` (None when the key does not move)."""
    new, note = _migrate_segments(key.split("."))
    return (".".join(new) if new is not None else None), note


ENV_RE = re.compile(r"\bMEMSPINE_([A-Za-z0-9]+(?:_[A-Za-z0-9]+)*(?:__[A-Za-z0-9_]+)+)")


def migrate_env(name: str) -> tuple[str | None, str | None]:
    """``MEMSPINE_READ__RERANK_BLEND`` -> ``MEMSPINE_MODEL__RERANK__BLEND``."""
    if not name.startswith("MEMSPINE_"):
        return None, None
    segs = name[len("MEMSPINE_") :].split("__")
    new_prefix, used, _ = migrate_path(segs)
    new, note = _migrate_segments(segs)
    if new is None or new_prefix is None:
        return None, note
    return "MEMSPINE_" + "__".join([*(p.upper() for p in new_prefix), *segs[used:]]), note


def _env_sub(text: str, notes: list[str], where: str) -> str:
    def repl(match: re.Match[str]) -> str:
        new, note = migrate_env(match.group(0))
        if new is None:
            if note:
                notes.append(f"{where}: {match.group(0)}: {note}")
            return match.group(0)
        return new

    return ENV_RE.sub(repl, text)


# ── Python sources ─────────────────────────────────────────────────────────────────────

ATTRIBUTE_SUBS: tuple[tuple[str, str], ...] = (
    (
        # ``(?<!model)``, not ``(?<!\.model)``: a dotted string the constant pass already
        # rewrote ("model.embedding.provider") must not gain a second ``model.``.
        r"(?<!model)\.embedding\.(provider|settings|batch_size|versioned_spaces)\b",
        r".model.embedding.\1",
    ),
    (r"(?<!model)\.embedding\.(pin_|migrate_)?profile\b", r".model.embedding.\1preset"),
    (r"\.llm\.roles\b", ".model.llm"),
    (r"\.models\.(" + "|".join(PLACEMENT_KEYS) + r")\b", r".model.placement.\1"),
    (r"\.models\.default_rerankers\b", ".model.placement.catalogue_rerankers"),
    (r"\.models\.gliner_max_models\b", ".model.placement.max_loaded_models"),
    (r"\.read\.rerank_(" + "|".join(RERANK_SUFFIXES) + r")\b", r".model.rerank.\1"),
    (r"\.read\.rerank_v2_pool\b", ".model.rerank.pool"),
    (r"\.read\.rerank_v2_search_pool\b", ".model.rerank.search_pool"),
    (r"\.read\.skip_rerank_for_ordering\b", ".model.rerank.skip_for_ordering"),
    (r"\.read\.decider_(tasks|min_confidence|workers)\b", r".model.decider.read.\1"),
    (r"\.ingest\.asset_vision_timeout_s\b", ".model.vision.timeout_s"),
)
#: Attribute paths whose value changes shape: listed for hand review.
ATTRIBUTE_MANUAL = re.compile(
    r"\.llm\.structured\b|\.models\.(bindings|profiles|thinking|escalation|topic|rerank"
    r"|embedder_profile)\b|\.decision\.provider\b|\.read\.rerank(_settings|_v2|_v2_chain)?\b"
    r"|\.read\.decider(_model|_device|_threads|_backend|_dtype)?\b"
    r"|\.ingest\.asset_vision(_model|_url)?\b"
)
DOTTED_RE = re.compile(r"(embedding|llm|models|decision|read|ingest)(\.[A-Za-z0-9_*]+)+")
#: Calls whose first argument is an event name (tap spans / events, structlog methods).
EVENT_CALLS = {
    "event",
    "span",
    "count",
    "timer",
    "debug",
    "info",
    "warning",
    "error",
    "exception",
    "critical",
}
YAML_STRING_RE = re.compile(
    r"^\s*(llm|models|embedding|decision):\s*$|^\s*(rerank\w*|decider\w*|asset_vision\w*):",
    re.MULTILINE,
)


def _const_key(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_root(tree: dict[str, Any]) -> bool:
    """A config mapping (not a report dict that merely shares a key name)."""

    def sub(key: str) -> dict[str, Any] | None:
        value = tree.get(key)
        return value if isinstance(value, dict) and value else None

    emb = sub("embedding")
    llm = sub("llm")
    models = sub("models")
    decision = sub("decision")
    read = sub("read")
    ingest = sub("ingest")
    return bool(
        (emb is not None and set(emb) <= set(EMBEDDING_RENAME))
        or (llm is not None and set(llm) <= {"roles", "structured"})
        or (models is not None and set(models) & MODELS_KNOWN)
        or (decision is not None and set(decision) <= {"provider"})
        or (read is not None and set(read) & READ_MOVED)
        or (ingest is not None and set(ingest) & set(VISION_KEYS))
    )


class Source:
    def __init__(self, text: str) -> None:
        self.text = text
        self._lines = text.splitlines(keepends=True)
        self._offsets = [0]
        for line in self._lines:
            self._offsets.append(self._offsets[-1] + len(line))

    def offset(self, line: int, col: int) -> int:
        # ast columns are UTF-8 byte offsets
        raw = self._lines[line - 1].encode("utf-8")[:col].decode("utf-8", errors="ignore")
        return self._offsets[line - 1] + len(raw)

    def span(self, node: ast.AST) -> tuple[int, int]:
        lineno: int = getattr(node, "lineno")  # noqa: B009 - typed access on ast.AST
        col: int = getattr(node, "col_offset")  # noqa: B009
        end_line = getattr(node, "end_lineno", None)
        end_col = getattr(node, "end_col_offset", None)
        assert end_line is not None and end_col is not None
        return self.offset(lineno, col), self.offset(end_line, end_col)

    def seg(self, node: ast.AST) -> str:
        start, end = self.span(node)
        return self.text[start:end]


@dataclass
class Edit:
    start: int
    end: int
    text: str


@dataclass
class Entry:
    key: str | None  # None = positional argument or ``**spread``
    start: int
    end: int
    value: ast.expr | None


class PyRewriter(ast.NodeVisitor):
    def __init__(self, src: Source, path: Path) -> None:
        self.src = src
        self.path = path
        self.edits: list[Edit] = []
        self.notes: list[str] = []
        self.quote = (
            "'"
            if re.search(r"\{\s*'[a-z_]+'\s*:", src.text)
            and not re.search(r'\{\s*"[a-z_]+"\s*:', src.text)
            else '"'
        )
        self._keep: list[Any] = []  # keeps tree objects alive so their ids stay unique
        self._event_names: set[int] = set()
        #: Values of a ``model`` key / ``model=`` kwarg: already the new block, never an old
        #: root (its ``embedding`` / ``llm`` keys are slots), so a second run is a no-op.
        self._model_blocks: set[int] = set()

    # ── helpers ──
    def _note(self, node: ast.AST, text: str) -> None:
        self.notes.append(f"{self.path}:{getattr(node, 'lineno', 0)}: {text}")

    def _covered(self, node: ast.AST) -> bool:
        start, end = self.src.span(node)
        return any(e.start <= start and end <= e.end and e.start < e.end for e in self.edits)

    def to_tree(self, node: ast.expr) -> Any:
        if isinstance(node, ast.Dict) and all(_const_key(k) is not None for k in node.keys):
            tree: dict[str, Any] = {}
            for k, v in zip(node.keys, node.values, strict=True):
                key = _const_key(k)
                assert key is not None
                tree[key] = self.to_tree(v)
            self._keep.append(tree)
            return tree
        if isinstance(node, (ast.List, ast.Tuple)) and not any(
            isinstance(e, ast.Starred) for e in node.elts
        ):
            items: list[Any] = Tup() if isinstance(node, ast.Tuple) else []
            items.extend(self.to_tree(e) for e in node.elts)
            return items
        return Src(self.src.seg(node))

    def _dict_entries(self, node: ast.Dict) -> list[Entry]:
        entries = []
        for k, v in zip(node.keys, node.values, strict=True):
            vstart, vend = self.src.span(v)
            if k is None:
                entries.append(Entry(None, self.src.text.rfind("**", 0, vstart), vend, v))
            else:
                entries.append(Entry(_const_key(k), self.src.span(k)[0], vend, v))
        return entries

    def _call_entries(self, node: ast.Call) -> list[Entry]:
        entries = [Entry(None, *self.src.span(a), None) for a in node.args]
        for kw in node.keywords:
            start, end = self.src.span(kw)
            entries.append(Entry(kw.arg, start, end, kw.value))
        return sorted(entries, key=lambda e: e.start)

    # ── structural diff → in-place edits ──
    def _diff_value(self, old: Any, new: Any, node: ast.expr) -> None:
        if old == new:
            return
        if isinstance(old, dict) and isinstance(new, dict) and isinstance(node, ast.Dict):
            self._diff_entries(self._dict_entries(node), old, new, keyword=False, container=node)
        elif (
            isinstance(old, list)
            and isinstance(new, list)
            and isinstance(node, (ast.List, ast.Tuple))
            and len(old) == len(new) == len(node.elts)
        ):
            for o, n, elt in zip(old, new, node.elts, strict=True):
                self._diff_value(o, n, elt)
        else:
            self.edits.append(Edit(*self.src.span(node), render(new, self.quote)))

    def _diff_entries(
        self,
        entries: list[Entry],
        old: dict[str, Any],
        new: dict[str, Any],
        *,
        keyword: bool,
        container: ast.AST,
    ) -> None:
        def fmt(key: str, value: Any) -> str:
            text = render(value, self.quote)
            return f"{key}={text}" if keyword else f"{_q(key, self.quote)}: {text}"

        keyed = {e.key: e for e in entries if e.key is not None}
        for key, entry in keyed.items():
            if key in old and key in new and old[key] != new[key] and entry.value is not None:
                self._diff_value(old[key], new[key], entry.value)
        added = [k for k in new if k not in old]
        added_text = ", ".join(fmt(k, new[k]) for k in added)
        deleted = [e for e in entries if e.key is not None and e.key in old and e.key not in new]
        if added and deleted:
            first = deleted.pop(0)
            self.edits.append(Edit(first.start, first.end, added_text))
        elif added:
            if entries:
                self.edits.append(Edit(entries[-1].end, entries[-1].end, ", " + added_text))
            else:
                self.edits.append(Edit(*self.src.span(container), "{" + added_text + "}"))
        if not deleted:
            return
        gone = {id(e) for e in deleted}
        index = {id(e): i for i, e in enumerate(entries)}
        last_kept = max((i for i, e in enumerate(entries) if id(e) not in gone), default=None)
        trailing: list[Entry] = []
        for entry in deleted:
            i = index[id(entry)]
            if last_kept is not None and i < last_kept:
                self.edits.append(Edit(entry.start, entries[i + 1].start, ""))
            else:
                trailing.append(entry)
        if trailing:
            end = trailing[-1].end
            if last_kept is not None:
                self.edits.append(Edit(entries[last_kept].end, end, ""))
            else:
                tail = re.match(r"\s*,", self.src.text[end:])
                self.edits.append(Edit(trailing[0].start, end + (tail.end() if tail else 0), ""))

    # ── roots ──
    def _migrate_root(self, node: ast.AST, entries: list[Entry], *, keyword: bool) -> bool:
        tree: dict[str, Any] = {}
        for entry in entries:
            if entry.key is not None and entry.value is not None:
                tree[entry.key] = self.to_tree(entry.value)
        if not _is_root(tree):
            for key in OLD_ROOTS:
                if keyword and isinstance(tree.get(key), Src) and isinstance(node, ast.Call):
                    if _call_name(node) in {"Engine", "MemspineConfig", "load_config"}:
                        self._note(node, f"{key}= is not a literal: migrate it by hand")
            return False
        new = copy.deepcopy(tree)
        notes = migrate_config(new, warmup=False)
        for text in notes:
            self._note(node, text)
        if new == tree:
            return False
        self._diff_entries(entries, tree, new, keyword=keyword, container=node)
        return True

    def _mark_model_blocks(self, entries: list[Entry]) -> None:
        for entry in entries:
            if entry.key == "model" and entry.value is not None:
                self._model_blocks.add(id(entry.value))

    def visit_Dict(self, node: ast.Dict) -> None:
        if self._covered(node) or id(node) in self._model_blocks:
            return
        self._mark_model_blocks(self._dict_entries(node))
        if any(_const_key(k) in ROOT_TRIGGERS for k in node.keys if k is not None):
            self._migrate_root(node, self._dict_entries(node), keyword=False)
        self._warmup_entries(self._dict_entries(node))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if self._covered(node):
            return
        self._mark_model_blocks(self._call_entries(node))
        name = _call_name(node)
        if name in REMOVED_CLASSES:
            self._note(node, f"{name}(...) is removed by ADR-082: rewrite as a model slot")
        elif name in {"ReadConfig", "IngestConfig"} and any(
            kw.arg in READ_MOVED or kw.arg in VISION_KEYS for kw in node.keywords
        ):
            self._note(node, f"{name}(...) takes a key moved under model: rewrite by hand")
        if name in EVENT_CALLS and node.args and isinstance(node.args[0], ast.Constant):
            self._event_names.add(id(node.args[0]))  # a tap / log event name, not a key
        if name == "ModelDecl":
            for kw in node.keywords:
                kind = _const_key(kw.value) if kw.arg == "kind" else None
                if kind in CATALOGUE_KIND:
                    self._replace_const(kw.value, CATALOGUE_KIND[kind])
        if any(kw.arg in ROOT_TRIGGERS for kw in node.keywords):
            self._migrate_root(node, self._call_entries(node), keyword=True)
        self._warmup_entries(self._call_entries(node))
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "setdefault"
            and len(node.args) == 2
            and _const_key(node.args[0]) in OLD_ROOTS
            and not (isinstance(node.args[1], ast.Dict) and not node.args[1].keys)
        ):
            self._note(node, f"setdefault({_const_key(node.args[0])!r}, ...): migrate by hand")
        self.generic_visit(node)

    def _warmup_entries(self, entries: list[Entry]) -> None:
        for entry in entries:
            if entry.key in {"warmup_include", "warmup_exclude"} and isinstance(
                entry.value, (ast.List, ast.Tuple)
            ):
                for elt in entry.value.elts:
                    value = _const_key(elt)
                    if value in WARMUP_RENAME:
                        self._replace_const(elt, WARMUP_RENAME[value])

    def _replace_const(self, node: ast.expr, value: str) -> None:
        seg = self.src.seg(node)
        quote = seg[0] if seg[:1] in {"'", '"'} else '"'
        self.edits.append(Edit(*self.src.span(node), _q(value, quote)))

    # ── item assignment / setdefault chains ──
    def _model_target(self, target: ast.expr, value: ast.expr | None) -> None:
        """``model = {...}`` / ``cfg["model"] = {...}``: the value is a new model block."""
        chain = _subscript_chain(target)
        named = isinstance(target, ast.Name) and target.id == "model"
        if value is not None and (named or (chain is not None and chain[1][-1] == "model")):
            self._model_blocks.add(id(value))

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._model_target(node.target, node.value)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._model_target(target, node.value)
        if len(node.targets) == 1 and not self._covered(node):
            chain = _subscript_chain(node.targets[0])
            if chain is not None and self._assign(node, *chain):
                return
        self.generic_visit(node)

    def _assign(self, node: ast.Assign, base: ast.expr, path: list[str]) -> bool:
        root = next((i for i, k in enumerate(path) if k in ROOT_TRIGGERS), None)
        if root is None or "model" in path[:root]:
            return False  # below ``model`` the keys are already slots
        if root > 0:
            # the base is the expression up to the root key; re-derive it from the target
            sub = _chain_node(node.targets[0], len(path) - root)
            base = sub
            path = path[root:]
        if (
            path[0] == "models"
            and len(path) > 1
            and path[1]
            in {
                "profiles",
                "bindings",
                "escalation",
            }
        ):
            self._note(node, f"assignment to {'.'.join(path)}: expand the binding by hand")
            return True
        value = self.to_tree(node.value)
        tree: dict[str, Any] = value
        for key in reversed(path):
            tree = {key: tree}
        if len(path) == 1 and not _is_root(tree):
            if path[0] in OLD_ROOTS and isinstance(value, Src):
                self._note(node, f"[{path[0]!r}] assigned a non-literal: migrate it by hand")
            return False
        new = copy.deepcopy(tree)
        for text in migrate_config(new, warmup=False):
            self._note(node, text)
        if new == tree:
            return False
        base_src = self.src.seg(base)
        target_src = self.src.seg(node.targets[0])
        indent = " " * node.col_offset
        stmts: list[str] = []
        for key, sub_value in new.items():
            depth = 3 if key == "model" else len(path)
            for leaf_path, leaf in _flatten([key], sub_value, depth):
                if leaf_path == path:
                    stmts.append(f"{target_src} = {render(leaf, self.quote)}")
                    continue
                q = self.quote
                access = "".join(f".setdefault({_q(p, q)}, {{}})" for p in leaf_path[:-1])
                stmts.append(f"{base_src}{access}[{_q(leaf_path[-1], q)}] = {render(leaf, q)}")
        self.edits.append(Edit(*self.src.span(node), f"\n{indent}".join(stmts)))
        return True

    # ── dotted strings and YAML inside strings ──
    def visit_Constant(self, node: ast.Constant) -> None:
        if not isinstance(node.value, str) or id(node) in self._event_names or self._covered(node):
            return
        value = node.value
        if DOTTED_RE.fullmatch(value):
            new, note = migrate_dotted(value)
            seg = self.src.seg(node)
            simple = len(seg) >= 2 and seg[0] in "'\"" and seg[-1] == seg[0] and seg[1:-1] == value
            if new is not None and simple:
                self.edits.append(Edit(*self.src.span(node), seg[0] + new + seg[0]))
                if note:
                    self._note(node, f"{value!r} -> {new!r}: {note}")
            elif new is not None or note:
                self._note(node, f"dotted key {value!r}: {note or 'rewrite by hand'}")
        elif "\n" in value and YAML_STRING_RE.search(value):
            self._note(node, "YAML in a string may hold old model keys: review by hand")

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        return  # f-string parts are never config keys


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _chain_step(node: ast.expr) -> tuple[str, ast.expr] | None:
    if isinstance(node, ast.Subscript):
        key = _const_key(node.slice)
        if key is not None:
            return key, node.value
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "setdefault"
        and len(node.args) == 2
        and isinstance(node.args[1], ast.Dict)
        and not node.args[1].keys
    ):
        key = _const_key(node.args[0])
        if key is not None:
            return key, node.func.value
    return None


def _subscript_chain(target: ast.expr) -> tuple[ast.expr, list[str]] | None:
    """``x["read"]["rerank"]`` -> (``x``, ["read", "rerank"])."""
    path: list[str] = []
    cur = target
    while (step := _chain_step(cur)) is not None:
        path.append(step[0])
        cur = step[1]
    if not path or not isinstance(target, ast.Subscript):
        return None
    return cur, list(reversed(path))


def _chain_node(target: ast.expr, levels: int) -> ast.expr:
    """The container expression ``levels`` subscripts above the assignment target."""
    cur = target
    for _ in range(levels):
        step = _chain_step(cur)
        assert step is not None
        cur = step[1]
    return cur


def _flatten(prefix: list[str], value: Any, depth: int) -> Iterable[tuple[list[str], Any]]:
    if len(prefix) >= depth or not isinstance(value, dict) or not value:
        yield prefix, value
        return
    for key, sub in value.items():
        yield from _flatten([*prefix, key], sub, depth)


def rewrite_python(path: Path, text: str | None = None) -> tuple[str, list[str]]:
    text = path.read_text(encoding="utf-8") if text is None else text
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text, [f"{path}: not parseable"]
    src = Source(text)
    rewriter = PyRewriter(src, path)
    rewriter.visit(tree)
    edits = sorted(rewriter.edits, key=lambda e: (e.start, e.end), reverse=True)
    out = text
    last_start = len(text) + 1
    for edit in edits:
        if edit.end > last_start:
            rewriter.notes.append(f"{path}: overlapping edit skipped at offset {edit.start}")
            continue
        out = out[: edit.start] + edit.text + out[edit.end :]
        last_start = edit.start
    for pattern, repl in ATTRIBUTE_SUBS:
        out = re.sub(pattern, repl, out)
    for match in ATTRIBUTE_MANUAL.finditer(out):
        line = out.count("\n", 0, match.start()) + 1
        rewriter.notes.append(f"{path}:{line}: attribute {match.group(0)} changes shape: by hand")
    out = _env_sub(out, rewriter.notes, str(path))
    return out, rewriter.notes


# ── YAML / JSON / .env ─────────────────────────────────────────────────────────────────


def rewrite_json(path: Path, text: str | None = None) -> tuple[str | None, list[str]]:
    """A JSON config (eval arms): migrated in place, keeping compact or indented layout."""
    text = path.read_text(encoding="utf-8") if text is None else text
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None, []
    if not isinstance(data, dict):
        return None, []
    before = copy.deepcopy(data)
    notes = [f"{path}: {n}" for n in migrate_config(data)]
    if data == before:
        return None, notes
    match = re.search(r"\n( +)", text)
    if match:
        out = json.dumps(data, indent=len(match.group(1)), ensure_ascii=False)
    else:
        out = json.dumps(data, ensure_ascii=False)
    return out + ("\n" if text.endswith("\n") else ""), notes


def rewrite_yaml(path: Path, text: str | None = None) -> tuple[str | None, list[str]]:
    import yaml

    text = path.read_text(encoding="utf-8") if text is None else text
    notes: list[str] = []
    env_text = _env_sub(text, notes, str(path))
    try:
        data = yaml.safe_load(env_text)
    except yaml.YAMLError:
        return (env_text if env_text != text else None), [f"{path}: YAML not parseable", *notes]
    if not isinstance(data, dict):
        return (env_text if env_text != text else None), notes
    before = copy.deepcopy(data)
    notes.extend(f"{path}: {n}" for n in migrate_config(data))
    if data == before:
        return (env_text if env_text != text else None), notes
    out = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    if re.search(r"(^|\s)#", text):
        notes.append(f"{path}: YAML rewritten (comments dropped; review)")
    return out, notes


def rewrite_env(path: Path, text: str | None = None) -> tuple[str | None, list[str]]:
    text = path.read_text(encoding="utf-8") if text is None else text
    notes: list[str] = []
    out = _env_sub(text, notes, str(path))
    return (out if out != text else None), notes


def _kind(path: Path) -> str | None:
    if path.suffix == ".py":
        return "py"
    if path.suffix == ".json":
        return "json"
    if path.suffix in {".yaml", ".yml"}:
        return "yaml"
    if path.name == ".env" or path.name.startswith(".env.") or path.suffix == ".env":
        return "env"
    return None


def iter_files(paths: Iterable[str]) -> Iterable[Path]:
    skip = {".venv", "node_modules", ".git", "__pycache__", "archive"}
    for raw in paths:
        root = Path(raw)
        if root.is_file():
            yield root
            continue
        for path in sorted(root.rglob("*")):
            if any(part in skip for part in path.parts):
                continue
            if _kind(path) is not None and path.is_file():
                yield path


def rewrite_file(path: Path) -> tuple[str | None, list[str]]:
    """The new text of one file (None = unchanged) and its notes."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if SKIP_MARKER in text:
        return None, []
    kind = _kind(path)
    if kind == "py":
        new, notes = rewrite_python(path, text)
        return (new if new != text else None), notes
    if kind == "json":
        return rewrite_json(path, text)
    if kind == "yaml":
        return rewrite_yaml(path, text)
    if kind == "env":
        return rewrite_env(path, text)
    return None, []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("paths", nargs="*")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="write nothing; exit 1 on changes")
    mode.add_argument("--dry-run", action="store_true", help="write nothing; print a diff")
    parser.add_argument(
        "--key",
        action="append",
        default=[],
        help="print the new name of a dotted key or MEMSPINE_ variable (repeatable)",
    )
    args = parser.parse_args(argv)
    for key in args.key:
        new, note = migrate_env(key) if key.startswith("MEMSPINE_") else migrate_dotted(key)
        print(f"{key} -> {new or '(unchanged)'}" + (f"  ({note})" if note else ""))
    if not args.paths:
        if not args.key:
            parser.error("give at least one PATH or --key")
        return 0
    changed: list[Path] = []
    manual: list[str] = []
    for path in iter_files(args.paths):
        if path.resolve() == Path(__file__).resolve():
            continue
        new, notes = rewrite_file(path)
        manual.extend(notes)
        if new is None:
            continue
        changed.append(path)
        if args.dry_run:
            old = path.read_text(encoding="utf-8", errors="replace")
            sys.stdout.writelines(
                difflib.unified_diff(
                    old.splitlines(keepends=True),
                    new.splitlines(keepends=True),
                    fromfile=f"a/{path}",
                    tofile=f"b/{path}",
                )
            )
        elif not args.check:
            path.write_text(new, encoding="utf-8", newline="\n")
    for path in changed:
        print(f"changed: {path}")
    for note in manual:
        print(f"manual: {note}")
    dry = args.check or args.dry_run
    print(f"{len(changed)} file(s) {'would change' if dry else 'changed'}")
    return 1 if args.check and changed else 0


if __name__ == "__main__":
    sys.exit(main())
