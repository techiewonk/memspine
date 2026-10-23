"""Replace fastembed provider selections with onnx (ADR-084), without aliases.

Usage: python scripts/migrate_config_keys_adr084.py [--check | --dry-run] PATH...
Supports Python literal provider fields and YAML/JSON mappings. Historical result
directories must not be passed to this command. A second run is a no-op.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import json
from pathlib import Path
from typing import Any


def migrate_mapping(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "onnx" if key == "provider" and child == "fastembed" else migrate_mapping(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [migrate_mapping(child) for child in value]
    return value


def migrate_python(source: str) -> str:
    """Patch only literal provider values, keeping comments and unrelated strings."""
    tree = ast.parse(source)
    candidates: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            candidates.extend(
                value
                for key, value in zip(node.keys, node.values, strict=True)
                if isinstance(key, ast.Constant) and key.value == "provider"
            )
        elif isinstance(node, ast.keyword) and node.arg == "provider":
            candidates.append(node.value)
    lines = source.encode("utf-8").splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    edits = []
    for node in candidates:
        if isinstance(node, ast.Constant) and node.value == "fastembed":
            assert node.end_lineno is not None and node.end_col_offset is not None
            edits.append(
                (
                    offsets[node.lineno - 1] + node.col_offset,
                    offsets[node.end_lineno - 1] + node.end_col_offset,
                )
            )
    result = source.encode("utf-8")
    for start, end in sorted(set(edits), reverse=True):
        result = result[:start] + b'"onnx"' + result[end:]
    return result.decode("utf-8")


def migrate_source(source: str, suffix: str) -> str:
    if suffix == ".py":
        return migrate_python(source)
    if suffix == ".json":
        old = json.loads(source)
        new = migrate_mapping(old)
        return source if old == new else json.dumps(new, indent=2, ensure_ascii=False) + "\n"
    import yaml

    old = yaml.safe_load(source)
    new = migrate_mapping(old)
    return source if old == new else yaml.safe_dump(new, sort_keys=False, allow_unicode=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()
    changed = False
    for path in args.paths:
        files = sorted(path.rglob("*")) if path.is_dir() else [path]
        for file in files:
            if file.suffix not in {".py", ".yaml", ".yml", ".json"}:
                continue
            source = file.read_text(encoding="utf-8")
            if "migrate_config_keys_adr084: skip" in source:
                continue
            target = migrate_source(source, file.suffix)
            if source == target:
                continue
            changed = True
            print(file)
            if args.dry_run:
                print(
                    "".join(difflib.unified_diff(source.splitlines(True), target.splitlines(True)))
                )
            elif not args.check:
                file.write_text(target, encoding="utf-8", newline="\n")
    return int(args.check and changed)


if __name__ == "__main__":
    raise SystemExit(main())
