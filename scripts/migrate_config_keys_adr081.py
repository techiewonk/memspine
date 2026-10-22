"""Codemod: move pre-ADR-081 config keys to the storage slots and the deployment block.

ADR-081 / RM-111 WP7. There are no aliases (ADR-068), so every config shape in the tree
must move. This rewrites Python sources (dict literals, keyword arguments, item
assignments and attribute paths) and YAML files::

    storage.{profile,provider,settings}   -> storage.records.{provider,settings}
    storage.settings.data_dir              -> deployment.data_dir
    vector.* / cache.*                     -> storage.vector.* / storage.cache.*
    graph.{provider,settings}              -> storage.graph.*
    graph.mirror {enabled: true, ...}      -> storage.graph_mirror (dropped when disabled)
    read.{lexical_provider,lexical_settings} -> storage.lexical.{provider,settings}
    runtime.deployment_id                  -> deployment.id

Usage::

    python scripts/migrate_config_keys_adr081.py [--check] PATH...

Python dict literals that are rebuilt lose comments *between their top-level entries*; the
entry values keep their original source text. Sites the codemod cannot prove (a config
held in a variable, YAML inside a string) are listed as ``manual:`` for hand review.
YAML files are rewritten through PyYAML and therefore lose comments; they are listed.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

#: A file containing this marker is left untouched (it tests the old keys on purpose).
SKIP_MARKER = "migrate_config_keys_adr081: skip"
SLOT_ROOT_KEYS = ("vector", "cache")
CONFIG_KEYS = {"storage", "vector", "cache", "graph", "read", "runtime", "deployment"}
OLD_STORAGE_KEYS = {"profile", "provider", "settings"}
SLOT_KEYS = {"provider", "settings"}
GRAPH_MOVED = {"provider", "settings", "mirror"}
GRAPH_FLAGS = {"entity_embeddings", "key_v2", "edge_compiler_v2"}
READ_MOVED = {"lexical_provider", "lexical_settings"}

# ── attribute paths (Python) ──────────────────────────────────────────────────────────
ATTRIBUTE_SUBS: tuple[tuple[str, str], ...] = (
    (r"\.storage\.provider\b", ".storage.records.provider"),
    (r"\.storage\.settings\b", ".storage.records.settings"),
    (r"(?<!storage)\.vector\.(provider|settings)\b", r".storage.vector.\1"),
    (r"(?<!storage)(?<!read)\.cache\.(provider|settings)\b", r".storage.cache.\1"),
    (r"\.graph\.mirror\.enabled\b", ".storage.graph_mirror"),
    (r"\.graph\.mirror\.(provider|settings)\b", r".storage.graph_mirror.\1"),
    (r"(?<!storage)\.graph\.(provider|settings)\b", r".storage.graph.\1"),
    (r"\.read\.lexical_provider\b", ".storage.lexical.provider"),
    (r"\.read\.lexical_settings\b", ".storage.lexical.settings"),
    (r"\.runtime\.deployment_id\b", ".deployment.id"),
)


@dataclass
class Item:
    """One mapping entry: its key and the source text of its value (or a nested plan)."""

    key: str | None  # None = ``**spread``
    text: str


@dataclass
class Plan:
    """The rewritten top-level config keys of one root mapping."""

    storage: dict[str, str] = field(default_factory=dict)  # slot -> value text
    storage_extra: list[Item] = field(default_factory=list)  # already-new storage entries
    deployment: dict[str, str] = field(default_factory=dict)
    deployment_extra: list[Item] = field(default_factory=list)
    replaced: dict[str, str | None] = field(default_factory=dict)  # key -> new text / drop
    manual: list[str] = field(default_factory=list)


def _const_key(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


class Source:
    def __init__(self, text: str) -> None:
        self.text = text
        self._lines = text.splitlines(keepends=True)
        self._offsets = [0]
        for line in self._lines:
            self._offsets.append(self._offsets[-1] + len(line))

    def offset(self, line: int, col: int) -> int:
        # ast columns are UTF-8 byte offsets
        raw = self._lines[line - 1].encode("utf-8")[:col].decode("utf-8")
        return self._offsets[line - 1] + len(raw)

    def span(self, node: ast.AST) -> tuple[int, int]:
        assert node.end_lineno is not None and node.end_col_offset is not None
        return (
            self.offset(node.lineno, node.col_offset),  # type: ignore[attr-defined]
            self.offset(node.end_lineno, node.end_col_offset),
        )

    def seg(self, node: ast.AST) -> str:
        start, end = self.span(node)
        return self.text[start:end]


def _dict_items(node: ast.Dict, src: Source) -> list[tuple[str | None, ast.expr]]:
    return [(_const_key(k) if k is not None else None, v) for k, v in zip(node.keys, node.values)]


def _render_dq(items: Iterable[Item]) -> str:
    parts = []
    for item in items:
        if item.key is None:
            parts.append(f"**{item.text}")
        else:
            parts.append(f'"{item.key}": {item.text}')
    return "{" + ", ".join(parts) + "}"


def _literal_dict(node: ast.expr) -> ast.Dict | None:
    if isinstance(node, ast.Dict) and all(
        k is None or _const_key(k) is not None for k in node.keys
    ):
        return node
    return None


def _is_old_storage(node: ast.expr) -> bool:
    d = _literal_dict(node)
    if d is None:
        return False
    keys = {k for k, _ in _dict_items(d, None)}  # type: ignore[arg-type]
    return bool(keys & OLD_STORAGE_KEYS) and None not in keys


def _is_slot_dict(node: ast.expr) -> bool:
    d = _literal_dict(node)
    if d is None:
        return False
    keys = {_const_key(k) for k in d.keys}
    return bool(keys) and keys <= SLOT_KEYS


def _is_old_graph(node: ast.expr) -> bool:
    d = _literal_dict(node)
    if d is None:
        return False
    keys = {_const_key(k) for k in d.keys}
    # Only a real graph config block (a report dict such as {"provider", "outcome"} is not).
    return bool(keys & GRAPH_MOVED) and keys <= GRAPH_MOVED | GRAPH_FLAGS


def _is_old_read(node: ast.expr) -> bool:
    d = _literal_dict(node)
    return d is not None and bool({_const_key(k) for k in d.keys} & READ_MOVED)


def _is_old_runtime(node: ast.expr) -> bool:
    d = _literal_dict(node)
    return d is not None and "deployment_id" in {_const_key(k) for k in d.keys}


def _storage_records(node: ast.Dict, src: Source, plan: Plan) -> None:
    """Old ``storage`` -> ``records`` slot (+ ``deployment.data_dir``)."""
    items = dict(_dict_items(node, src))
    record: list[Item] = []
    provider = items.get("provider")
    profile = items.get("profile")
    if provider is not None:
        record.append(Item("provider", src.seg(provider)))
    elif profile is not None and _const_key(profile) == "server":
        record.append(Item("provider", '"postgres"'))
    settings = items.get("settings")
    if settings is not None:
        sd = _literal_dict(settings)
        if sd is not None and "data_dir" in {_const_key(k) for k in sd.keys}:
            kept = []
            for key, value in _dict_items(sd, src):
                if key == "data_dir":
                    plan.deployment["data_dir"] = src.seg(value)
                else:
                    kept.append(Item(key, src.seg(value)))
            record.append(Item("settings", _render_dq(kept)))
        else:
            record.append(Item("settings", src.seg(settings)))
    for key, value in _dict_items(node, src):
        if key not in OLD_STORAGE_KEYS:
            plan.storage_extra.append(Item(key, src.seg(value)))
    if record:
        plan.storage["records"] = _render_dq(record)


def plan_root(items: list[tuple[str | None, ast.expr]], src: Source) -> Plan | None:
    """Plan the rewrite of one root config mapping; None when nothing moves."""
    by_key = {k: v for k, v in items if k is not None}
    plan = Plan()
    moved = False
    storage = by_key.get("storage")
    if storage is not None:
        d = _literal_dict(storage)
        if d is not None and _is_old_storage(storage):
            _storage_records(d, src, plan)
            plan.replaced["storage"] = None
            moved = True
        elif d is not None:
            for key, value in _dict_items(d, src):
                plan.storage_extra.append(Item(key, src.seg(value)))
        else:
            # A storage value held in a variable or call: keep it as a spread so moved slots
            # merge into it, and flag it, since its shape cannot be proven here.
            plan.storage_extra.append(Item(None, src.seg(storage)))
            plan.manual.append("storage is not a literal: check its value has the slot shape")
    for key in SLOT_ROOT_KEYS:
        value = by_key.get(key)
        if value is not None and _is_slot_dict(value):
            plan.storage[key] = src.seg(value)
            plan.replaced[key] = None
            moved = True
    graph = by_key.get("graph")
    if graph is not None and _is_old_graph(graph):
        gd = _literal_dict(graph)
        assert gd is not None
        kept: list[Item] = []
        slot: list[Item] = []
        for key, value in _dict_items(gd, src):
            if key in {"provider", "settings"}:
                slot.append(Item(key, src.seg(value)))
            elif key == "mirror":
                md = _literal_dict(value)
                if md is None:
                    plan.manual.append("graph.mirror is not a literal")
                    continue
                mitems = dict(_dict_items(md, src))
                enabled = mitems.get("enabled")
                if isinstance(enabled, ast.Constant) and enabled.value is True:
                    mslot = [
                        Item(
                            "provider",
                            src.seg(mitems["provider"]) if "provider" in mitems else '"neo4j"',
                        )
                    ]
                    if "settings" in mitems:
                        mslot.append(Item("settings", src.seg(mitems["settings"])))
                    plan.storage["graph_mirror"] = _render_dq(mslot)
                elif enabled is not None and not (
                    isinstance(enabled, ast.Constant) and enabled.value is False
                ):
                    plan.manual.append("graph.mirror.enabled is not a literal")
            else:
                kept.append(Item(key, src.seg(value)))
        if slot:
            plan.storage["graph"] = _render_dq(slot)
        plan.replaced["graph"] = _render_dq(kept) if kept else None
        moved = True
    read = by_key.get("read")
    if read is not None and _is_old_read(read):
        rd = _literal_dict(read)
        assert rd is not None
        kept = []
        slot = []
        for key, value in _dict_items(rd, src):
            if key == "lexical_provider":
                slot.append(Item("provider", src.seg(value)))
            elif key == "lexical_settings":
                slot.append(Item("settings", src.seg(value)))
            else:
                kept.append(Item(key, src.seg(value)))
        plan.storage["lexical"] = _render_dq(slot)
        plan.replaced["read"] = _render_dq(kept) if kept else None
        moved = True
    runtime = by_key.get("runtime")
    if runtime is not None and _is_old_runtime(runtime):
        rt = _literal_dict(runtime)
        assert rt is not None
        kept = []
        for key, value in _dict_items(rt, src):
            if key == "deployment_id":
                plan.deployment["id"] = src.seg(value)
            else:
                kept.append(Item(key, src.seg(value)))
        plan.replaced["runtime"] = _render_dq(kept) if kept else None
        moved = True
    deployment = by_key.get("deployment")
    if moved and deployment is not None and plan.deployment:
        dd = _literal_dict(deployment)
        if dd is None:
            plan.manual.append("deployment is not a literal")
        else:
            for key, value in _dict_items(dd, src):
                if key not in plan.deployment:
                    plan.deployment_extra.append(Item(key, src.seg(value)))
            plan.replaced["deployment"] = None
    if moved and storage is not None and "storage" not in plan.replaced:
        plan.replaced["storage"] = None  # merged into the new storage mapping
    return plan if moved else None


def storage_text(plan: Plan) -> str:
    slots = [Item(slot, text) for slot, text in plan.storage.items()]
    existing = {item.key for item in slots}
    return _render_dq([*[i for i in plan.storage_extra if i.key not in existing], *slots])


def deployment_text(plan: Plan) -> str:
    items = [Item(k, v) for k, v in plan.deployment.items()]
    return _render_dq([*plan.deployment_extra, *items])


@dataclass
class Edit:
    start: int
    end: int
    text: str


class PyRewriter(ast.NodeVisitor):
    def __init__(self, src: Source, path: Path) -> None:
        self.src = src
        self.path = path
        self.edits: list[Edit] = []
        self.manual: list[str] = []
        #: ids of mappings that are already a `storage` value (never a config root), so a
        #: second run over migrated code is a no-op.
        self.storage_values: set[int] = set()

    def _mark_storage(self, key: str | None, value: ast.expr) -> None:
        if key == "storage" and isinstance(value, ast.Dict):
            self.storage_values.add(id(value))

    def _covered(self, node: ast.AST) -> bool:
        start, end = self.src.span(node)
        return any(e.start <= start and end <= e.end for e in self.edits)

    def visit_Dict(self, node: ast.Dict) -> None:
        if self._covered(node):
            return
        items = _dict_items(node, self.src)
        for key, value in items:
            self._mark_storage(key, value)
        keys = {k for k, _ in items}
        if keys & CONFIG_KEYS and id(node) not in self.storage_values:
            plan = plan_root(items, self.src)
            if plan is not None:
                self._apply_dict(node, items, plan)
                return
        self.generic_visit(node)

    def _apply_dict(
        self, node: ast.Dict, items: list[tuple[str | None, ast.expr]], plan: Plan
    ) -> None:
        entries = []
        for (key, value), knode in zip(items, node.keys, strict=True):
            if key is None or knode is None:
                continue
            start = self.src.span(knode)[0]
            entries.append((key, start, self.src.span(value)[1], value))
        self._apply_entries(entries, plan, keyword=False)
        self.manual.extend(f"{self.path}:{node.lineno}: {m}" for m in plan.manual)

    def visit_Call(self, node: ast.Call) -> None:
        if self._covered(node):
            return
        kws = [(kw.arg, kw.value) for kw in node.keywords]
        for key, value in kws:
            self._mark_storage(key, value)
        if len(node.args) == 2:
            self._mark_storage(_const_key(node.args[0]), node.args[1])
        keys = {k for k, _ in kws}
        plan = plan_root(kws, self.src) if keys & CONFIG_KEYS else None
        if plan is not None:
            self._apply_kwargs(node, plan)
        # storage passed positionally to setdefault("storage", {...}) / x["storage"] = {...}
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "setdefault"
            and len(node.args) == 2
            and _const_key(node.args[0]) in {"storage", "vector", "cache", "graph"}
        ):
            if self._rewrite_single(
                node, func.value, _const_key(node.args[0]), node.args[1], call=True
            ):
                return
        self.generic_visit(node)

    def _apply_kwargs(self, node: ast.Call, plan: Plan) -> None:
        entries = []
        for kw in node.keywords:
            if kw.arg is None:
                continue
            start, end = self.src.span(kw)
            entries.append((kw.arg, start, end, kw.value))
        self._apply_entries(entries, plan, keyword=True)
        self.manual.extend(f"{self.path}:{node.lineno}: {m}" for m in plan.manual)

    def _delete(self, start: int, end: int) -> None:
        """Delete one entry with its trailing (else leading) comma."""
        tail = re.match(r"\s*,\s*", self.src.text[end:])
        if tail:
            self.edits.append(Edit(start, end + tail.end(), ""))
            return
        head = re.search(r",\s*$", self.src.text[:start])
        self.edits.append(Edit(head.start() if head else start, end, ""))

    def _apply_entries(
        self, entries: list[tuple[str, int, int, ast.expr]], plan: Plan, *, keyword: bool
    ) -> None:
        """Edit entries in place so untouched entries keep their formatting and comments."""

        def fmt(key: str, text: str) -> str:
            return f"{key}={text}" if keyword else f'"{key}": {text}'

        new_storage = storage_text(plan) if (plan.storage or plan.storage_extra) else None
        new_deploy = deployment_text(plan) if plan.deployment else None
        storage_done = new_storage is None
        deploy_done = new_deploy is None
        last_end = entries[-1][2] if entries else None
        for key, start, end, value in entries:
            if key == "storage" and new_storage is not None:
                self.edits.append(Edit(*self.src.span(value), new_storage))
                storage_done = True
            elif key == "deployment" and new_deploy is not None:
                self.edits.append(Edit(*self.src.span(value), new_deploy))
                deploy_done = True
        for key, start, end, value in entries:
            if key in {"storage", "deployment"} or key not in plan.replaced:
                continue
            replacement = plan.replaced[key]
            if replacement is not None:
                self.edits.append(Edit(*self.src.span(value), replacement))
                extra = []
                if not storage_done and key in {"graph", "read"}:
                    extra.append(fmt("storage", new_storage or "{}"))
                    storage_done = True
                if not deploy_done and key == "runtime":
                    extra.append(fmt("deployment", new_deploy or "{}"))
                    deploy_done = True
                if extra:
                    self.edits.append(Edit(end, end, ", " + ", ".join(extra)))
            elif not storage_done and key in {"vector", "cache", "graph", "read"}:
                self.edits.append(Edit(start, end, fmt("storage", new_storage or "{}")))
                storage_done = True
            elif not deploy_done and key == "runtime":
                self.edits.append(Edit(start, end, fmt("deployment", new_deploy or "{}")))
                deploy_done = True
            else:
                self._delete(start, end)
        tail = []
        if not storage_done:
            tail.append(fmt("storage", new_storage or "{}"))
        if not deploy_done:
            tail.append(fmt("deployment", new_deploy or "{}"))
        if tail and last_end is not None:
            self.edits.append(Edit(last_end, last_end, ", " + ", ".join(tail)))

    def _rewrite_single(
        self, node: ast.AST, target: ast.expr, key: str | None, value: ast.expr, *, call: bool
    ) -> bool:
        target_src = self.src.seg(target)
        if re.search(r"""["']storage["']""", target_src):
            return False  # already under storage (x.setdefault("storage", {})["graph"] = ...)
        value_src = self.src.seg(value)
        if key == "storage":
            d = _literal_dict(value)
            if d is not None and _is_old_storage(value):
                plan = Plan()
                _storage_records(d, self.src, plan)
                start, end = self.src.span(value)
                self.edits.append(Edit(start, end, storage_text(plan)))
                if plan.deployment and not call:
                    indent = " " * getattr(node, "col_offset", 0)
                    stmt_end = self.src.span(node)[1]
                    self.edits.append(
                        Edit(
                            stmt_end,
                            stmt_end,
                            f'\n{indent}{target_src}.setdefault("deployment", {{}})'
                            f'["data_dir"] = {plan.deployment["data_dir"]}',
                        )
                    )
                elif plan.deployment:
                    self.manual.append(f"{self.path}:{getattr(node, 'lineno', 0)}: data_dir")
                return True
            return False
        if (key in {"vector", "cache"} and _is_slot_dict(value)) or (
            key == "graph" and _is_slot_dict(value)
        ):
            new = (
                f'{target_src}.setdefault("storage", {{}}).setdefault("{key}", {value_src})'
                if call
                else f'{target_src}.setdefault("storage", {{}})["{key}"] = {value_src}'
            )
            start, end = self.src.span(node)
            self.edits.append(Edit(start, end, new))
            return True
        return False

    def visit_Assign(self, node: ast.Assign) -> None:
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Subscript):
            sub = node.targets[0]
            key = _const_key(sub.slice)
            self._mark_storage(key, node.value)
            if key in {"storage", "vector", "cache", "graph"}:
                if self._rewrite_single(node, sub.value, key, node.value, call=False):
                    return
        self.generic_visit(node)


def rewrite_python(path: Path) -> tuple[str, list[str]]:
    text = path.read_text(encoding="utf-8")
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
            rewriter.manual.append(f"{path}: overlapping edit skipped at offset {edit.start}")
            continue
        out = out[: edit.start] + edit.text + out[edit.end :]
        last_start = edit.start
    for pattern, repl in ATTRIBUTE_SUBS:
        out = re.sub(pattern, repl, out)
    if re.search(
        r"storage\.profile|lexical_provider|deployment_id\s*=|\"profile\":\s*\"server\"", out
    ):
        rewriter.manual.append(f"{path}: residual old key text")
    return out, rewriter.manual


# ── YAML / plain mappings ──────────────────────────────────────────────────────────────


def migrate_mapping(data: dict) -> bool:
    """Rewrite one plain config mapping in place; True when something moved."""
    changed = False
    storage = data.get("storage")
    new_storage: dict = {}
    deployment: dict = dict(data.get("deployment") or {})
    if isinstance(storage, dict):
        if set(storage) & OLD_STORAGE_KEYS:
            record = {}
            if "provider" in storage:
                record["provider"] = storage["provider"]
            elif storage.get("profile") == "server":
                record["provider"] = "postgres"
            if "settings" in storage:
                settings = dict(storage["settings"] or {})
                if "data_dir" in settings:
                    deployment["data_dir"] = settings.pop("data_dir")
                record["settings"] = settings
            new_storage = {k: v for k, v in storage.items() if k not in OLD_STORAGE_KEYS}
            if record:
                new_storage["records"] = record
            changed = True
        else:
            new_storage = dict(storage)
    for key in SLOT_ROOT_KEYS:
        if isinstance(data.get(key), dict) and set(data[key]) <= SLOT_KEYS:
            new_storage[key] = data.pop(key)
            changed = True
    graph = data.get("graph")
    if (
        isinstance(graph, dict)
        and set(graph) & GRAPH_MOVED
        and set(graph) <= GRAPH_MOVED | GRAPH_FLAGS
    ):
        slot = {k: graph.pop(k) for k in ("provider", "settings") if k in graph}
        if slot:
            new_storage["graph"] = slot
        mirror = graph.pop("mirror", None)
        if isinstance(mirror, dict) and mirror.get("enabled") is True:
            new_storage["graph_mirror"] = {
                "provider": mirror.get("provider", "neo4j"),
                **({"settings": mirror["settings"]} if "settings" in mirror else {}),
            }
        if not graph:
            data.pop("graph")
        changed = True
    read = data.get("read")
    if isinstance(read, dict) and set(read) & READ_MOVED:
        slot = {}
        if "lexical_provider" in read:
            slot["provider"] = read.pop("lexical_provider")
        if "lexical_settings" in read:
            slot["settings"] = read.pop("lexical_settings")
        new_storage["lexical"] = slot
        if not read:
            data.pop("read")
        changed = True
    runtime = data.get("runtime")
    if isinstance(runtime, dict) and "deployment_id" in runtime:
        deployment["id"] = runtime.pop("deployment_id")
        changed = True
    if changed:
        if new_storage:
            data["storage"] = new_storage
        if deployment:
            data["deployment"] = deployment
    return changed


def rewrite_json(path: Path) -> tuple[str | None, list[str]]:
    """A JSON config (eval arms): migrated in place, keeping compact or indented layout."""
    import json

    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None, []
    if not isinstance(data, dict) or not migrate_mapping(data):
        return None, []
    match = re.search(r"\n( +)", text)
    if match:
        out = json.dumps(data, indent=len(match.group(1)), ensure_ascii=False)
    else:
        out = json.dumps(data, ensure_ascii=False)
    return out + ("\n" if text.endswith("\n") else ""), []


def rewrite_yaml(path: Path) -> tuple[str | None, list[str]]:
    import yaml

    text = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None, [f"{path}: YAML not parseable"]
    if not isinstance(data, dict) or not migrate_mapping(data):
        return None, []
    out = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    return out, [f"{path}: YAML rewritten (comments dropped; review)"]


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
            if path.suffix in {".py", ".yaml", ".yml", ".json"} and path.is_file():
                yield path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--check", action="store_true", help="report only, write nothing")
    args = parser.parse_args(argv)
    changed: list[Path] = []
    manual: list[str] = []
    for path in iter_files(args.paths):
        if path.resolve() == Path(__file__).resolve():
            continue
        if SKIP_MARKER in path.read_text(encoding="utf-8", errors="replace"):
            continue  # a file that must keep old keys (e.g. the rejection tests)
        if path.suffix == ".py":
            new, notes = rewrite_python(path)
            if new != path.read_text(encoding="utf-8"):
                changed.append(path)
                if not args.check:
                    path.write_text(new, encoding="utf-8", newline="\n")
        else:
            out, notes = rewrite_json(path) if path.suffix == ".json" else rewrite_yaml(path)
            if out is not None:
                changed.append(path)
                if not args.check:
                    path.write_text(out, encoding="utf-8", newline="\n")
        manual.extend(notes)
    for path in changed:
        print(f"changed: {path}")
    for note in manual:
        print(f"manual: {note}")
    print(f"{len(changed)} file(s) {'would change' if args.check else 'changed'}")
    return 1 if args.check and changed else 0


if __name__ == "__main__":
    sys.exit(main())
