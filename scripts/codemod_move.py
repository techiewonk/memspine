# ruff: noqa: E501
"""Move modules and rewrite every reference (restructure codemod, reused by each move package).

Usage:
    python scripts/codemod_move.py scripts/moves/F-05.toml [--dry-run] [--root DIR]

The move list is TOML::

    [[move]]
    from = "memspine.core.records"
    to = "memspine.store.records"

For each move the module file is ``git mv``-ed (``<pkg>/__init__.py`` packages are not supported, only
single-file modules), then every code file under ``src/``, ``tests/``, ``evals/`` and ``scripts/`` (plus the
root ``pyproject.toml``) is rewritten:

* dotted references (imports, ``mock.patch`` targets, importlib strings, entry points, YAML class paths,
  logger names) are rewritten textually, matching on module boundaries (``memspine.core.records`` does not
  match ``memspine.core.records_x``);
* ``from memspine.core import records`` becomes ``from memspine.store import records`` (``as <old leaf>`` is
  added when the leaf name changes, so the bound name is unchanged);
* relative imports are resolved to absolute module names and rewritten when they hit a moved module; every
  relative import inside a moved file is made absolute.

Only ``.py .pyi .toml .ini .cfg .yaml .yml`` files are scanned. ``.importlinter``, ``uv.lock`` and
``scripts/moves/`` are never touched (override with ``--also-touch``); ``docs/`` is never scanned.
The script is idempotent: a second run changes nothing. Untouched code is never reformatted.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
import tomllib
import warnings
from pathlib import Path

SCAN_DIRS = ("src", "tests", "evals", "scripts")
SCAN_ROOT_FILES = ("pyproject.toml",)
SUFFIXES = {".py", ".pyi", ".toml", ".ini", ".cfg", ".yaml", ".yml"}
SKIP_PARTS = {".venv", "__pycache__", "node_modules", ".git", "runs", ".pytest_cache"}
NEVER_TOUCH = {"uv.lock", ".importlinter"}
SKIP_PREFIX = ("scripts/moves/", "scripts/codemod_move.py")


def load_moves(path: Path) -> dict[str, str]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    moves = {m["from"]: m["to"] for m in data.get("move", [])}
    if not moves:
        raise SystemExit(f"no [[move]] entries in {path}")
    return moves


def module_path(root: Path, mod: str) -> Path:
    return root / "src" / Path(*mod.split(".")).with_suffix(".py")


def module_of_file(root: Path, file: Path) -> tuple[str, bool] | None:
    """Return (module name, is_package) for a file under src/, else None."""
    try:
        rel = file.relative_to(root / "src")
    except ValueError:
        return None
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        return ".".join(parts[:-1]), True
    return ".".join(parts), False


def resolve_relative(cur_module: str, is_pkg: bool, level: int, module: str | None) -> str:
    pkg = cur_module.split(".") if is_pkg else cur_module.split(".")[:-1]
    if level > 1:
        pkg = pkg[: len(pkg) - (level - 1)]
    return ".".join(pkg + ([module] if module else []))


def build_dotted_regex(moves: dict[str, str]) -> re.Pattern[str]:
    names = sorted(moves, key=len, reverse=True)
    return re.compile(r"(?<![\w.])(" + "|".join(re.escape(n) for n in names) + r")(?!\w)")


def git_mv(root: Path, src: str, dst: str, dry: bool, report: list[str]) -> bool:
    s, d = module_path(root, src), module_path(root, dst)
    if not s.exists():
        if d.exists():
            return False  # already moved (idempotent re-run)
        raise SystemExit(f"neither {s} nor {d} exists")
    if d.exists():
        raise SystemExit(f"target already exists: {d}")
    # make sure every package directory on the way exists as a package
    for parent in reversed(d.relative_to(root / "src").parents):
        pdir = root / "src" / parent
        if str(parent) in (".", ""):
            continue
        init = pdir / "__init__.py"
        if not init.exists() and not dry:
            pdir.mkdir(parents=True, exist_ok=True)
            init.write_text('"""Package skeleton created by codemod_move.py."""\n', encoding="utf-8")
            subprocess.run(["git", "add", str(init)], cwd=root, check=True)
            report.append(f"created {init.relative_to(root).as_posix()}")
    if not dry:
        d.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "mv", str(s), str(d)], cwd=root, check=True)
    report.append(f"moved {s.relative_to(root).as_posix()} -> {d.relative_to(root).as_posix()}")
    return True


def iter_files(root: Path, also: set[str]):
    cands: list[Path] = [root / f for f in SCAN_ROOT_FILES if (root / f).exists()]
    for d in SCAN_DIRS:
        base = root / d
        if base.exists():
            cands.extend(p for p in base.rglob("*") if p.is_file())
    for p in cands:
        rel = p.relative_to(root).as_posix()
        if p.suffix not in SUFFIXES or SKIP_PARTS & set(p.relative_to(root).parts):
            continue
        if p.name in NEVER_TOUCH and rel not in also:
            continue
        if rel.startswith(SKIP_PREFIX) and rel not in also:
            continue
        yield p


def rewrite_imports(text: str, cur: tuple[str, bool] | None, old_name: tuple[str, bool] | None,
                    moves: dict[str, str]) -> str:
    """AST-guided rewrite of ``from X import Y`` forms the dotted regex cannot reach."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except SyntaxError:
        return text
    # for relative-import resolution use the pre-move location when the file moved in this run
    ctx = old_name or cur
    lines = text.splitlines(keepends=True)
    offs = [0]
    for ln in lines:
        offs.append(offs[-1] + len(ln.encode("utf-8")))
    raw = text.encode("utf-8")
    edits: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            if ctx is None:
                continue
            base = resolve_relative(ctx[0], ctx[1], node.level, node.module)
        else:
            base = node.module or ""
        if not base:
            continue
        # group aliases by target (module, bound-name rewrite)
        renamed = False
        new_aliases: list[tuple[str, str, str | None]] = []  # (target module, name, asname)
        for a in node.names:
            full = f"{base}.{a.name}"
            if a.name != "*" and full in moves:
                tgt_mod, _, tgt_leaf = moves[full].rpartition(".")
                asname = a.asname
                if tgt_leaf != a.name and asname is None:
                    asname = a.name
                    renamed = True
                if tgt_leaf != a.name and a.asname is not None:
                    renamed = True
                new_aliases.append((tgt_mod, tgt_leaf, asname))
            else:
                tgt_mod = moves.get(base, base)
                new_aliases.append((tgt_mod, a.name, a.asname))
        targets = {t for t, _, _ in new_aliases}
        moved_any = any(
            f"{base}.{a.name}" in moves for a in node.names
        ) or base in moves
        if not node.level and not moved_any:
            continue
        if node.level and not moved_any and not (old_name is not None):
            # relative import in an unmoved file that hits nothing: leave as is
            continue
        start = offs[node.lineno - 1] + len(lines[node.lineno - 1].encode("utf-8")[: node.col_offset])
        end = offs[node.end_lineno - 1] + node.end_col_offset
        seg = raw[start:end].decode("utf-8")
        indent_line = lines[node.lineno - 1]
        indent = indent_line[: len(indent_line) - len(indent_line.lstrip())]
        if len(targets) == 1 and not renamed:
            tgt = next(iter(targets))
            new = re.sub(r"^from\s+\S+\s+import", f"from {tgt} import", seg, count=1)
        else:
            order: list[str] = []
            by: dict[str, list[str]] = {}
            for t, n, asn in new_aliases:
                if t not in by:
                    by[t] = []
                    order.append(t)
                by[t].append(n if asn is None or asn == n else f"{n} as {asn}")
            new = f"\n{indent}".join(f"from {t} import {', '.join(by[t])}" for t in order)
        if new != seg:
            edits.append((start, end, new))
    for s, e, new in sorted(edits, reverse=True):
        raw = raw[:s] + new.encode("utf-8") + raw[e:]
    return raw.decode("utf-8")


def process(root: Path, moves: dict[str, str], moved_now: dict[str, str], dry: bool, also: set[str]):
    rx = build_dotted_regex(moves)
    new_to_old = {v: k for k, v in moved_now.items()}
    touched: list[Path] = []
    for p in iter_files(root, also):
        orig = p.read_bytes().decode("utf-8")
        cur = module_of_file(root, p) if p.suffix in {".py", ".pyi"} else None
        old = None
        if cur and cur[0] in new_to_old:
            old = (new_to_old[cur[0]], cur[1])
        text = rx.sub(lambda m: moves[m.group(1)], orig)
        if p.suffix in {".py", ".pyi"}:
            text = rewrite_imports(text, cur, old, moves)
            if old is not None:
                text = absolutise_relative(text, old)
        if text != orig:
            touched.append(p)
            if not dry:
                p.write_bytes(text.encode("utf-8"))
    return touched


def absolutise_relative(text: str, ctx: tuple[str, bool]) -> str:
    """Rewrite remaining relative imports in a moved file to absolute names (resolved from the OLD location)."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except SyntaxError:
        return text
    lines = text.splitlines(keepends=True)
    offs = [0]
    for ln in lines:
        offs.append(offs[-1] + len(ln.encode("utf-8")))
    raw = text.encode("utf-8")
    edits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            base = resolve_relative(ctx[0], ctx[1], node.level, node.module)
            start = offs[node.lineno - 1] + len(lines[node.lineno - 1].encode("utf-8")[: node.col_offset])
            end = offs[node.end_lineno - 1] + node.end_col_offset
            seg = raw[start:end].decode("utf-8")
            new = re.sub(r"^from\s+\S+\s+import", f"from {base} import", seg, count=1)
            edits.append((start, end, new))
    for s, e, new in sorted(edits, reverse=True):
        raw = raw[:s] + new.encode("utf-8") + raw[e:]
    return raw.decode("utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("moves", type=Path)
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--also-touch", action="append", default=[], help="repo-relative path normally skipped")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    moves = load_moves(args.moves)
    report: list[str] = []
    moved_now: dict[str, str] = {}
    for src, dst in moves.items():
        if git_mv(root, src, dst, args.dry_run, report):
            moved_now[src] = dst
    touched = process(root, moves, moved_now, args.dry_run, set(args.also_touch))
    for p in touched:
        report.append(f"rewrote {p.relative_to(root).as_posix()}")
    print("\n".join(report))
    print(f"{len(moved_now)} moved, {len(touched)} rewritten" + (" (dry run)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
