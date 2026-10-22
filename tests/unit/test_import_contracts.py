"""AST enforcement of the import-boundary contracts in ``.importlinter`` (P0-04, RM-113).

This test reads the same ``.importlinter`` file with ``configparser`` (one source of
truth) and checks ``src/memspine/**`` with ``ast``. It understands the subset the file
documents: ``layers`` and ``forbidden`` contracts, direct imports only (function-level and
``TYPE_CHECKING`` imports count, as in import-linter), plus each contract's permanent
``ignore_imports`` (``*`` = one module segment, ``**`` = one or more).

External packages (RM-113): imports outside ``memspine`` are recorded as their top-level
package name (``redis.asyncio`` -> ``redis``), as import-linter does with
``include_external_packages = True``. A forbidden-contract violation is reported as
``(file under src/, matched forbidden module)``, so one entry covers every submodule of the
forbidden target that file imports.

Baseline: ``tests/unit/baselines/import_contracts.txt`` lists the violations that exist
today, one per line, ``<contract> <file under src/> <imported> -- <reason>``.
``scripts/ci_importlinter.py`` feeds the same file to ``lint-imports`` as extra
``ignore_imports``. Rules:

* a violation that is not in the baseline fails the test (no new violation);
* a baselined violation that no longer occurs fails the test (delete its line in the
  same PR, so the baseline only shrinks).
"""

from __future__ import annotations

import ast
import configparser
import importlib.util
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
CONFIG = REPO / ".importlinter"


def _ci_script():
    spec = importlib.util.spec_from_file_location(
        "ci_importlinter", REPO / "scripts" / "ci_importlinter.py"
    )
    assert spec
    assert spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_CI = _ci_script()
BASELINE_FILE: Path = _CI.BASELINE


def load_baseline(path: Path = BASELINE_FILE) -> dict[str, set[tuple[str, str]]]:
    out: dict[str, set[tuple[str, str]]] = {}
    for e in _CI.load_baseline(path):
        out.setdefault(e.contract, set()).add((e.file, e.imported))
    return out


# contract id (the part after "importlinter:contract:") -> {(file under src/, imported module)}
BASELINE: dict[str, set[tuple[str, str]]] = load_baseline()


@dataclass(frozen=True)
class Contract:
    cid: str
    kind: str
    # layers: list of layers, top first; each layer is (members, independent)
    layers: tuple[tuple[tuple[str, ...], bool], ...] = ()
    sources: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    ignores: tuple[tuple[re.Pattern[str], re.Pattern[str]], ...] = ()


def _lines(value: str) -> list[str]:
    return [ln.strip() for ln in value.splitlines() if ln.strip()]


def _wildcard(expr: str) -> re.Pattern[str]:
    parts = []
    for seg in expr.strip().split("."):
        if seg == "**":
            parts.append(r"[^.]+(?:\.[^.]+)*")
        elif seg == "*":
            parts.append(r"[^.]+")
        else:
            parts.append(re.escape(seg))
    return re.compile(r"\.".join(parts) + r"\Z")


def _ignores(value: str) -> tuple[tuple[re.Pattern[str], re.Pattern[str]], ...]:
    out = []
    for line in _lines(value):
        a, sep, b = line.partition("->")
        if not sep:
            raise ValueError(f"bad ignore_imports line {line!r}")
        out.append((_wildcard(a), _wildcard(b)))
    return tuple(out)


def load_contracts(path: Path = CONFIG) -> tuple[str, list[Contract]]:
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(path, encoding="utf-8")
    root = cp.get("importlinter", "root_package")
    out: list[Contract] = []
    for section in cp.sections():
        if not section.startswith("importlinter:contract:"):
            continue
        cid = section.split(":", 2)[2]
        kind = cp.get(section, "type")
        ignores = _ignores(cp.get(section, "ignore_imports", fallback=""))
        if kind == "layers":
            layers: list[tuple[tuple[str, ...], bool]] = []
            for line in _lines(cp.get(section, "layers")):
                if "|" in line:
                    layers.append((tuple(p.strip() for p in line.split("|")), True))
                elif ":" in line:
                    layers.append((tuple(p.strip() for p in line.split(":")), False))
                else:
                    layers.append(((line,), False))
            out.append(Contract(cid, kind, layers=tuple(layers), ignores=ignores))
        elif kind == "forbidden":
            out.append(
                Contract(
                    cid,
                    kind,
                    sources=tuple(_lines(cp.get(section, "source_modules"))),
                    forbidden=tuple(_lines(cp.get(section, "forbidden_modules"))),
                    ignores=ignores,
                )
            )
        else:  # fail loudly: the checker must not silently skip a contract type
            raise ValueError(f"{cid}: unsupported contract type {kind!r}")
    return root, out


def _module_name(file: Path, src: Path) -> tuple[str, bool]:
    rel = file.relative_to(src).with_suffix("")
    parts = list(rel.parts)
    is_pkg = parts[-1] == "__init__"
    if is_pkg:
        parts = parts[:-1]
    return ".".join(parts), is_pkg


def collect_imports(src: Path, root: str) -> tuple[set[str], list[tuple[str, str, str]]]:
    """Return (all module names, [(file rel to src, importer module, imported module)]).

    Imports outside ``root`` are recorded as their top-level package name.
    """
    files = sorted((src / root).rglob("*.py"))
    known = {_module_name(f, src)[0] for f in files}
    edges: list[tuple[str, str, str]] = []
    for f in files:
        mod, is_pkg = _module_name(f, src)
        pkg = mod if is_pkg else mod.rpartition(".")[0]
        rel = f.relative_to(src).as_posix()
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = pkg.split(".")
                    base = base[: len(base) - (node.level - 1)]
                    prefix = ".".join(base)
                    module = f"{prefix}.{node.module}" if node.module else prefix
                else:
                    module = node.module or ""
                targets = [module]
                targets += [
                    f"{module}.{a.name}" for a in node.names if f"{module}.{a.name}" in known
                ]
            for t in targets:
                if not t:
                    continue
                if t == root or t.startswith(root + "."):
                    edges.append((rel, mod, t))
                else:
                    edges.append((rel, mod, t.split(".")[0]))
    return known, edges


def _within(module: str, package: str) -> bool:
    return module == package or module.startswith(package + ".")


def _layer_of(module: str, layers) -> tuple[int, str] | None:
    for i, (members, _indep) in enumerate(layers):
        for m in members:
            if _within(module, m):
                return i, m
    return None


def _ignored(contract: Contract, importer: str, imported: str) -> bool:
    return any(a.match(importer) and b.match(imported) for a, b in contract.ignores)


def check(contract: Contract, edges: list[tuple[str, str, str]]) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for rel, importer, imported in edges:
        if _ignored(contract, importer, imported):
            continue
        if contract.kind == "forbidden":
            if not any(_within(importer, s) for s in contract.sources):
                continue
            hits = [f for f in contract.forbidden if _within(imported, f)]
            if hits:
                found.add((rel, max(hits, key=len)))
        else:
            a = _layer_of(importer, contract.layers)
            b = _layer_of(imported, contract.layers)
            if a is None or b is None:
                continue
            (ia, ma), (ib, mb) = a, b
            if ib < ia or (
                ib == ia and ma != mb and contract.layers[ia][1]
            ):  # importing a layer above
                found.add((rel, imported))
    return found


def violations(src: Path = SRC, config: Path = CONFIG) -> dict[str, set[tuple[str, str]]]:
    root, contracts = load_contracts(config)
    _known, edges = collect_imports(src, root)
    return {c.cid: check(c, edges) for c in contracts}


def compare(found: dict[str, set[tuple[str, str]]], baseline: dict[str, set[tuple[str, str]]]):
    new: dict[str, set[tuple[str, str]]] = {}
    gone: dict[str, set[tuple[str, str]]] = {}
    for cid in set(found) | set(baseline):
        f, b = found.get(cid, set()), baseline.get(cid, set())
        if f - b:
            new[cid] = f - b
        if b - f:
            gone[cid] = b - f
    return new, gone


# ---------------------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------------------

RM113_CONTRACTS = {
    "clients-isolated",
    "services-no-transport",
    "services-not-composition",
    "consumers-never-see-clients",
    "providers-composition-only",
}


def test_config_parses_and_has_contracts():
    root, contracts = load_contracts()
    assert root == "memspine"
    ids = {c.cid for c in contracts}
    assert {"target-layers", "schemas-leaf", "store-below-pipelines", "read-not-derive"} <= ids
    assert "engine-only-from-api" in ids
    assert ids >= RM113_CONTRACTS
    assert "v1-clients-low" not in ids  # replaced by clients-isolated


def test_external_packages_enabled():
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(CONFIG, encoding="utf-8")
    assert cp.getboolean("importlinter", "include_external_packages")


def test_baseline_entries_are_well_formed():
    _root, contracts = load_contracts()
    ids = {c.cid for c in contracts}
    seen: set[tuple[str, str, str]] = set()
    for e in _CI.load_baseline(BASELINE_FILE):
        assert e.contract in ids, f"line {e.line}: unknown contract {e.contract!r}"
        assert "RM-113" in e.reason, f"line {e.line}: reason must cite an RM-113 work item"
        key = (e.contract, e.file, e.imported)
        assert key not in seen, f"line {e.line}: duplicate entry {key}"
        seen.add(key)


def test_no_new_import_violations_and_baseline_is_honest():
    new, gone = compare(violations(), BASELINE)
    assert not new, (
        "new import-contract violations (fix the import; never grow the baseline "
        f"{BASELINE_FILE.relative_to(REPO).as_posix()}):\n"
        + "\n".join(f"  {c} {f} {m}" for c, v in sorted(new.items()) for f, m in sorted(v))
    )
    assert not gone, (
        "baselined violations no longer occur; delete these lines from "
        f"{BASELINE_FILE.relative_to(REPO).as_posix()} in this PR:\n"
        + "\n".join(f"  {c} {f} {m}" for c, v in sorted(gone.items()) for f, m in sorted(v))
    )


# --- checker self-tests on a synthetic tree (so the checker itself cannot rot) ----------


def _write(base: Path, rel: str, text: str = "") -> None:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture
def synthetic(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    for pkg in ("api", "read", "derive", "store", "schemas", "trust", "core"):
        _write(src, f"memspine/{pkg}/__init__.py")
    _write(src, "memspine/__init__.py", "from memspine.api.engine import Engine\n")
    _write(src, "memspine/engine.py")
    _write(
        src, "memspine/api/handle.py", "from memspine.read import x\nfrom memspine.store import y\n"
    )
    _write(src, "memspine/read/x.py", "from ..store import y\nfrom ..schemas import z\n")
    _write(src, "memspine/store/y.py", "")
    _write(src, "memspine/schemas/z.py", "")
    return src


def test_checker_passes_clean_tree(synthetic: Path):
    found = violations(synthetic)
    assert all(not v for v in found.values()), found


def test_checker_detects_new_violations(synthetic: Path):
    _write(synthetic, "memspine/store/y.py", "from memspine.read import x\n")  # store -> read
    _write(synthetic, "memspine/read/x.py", "def f():\n    from memspine.derive import d\n")  # lazy
    _write(synthetic, "memspine/schemas/z.py", "import memspine.store.y\n")  # schemas not leaf
    _write(synthetic, "memspine/core/c.py", "from memspine.api.engine import Engine\n")
    _write(synthetic, "memspine/derive/d.py", "from memspine.read import x\n")  # sibling layer, ok
    found = violations(synthetic)
    assert ("memspine/store/y.py", "memspine.read") in found["store-below-pipelines"]
    assert ("memspine/read/x.py", "memspine.derive") in found["read-not-derive"]
    assert found["schemas-leaf"], "schemas importing store must be flagged"
    assert ("memspine/core/c.py", "memspine.api.engine") in found["engine-only-from-api"]
    assert found["target-layers"], "store importing read goes up a layer"


def test_checker_handles_external_packages_and_ignores(tmp_path: Path):
    src = tmp_path / "src"
    _write(src, "memspine/__init__.py")
    _write(src, "memspine/services/__init__.py")
    _write(src, "memspine/services/a.py", "import redis.asyncio\nfrom httpx import Client\n")
    _write(src, "memspine/services/x/alembic/env.py", "import redis\n")
    _write(src, "memspine/services/b.py", "def f():\n    import json, httpx\n")
    cfg = tmp_path / ".importlinter"
    cfg.write_text(
        "[importlinter]\nroot_package = memspine\ninclude_external_packages = True\n\n"
        "[importlinter:contract:no-transport]\nname = n\ntype = forbidden\n"
        "source_modules =\n    memspine.services\n"
        "forbidden_modules =\n    redis\n    httpx\n"
        "ignore_imports =\n    memspine.services.*.alembic.env -> *\n",
        encoding="utf-8",
    )
    found = violations(src, cfg)["no-transport"]
    assert found == {
        ("memspine/services/a.py", "redis"),
        ("memspine/services/a.py", "httpx"),
        ("memspine/services/b.py", "httpx"),
    }


def test_wildcards():
    assert _wildcard("a.*.c").match("a.b.c")
    assert not _wildcard("a.*.c").match("a.b.b.c")
    assert _wildcard("a.**").match("a.b.c")
    assert not _wildcard("a.**").match("a")
    assert _wildcard("*").match("redis")
    assert not _wildcard("*").match("memspine.x")


def test_baseline_comparison_flags_new_and_vanished():
    found = {"c1": {("a.py", "m.x")}}
    new, gone = compare(found, {})
    assert new == {"c1": {("a.py", "m.x")}} and not gone
    new, gone = compare({}, {"c1": {("a.py", "m.x")}})
    assert gone == {"c1": {("a.py", "m.x")}} and not new
    new, gone = compare(found, found)
    assert not new and not gone
