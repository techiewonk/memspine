"""ONE-TIME rewrite (ADR-096 phase D: RM-19/RM-20/RM-21/PA-107). Not a user-facing codemod.

Rewrites the repository's own *active* evaluation configs to the current validated config
shape, then validates each rewritten config with ``validate_engine_config`` (schema, policy
blocks and each selected provider's own settings; no engine start, no network).

    python scripts/rewrite_eval_arms_phase_d.py [--check]

Active configs (rewritten): ``evals/arms/*.json``, ``evals/plans/*.json`` (``memspine_base.config``
and each arm's ``config_delta``), ``evals/memspine_evals/inspect/*arm.json``.

Historical files (never read or written): everything under ``evals/analysis/``,
``evals/results_qwen_stack/``, ``evals/runs/``, ``evals/labelled/``, ``evals/legacy_datasets/``,
``evals/tests/fixtures/``, ``docs/provider_verification/``, and any ``summary.json``,
``manifest.json``, ``results.jsonl`` or ``*.STATUS`` run output.

Mapping (old shape -> ADR-081/082/084 shape):

- ``storage.{path,provider,settings,profile}``  -> ``storage.records.{provider,settings}``
- top-level ``embedding``                        -> ``model.embedding``
- ``model.embedding.provider: fastembed``        -> ``onnx`` (ADR-084)
- ``read.rerank`` / ``read.rerank_model`` / ``read.rerank_device`` -> ``model.rerank.chain[0]``;
  ``read.rerank_keep`` / ``read.rerank_floor`` -> ``model.rerank.keep`` / ``.floor``
- ``llm.roles.<role>``                           -> ``model.llm.<role>`` (``provider`` and
  ``timeout_s`` required; ``timeout_s`` defaults to 180 when absent)
- ``ollama`` LLM ``settings.base_url``          -> ``settings.api_base`` (OllamaRoleSettings)
- a selected LLM / reranker whose descriptor ``requires_deployment_id`` (a shared server such
  as ``ollama``) and no ``deployment.id``        -> ``deployment.id: local-evaluation``
  (plan configs are skipped: ``c0-1 --memspine-llm`` adds ``deployment.id`` at run time)

Once every active file is current, ``--check`` reports nothing to do; delete this script
after the phase D rows close.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
EVALS = REPO / "evals"
DEFAULT_TIMEOUT_S = 180.0
LOCAL_DEPLOYMENT_ID = "local-evaluation"


def _storage(cfg: dict[str, Any], hits: Counter[str]) -> None:
    storage = cfg.get("storage")
    if not isinstance(storage, dict):
        return
    flat = {k: storage.pop(k) for k in ("path", "provider", "settings", "profile") if k in storage}
    if not flat:
        return
    hits["storage.flat->storage.records"] += 1
    settings = dict(flat.get("settings") or {})
    if "path" in flat:
        settings["path"] = flat["path"]
    storage["records"] = {"provider": flat.get("provider") or "sqlite", "settings": settings}


def _embedding(cfg: dict[str, Any], hits: Counter[str]) -> None:
    model = cfg.setdefault("model", {})
    if "embedding" in cfg:
        hits["embedding->model.embedding"] += 1
        old = dict(cfg.pop("embedding"))
        provider = old.pop("provider", "onnx")
        model["embedding"] = {"provider": provider, "settings": old}
    embedding = model.get("embedding")
    if isinstance(embedding, dict) and embedding.get("provider") == "fastembed":
        hits["fastembed->onnx"] += 1
        embedding["provider"] = "onnx"
    if not model:
        cfg.pop("model")


def _rerank(cfg: dict[str, Any], hits: Counter[str]) -> None:
    read = cfg.get("read")
    if not isinstance(read, dict):
        return
    keys = ("rerank", "rerank_model", "rerank_device", "rerank_keep", "rerank_floor")
    old = {k: read.pop(k) for k in keys if k in read}
    if not old:
        return
    hits["read.rerank*->model.rerank"] += 1
    rerank = cfg.setdefault("model", {}).setdefault("rerank", {})
    provider = old.get("rerank", "off")
    if provider != "off":
        settings = {
            k: v
            for k, v in (("model", old.get("rerank_model")), ("device", old.get("rerank_device")))
            if v is not None
        }
        rerank["chain"] = [{"provider": provider, "settings": settings}]
    else:
        rerank["chain"] = []
    if "rerank_keep" in old:
        rerank["keep"] = old["rerank_keep"]
    if "rerank_floor" in old:
        rerank["floor"] = old["rerank_floor"]


def _llm(cfg: dict[str, Any], hits: Counter[str]) -> None:
    llm = cfg.get("llm")
    if isinstance(llm, dict) and "roles" in llm:
        hits["llm.roles->model.llm"] += 1
        slots = cfg.setdefault("model", {}).setdefault("llm", {})
        for role, slot in llm.pop("roles").items():
            slots.setdefault(role, slot)
        if not llm:
            cfg.pop("llm")
    for slot in ((cfg.get("model") or {}).get("llm") or {}).values():
        if "timeout_s" not in slot:
            hits["model.llm.<role>.timeout_s added"] += 1
            slot["timeout_s"] = DEFAULT_TIMEOUT_S
        targets = list((slot.get("escalation") or {}).get("targets") or [])
        for entry in [slot, *targets]:
            settings = entry.get("settings") or {}
            if entry.get("provider") == "ollama" and "base_url" in settings:
                hits["ollama settings.base_url->api_base"] += 1
                settings["api_base"] = settings.pop("base_url")


def _deployment(cfg: dict[str, Any], hits: Counter[str]) -> None:
    """A shared (host/cluster) provider needs an explicit ``deployment.id`` (ADR-081)."""
    from memspine.providers.registry import get_registry

    model = cfg.get("model") or {}
    selected = [("llm_providers", s.get("provider")) for s in (model.get("llm") or {}).values()]
    selected += [
        ("rerankers", s.get("provider")) for s in (model.get("rerank") or {}).get("chain") or []
    ]
    needs = False
    for kind, name in selected:
        try:
            needs = needs or bool(get_registry(kind).get(name).requires_deployment_id)
        except Exception:
            continue
    if needs and not (cfg.get("deployment") or {}).get("id"):
        hits["deployment.id added for a shared provider"] += 1
        cfg.setdefault("deployment", {})["id"] = LOCAL_DEPLOYMENT_ID


def rewrite_config(cfg: dict[str, Any], hits: Counter[str], *, plan: bool = False) -> None:
    _storage(cfg, hits)
    _embedding(cfg, hits)
    _rerank(cfg, hits)
    _llm(cfg, hits)
    if not plan:
        _deployment(cfg, hits)


def _configs(path: Path, data: dict[str, Any]) -> list[dict[str, Any]]:
    """The engine-config mappings inside one active file."""
    if path.parent.name == "plans":
        out = [data.get("memspine_base", {}).get("config") or {}]
        out += [arm["config_delta"] for arm in data.get("arms", []) if arm.get("config_delta")]
        return out
    return [data]


def active_files() -> list[Path]:
    return [
        *sorted((EVALS / "arms").glob("*.json")),
        *sorted((EVALS / "plans").glob("*.json")),
        *sorted((EVALS / "memspine_evals" / "inspect").glob("*arm.json")),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="report only, write nothing")
    args = parser.parse_args()
    sys.path.insert(0, str(EVALS))
    from memspine_evals.rehearsal import arm_engine_config, validate_engine_config

    totals: Counter[str] = Counter()
    changed: list[Path] = []
    failures: list[str] = []
    for path in active_files():
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        ascii_only = text == json.dumps(json.loads(text), indent=2) + "\n"
        hits: Counter[str] = Counter()
        for cfg in _configs(path, data):
            rewrite_config(cfg, hits, plan=path.parent.name == "plans")
        if hits:
            totals.update(hits)
            changed.append(path)
            if not args.check:
                out = json.dumps(data, indent=2, ensure_ascii=ascii_only) + "\n"
                path.write_text(out, encoding="utf-8", newline="\n")
        if path.parent.name == "plans":
            engine_cfgs = []
            for arm in data.get("arms", []):
                cfg = arm_engine_config(data, arm)
                if cfg is None:
                    continue
                if data.get("protocol", {}).get("memspine_llm"):
                    # c0-1 --memspine-llm adds deployment.id (merge_engine_llm_roles)
                    cfg.setdefault("deployment", {}).setdefault("id", "bedrock-evaluation")
                engine_cfgs.append((f"{path.name}:{arm['id']}", cfg))
        else:
            engine_cfgs = [(path.name, data)]
        for name, cfg in engine_cfgs:
            try:
                validate_engine_config(cfg)
            except Exception as exc:  # report every file, not only the first
                failures.append(f"{name}: {exc}")
    print(f"{len(active_files())} active files, {len(changed)} rewritten")
    for rule, count in sorted(totals.items()):
        print(f"  {count:4d}  {rule}")
    for path in changed:
        print(f"  rewrote {path.relative_to(REPO).as_posix()}")
    for failure in failures:
        print(f"  INVALID {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
