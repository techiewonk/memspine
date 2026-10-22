"""Run import-linter on the contracts whose modules exist today (X-02).

``.importlinter`` names the target packages of the restructure (api, runtime, ingest,
derive, read, compose, store, models, trust, memory) before they exist, and
``lint-imports`` fails on a module it cannot find. This script writes a filtered copy
of the file to a temp path and runs ``lint-imports --config <copy>``:

* every module that is not a package or module under ``src/`` is dropped from
  ``layers`` lines, ``source_modules`` and ``forbidden_modules``;
* a layer left empty is dropped; a ``layers`` contract with fewer than two layers left,
  or a ``forbidden`` contract with no source or no forbidden module left, is dropped;
* modules outside the root package (third-party top-level packages such as ``redis``,
  RM-113) are always kept: ``include_external_packages = True`` puts them in the graph;
* every other option is copied verbatim;
* the shrink-only baseline ``tests/unit/baselines/import_contracts.txt`` (RM-113) is turned
  into extra ``ignore_imports`` lines of the contract it names, with
  ``unmatched_ignore_imports_alerting = none``: the AST test
  (tests/unit/test_import_contracts.py) owns the "baseline entry no longer occurs" check.

So a new package is enforced from the commit it appears in, with no edit to CI.

Usage:
    python scripts/ci_importlinter.py            # filter, then run lint-imports
    python scripts/ci_importlinter.py --print    # print the filtered config only
    python scripts/ci_importlinter.py --list     # print kept contract ids only
"""

from __future__ import annotations

import argparse
import configparser
import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
CONFIG = REPO / ".importlinter"
BASELINE = REPO / "tests" / "unit" / "baselines" / "import_contracts.txt"
PREFIX = "importlinter:contract:"
ROOT = "memspine"


def module_exists(dotted: str, src: Path = SRC) -> bool:
    base = src.joinpath(*dotted.split("."))
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


def _keep(dotted: str, src: Path) -> bool:
    """Keep internal modules that exist and every external (third-party) top-level name."""
    if dotted != ROOT and not dotted.startswith(ROOT + "."):
        return True
    return module_exists(dotted, src)


class BaselineEntry(NamedTuple):
    """One temporarily tolerated violation: ``<contract> <file under src/> <imported>``."""

    contract: str
    file: str
    imported: str
    reason: str
    line: int

    @property
    def importer(self) -> str:
        parts = list(Path(self.file).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts)


def load_baseline(path: Path = BASELINE) -> list[BaselineEntry]:
    """Parse the shrink-only baseline.

    Format, one entry per line (``#`` starts a comment line)::

        <contract-id> <file under src/> <imported module> -- <reason / RM-113 work item>
    """
    if not path.is_file():
        return []
    out: list[BaselineEntry] = []
    for no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        body, sep, reason = line.partition(" -- ")
        fields = body.split()
        if not sep or len(fields) != 3 or not reason.strip():
            raise ValueError(f"{path.name}:{no}: expected '<contract> <file> <module> -- <reason>'")
        out.append(BaselineEntry(fields[0], fields[1], fields[2], reason.strip(), no))
    return out


def _baseline_ignores(entries: list[BaselineEntry]) -> dict[str, list[str]]:
    by: dict[str, list[str]] = {}
    for e in entries:
        lines = by.setdefault(e.contract, [])
        lines.append(f"{e.importer} -> {e.imported}")
        if e.imported.startswith(ROOT + "."):  # grimp records the submodule actually imported
            lines.append(f"{e.importer} -> {e.imported}.**")
    return by


def _lines(value: str) -> list[str]:
    return [ln.strip() for ln in value.splitlines() if ln.strip()]


def _filter_layer(line: str, src: Path) -> str | None:
    sep = "|" if "|" in line else ":" if ":" in line else None
    members = [m.strip() for m in line.split(sep)] if sep else [line.strip()]
    kept = [m for m in members if _keep(m, src)]
    if not kept:
        return None
    return f" {sep} ".join(kept)


def _parser() -> configparser.ConfigParser:
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str  # type: ignore[assignment,method-assign]  # keep option case
    return cp


def filter_config(
    path: Path = CONFIG, src: Path = SRC, baseline: Path | None = BASELINE
) -> configparser.ConfigParser:
    """Return a parser holding only the contracts that still make sense today.

    ``baseline`` entries become extra ``ignore_imports`` of their contract (``None`` skips).
    """
    cp = _parser()
    cp.read(path, encoding="utf-8")
    extra = _baseline_ignores(load_baseline(baseline)) if baseline is not None else {}
    out = _parser()
    out.add_section("importlinter")
    for k, v in cp.items("importlinter"):
        out.set("importlinter", k, v)
    for section in cp.sections():
        if not section.startswith(PREFIX):
            continue
        opts = dict(cp.items(section))
        kind = opts["type"]
        if kind == "layers":
            layers = [x for x in (_filter_layer(ln, src) for ln in _lines(opts["layers"])) if x]
            if len(layers) < 2:
                continue
            opts["layers"] = "\n" + "\n".join(f"    {x}" for x in layers)
        elif kind == "forbidden":
            for key in ("source_modules", "forbidden_modules"):
                kept = [m for m in _lines(opts[key]) if _keep(m, src)]
                opts[key] = "\n" + "\n".join(f"    {m}" for m in kept) if kept else ""
            if not opts["source_modules"] or not opts["forbidden_modules"]:
                continue
        else:
            raise ValueError(f"{section}: unsupported contract type {kind!r}")
        cid = section[len(PREFIX) :]
        if extra.get(cid):
            ignores = _lines(opts.get("ignore_imports", "")) + extra[cid]
            opts["ignore_imports"] = "\n" + "\n".join(f"    {x}" for x in ignores)
            opts["unmatched_ignore_imports_alerting"] = "none"
        out.add_section(section)
        for k, v in opts.items():
            out.set(section, k, v)
    return out


def render(cp: configparser.ConfigParser) -> str:
    buf = io.StringIO()
    cp.write(buf)
    return buf.getvalue()


def kept_ids(cp: configparser.ConfigParser) -> list[str]:
    return [s[len(PREFIX) :] for s in cp.sections() if s.startswith(PREFIX)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run lint-imports on existing-module contracts.")
    ap.add_argument("--print", dest="print_only", action="store_true")
    ap.add_argument("--list", dest="list_only", action="store_true")
    args = ap.parse_args(argv)
    cp = filter_config()
    if args.list_only:
        print("\n".join(kept_ids(cp)))
        return 0
    text = render(cp)
    if args.print_only:
        print(text)
        return 0
    fd, tmp = tempfile.mkstemp(suffix=".importlinter")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        ids = kept_ids(cp)
        print(f"ci_importlinter: running {len(ids)} contracts: {', '.join(ids)}", flush=True)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
        exe = shutil.which("lint-imports") or shutil.which(
            "lint-imports", path=str(Path(sys.executable).parent)
        )
        if exe is None:
            print("ci_importlinter: lint-imports not found (install import-linter)", flush=True)
            return 2
        return subprocess.call([exe, "--config", tmp], cwd=REPO, env=env)
    finally:
        os.unlink(tmp)


if __name__ == "__main__":
    sys.exit(main())
