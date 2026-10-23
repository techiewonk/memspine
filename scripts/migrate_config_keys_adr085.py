"""Rewrite removed litellm provider slots (ADR-085), preserving model slots and values.

Usage: python scripts/migrate_config_keys_adr085.py config.yaml [--check]
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

import yaml


def migrate(value: Any) -> Any:
    if isinstance(value, list):
        return [migrate(item) for item in value]
    if not isinstance(value, dict):
        return value
    out = {key: migrate(item) for key, item in value.items()}
    if out.get("provider") != "litellm":
        return out
    settings = copy.deepcopy(out.get("settings", {}))
    model = settings.get("model", "")
    if "/" in model:
        route, bare = model.split("/", 1)
    else:
        route, bare = "", model
    if route in {"ollama", "ollama_chat", "hosted_vllm", "bedrock"}:
        out["provider"] = {"ollama_chat": "ollama", "hosted_vllm": "vllm"}.get(route, route)
        settings["model"] = bare
    elif route in {"openai", ""}:
        out["provider"] = "openai_compatible"
        settings["model"] = bare
        if not settings.get("api_base"):
            raise ValueError("openai_compatible migration requires an explicit settings.api_base")
    else:
        out["provider"] = "anyllm"
        settings["upstream_provider"] = route
        settings["model"] = bare
    out["settings"] = settings
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    changed = False
    for path in args.paths:
        text = path.read_text(encoding="utf-8")
        is_json = path.suffix == ".json"
        original = json.loads(text) if is_json else yaml.safe_load(text)
        result = migrate(original)
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
    return int(args.check and changed)


if __name__ == "__main__":
    raise SystemExit(main())
