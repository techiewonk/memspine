"""Rewrite removed litellm provider slots (ADR-085), preserving model slots and values.

Usage: python scripts/migrate_config_keys_adr085.py config.yaml [--check]
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from memspine.exceptions import ConfigError


def _category(path: tuple[str, ...]) -> str | None:
    if "model" not in path:
        return None
    section = path[path.index("model") + 1 :]
    return section[0] if section and section[0] in {"embedding", "llm", "rerank"} else None


def migrate(value: Any, *, _path: tuple[str, ...] = ()) -> Any:
    if isinstance(value, list):
        return [migrate(item, _path=(*_path, str(index))) for index, item in enumerate(value)]
    if not isinstance(value, dict):
        return value
    out = {key: migrate(item, _path=(*_path, str(key))) for key, item in value.items()}
    if out.get("provider") != "litellm":
        return out
    settings = copy.deepcopy(out.get("settings", {}))
    model = settings.get("model", "")
    if not isinstance(model, str) or not model:
        raise ValueError(f"{'.'.join(_path)}: migration requires an explicit model id")
    route, bare = model.split("/", 1) if "/" in model else ("", model)
    category = _category(_path)
    if category is None:
        raise ValueError(
            "cannot infer model category; migrate a complete model.embedding, model.llm or model.rerank configuration"
        )
    if category == "rerank" and route not in {"hosted_vllm", "bedrock"}:
        if route in {"ollama", "ollama_chat"}:
            raise ValueError("Ollama has no rerank API; manually select onnx or vllm")
        if not settings.get("api_base"):
            raise ValueError(
                "hosted rerank migration needs an explicit OpenAI-compatible settings.api_base; anyllm has no rerank API"
            )
        out["provider"] = "openai_compatible"
        settings["model"] = bare
        settings["family"] = route if route in {"cohere", "jina"} else "openai_compatible"
    elif route in {"ollama", "ollama_chat", "hosted_vllm", "bedrock"}:
        out["provider"] = {"ollama_chat": "ollama", "hosted_vllm": "vllm"}.get(route, route)
        settings["model"] = bare.removeprefix("converse/") if route == "bedrock" else bare
    elif route in {"openai", ""}:
        out["provider"] = "openai_compatible"
        settings["model"] = bare
        if not settings.get("api_base"):
            raise ValueError("openai_compatible migration requires an explicit settings.api_base")
    else:
        out["provider"] = "anyllm"
        settings["upstream_provider"] = route
        settings["model"] = bare
        if settings.get("api_key"):
            raise ValueError(
                "anyllm migration requires moving settings.api_key to a named settings.api_key_env environment variable"
            )
        settings.pop("api_key", None)
    if out["provider"] != "bedrock":
        if settings.get("aws_region"):
            raise ValueError(
                "non-Bedrock migration has settings.aws_region; remove it or select bedrock manually"
            )
        settings.pop("aws_region", None)
    else:
        if settings.get("api_base") or settings.get("api_key") or settings.get("api_key_env"):
            raise ValueError(
                "Bedrock migration uses the AWS credential chain; remove HTTP endpoint/key settings manually"
            )
        for key in ("api_base", "api_key", "api_key_env"):
            settings.pop(key, None)
    out["settings"] = settings
    # Refuse a partially valid rewrite. In particular, a rerank slot must never silently
    # select the LLM-only SDK API; model categories have distinct provider registries.
    from memspine.providers.registry import get_registry, validate_settings

    kind = {"embedding": "embedders", "llm": "llm_providers", "rerank": "rerankers"}[category]
    descriptor = get_registry(kind).get(out["provider"])
    native = dict(settings)
    if category == "llm":
        native["timeout_seconds"] = out.get("timeout_s", 60.0)
    validate_settings(descriptor, native)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    changed = False
    manual = False
    for path in args.paths:
        text = path.read_text(encoding="utf-8")
        is_json = path.suffix == ".json"
        original = json.loads(text) if is_json else yaml.safe_load(text)
        try:
            result = migrate(original)
        except (ValueError, ConfigError) as exc:
            print(f"manual: {path}: {exc}", file=sys.stderr)
            manual = True
            continue
        if result == original:
            continue
        changed = True
        print(path)
        if not args.check:
            path.write_text(
                json.dumps(result, indent=2) + "\n"
                if is_json
                else yaml.safe_dump(result, sort_keys=False),
                encoding="utf-8",
                newline="\n",
            )
    return 2 if manual else int(args.check and changed)


if __name__ == "__main__":
    raise SystemExit(main())
