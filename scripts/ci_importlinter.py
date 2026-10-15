"""Run import-linter on the contracts whose modules exist today (X-02).

``.importlinter`` names the target packages of the restructure (api, runtime, ingest,
derive, read, compose, store, models, trust, memory) before they exist, and
``lint-imports`` fails on a module it cannot find. This script writes a filtered copy
of the file to a temp path and runs ``lint-imports --config <copy>``:

* every module that is not a package or module under ``src/`` is dropped from
  ``layers`` lines, ``source_modules`` and ``forbidden_modules``;
* a layer left empty is dropped; a ``layers`` contract with fewer than two layers left,
  or a ``forbidden`` contract with no source or no forbidden module left, is dropped;
* every other option is copied verbatim.

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
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
CONFIG = REPO / ".importlinter"
PREFIX = "importlinter:contract:"


def module_exists(dotted: str, src: Path = SRC) -> bool:
    base = src.joinpath(*dotted.split("."))
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


def _lines(value: str) -> list[str]:
    return [ln.strip() for ln in value.splitlines() if ln.strip()]


def _filter_layer(line: str, src: Path) -> str | None:
    sep = "|" if "|" in line else ":" if ":" in line else None
    members = [m.strip() for m in line.split(sep)] if sep else [line.strip()]
    kept = [m for m in members if module_exists(m, src)]
    if not kept:
        return None
    return f" {sep} ".join(kept)


def _parser() -> configparser.ConfigParser:
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str  # type: ignore[assignment,method-assign]  # keep option case
    return cp


def filter_config(path: Path = CONFIG, src: Path = SRC) -> configparser.ConfigParser:
    """Return a parser holding only the contracts that still make sense today."""
    cp = _parser()
    cp.read(path, encoding="utf-8")
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
                kept = [m for m in _lines(opts[key]) if module_exists(m, src)]
                opts[key] = "\n" + "\n".join(f"    {m}" for m in kept) if kept else ""
            if not opts["source_modules"] or not opts["forbidden_modules"]:
                continue
        else:
            raise ValueError(f"{section}: unsupported contract type {kind!r}")
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
        return subprocess.call(["lint-imports", "--config", tmp], cwd=REPO, env=env)
    finally:
        os.unlink(tmp)


if __name__ == "__main__":
    sys.exit(main())
