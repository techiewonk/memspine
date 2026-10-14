"""Licence guard: no benchmark dataset text in tracked files.

Benchmark datasets (LoCoMo, LongMemEval, OP-Bench, ...) may be used locally but their TEXT must
not be committed; only ids, counts and labels may. Two checks:

* always on: no tracked archive or columnar data file (``*.zip``, ``*.parquet``, ...) under
  ``docs/`` or ``evals/``;
* when a local dataset is present: no tracked text file contains a run of 30 or more characters
  (compared on lower-cased letters and digits only, so whitespace, punctuation and JSON escaping
  do not hide a copy) that also occurs in a dataset string. Skipped when no dataset is on disk
  (CI).

Datasets are looked up in ``<repo>/data`` and ``<repo>/evals/data``, plus any directories in
``MEMSPINE_TEST_DATASET_DIRS`` (``os.pathsep``-separated). Third-party text that shares
phrases with benchmark few-shot material is listed in ``ALLOW``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WINDOW = 30  # characters of the normalised text that count as a copy
INDEX_WINDOW = 24  # indexed window; with stride 6 every common run of >= 30 contains one
STRIDE = 6
MAX_DATASET_FILE = 400 * 1024 * 1024
MAX_SCAN_FILE = 8 * 1024 * 1024

#: Tracked files exempt from the text scan (third-party text, not benchmark data).
ALLOW = frozenset(
    {
        "docs/ECOSYSTEM_PROMPTS.md",  # verbatim catalogue of other engines' prompts
        "evals/memspine_evals/vendor_judges.py",  # Mem0 judge prompt
        "tests/unit/core/test_firewall_signals.py",  # public ASB attack-template phrases
        # generated HTML of source listings from the old LoCoMo eval bundle; clean at 40 characters,
        # at 30 it only shares code fragments (imports, boilerplate) with the dataset
        "docs/combined-memory-locomo-eval-v4/docs/COMBINED_MEMORY_ENGINE_AND_LOCOMO.html",
        "tests/unit/test_no_dataset_text.py",
    }
)
#: Normalised (lower-case letters and digits) fragments that are generic language or code, not
#: benchmark content: such a fragment is cut out of the line before the comparison. Keep this list to
#: weekday lists, import lines, licence boilerplate and public titles; never add a sentence that
#: could come from a conversation.
GENERIC = (
    "mondaytuesdaywednesday",
    "tuesdaywednesdaythursday",
    "wednesdaythursdayfriday",
    "thursdayfridaysaturday",
    "fridaysaturdaysunday",
    "importdatetime",
    "authorizationbearer",
    "reproductionanddistribution",
    "naturallanguageprocessing",
    "informationprovided",
    "relationshipbetweentheen",
    "conflictresolutio",
    "taskorienteddialogue",
    "currentfactsonthis",
    "fromsklearnlinearmodelimport",
    "headersauthorizationfbearer",
    "weatherforecastfortomorrow",
)
BINARY_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".parquet", ".pyc", ".woff",
     ".woff2", ".ttf", ".whl", ".npy", ".npz", ".db", ".sqlite", ".bin", ".pt", ".safetensors"}
)
FORBIDDEN_SUFFIXES = (".zip", ".parquet", ".tar", ".tgz", ".tar.gz", ".7z", ".rar")

_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def _norm(text: str) -> str:
    return _NON_ALNUM.sub("", text.lower())


def _tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode("utf-8", "replace")
    return [p for p in out.split("\0") if p and (ROOT / p).is_file()]


def test_no_tracked_archives_or_parquet_under_docs_or_evals() -> None:
    bad = [
        p
        for p in _tracked()
        if p.startswith(("docs/", "evals/")) and p.lower().endswith(FORBIDDEN_SUFFIXES)
    ]
    assert not bad, f"archives / parquet must not be tracked (dataset content risk): {bad}"


# ---- dataset scan -----------------------------------------------------------------------------


def _dataset_dirs() -> list[Path]:
    dirs = [ROOT / "data", ROOT / "evals" / "data"]
    extra = os.environ.get("MEMSPINE_TEST_DATASET_DIRS", "")
    dirs += [Path(p) for p in extra.split(os.pathsep) if p]
    return [d for d in dirs if d.is_dir()]


def _strings(node: object, out: list[str]) -> None:
    if isinstance(node, str):
        if len(node) >= WINDOW:
            out.append(node)
    elif isinstance(node, list):
        for x in node:
            _strings(x, out)
    elif isinstance(node, dict):
        for x in node.values():
            _strings(x, out)


def _dataset_strings(dirs: list[Path]) -> list[str]:
    found: list[str] = []
    for d in dirs:
        for path in d.rglob("*"):
            if not path.is_file() or path.stat().st_size > MAX_DATASET_FILE:
                continue
            try:
                if path.suffix == ".json":
                    _strings(json.loads(path.read_text(encoding="utf-8")), found)
                elif path.suffix == ".jsonl":
                    for line in path.read_text(encoding="utf-8").splitlines():
                        if line.strip():
                            _strings(json.loads(line), found)
            except (ValueError, OSError, UnicodeDecodeError):
                continue
    return found


def _build_index(strings: list[str]) -> tuple[set[str], str]:
    windows: set[str] = set()
    pieces: list[str] = []
    for s in strings:
        n = _norm(s)
        if len(n) < WINDOW:
            continue
        pieces.append(n)
        for i in range(0, len(n) - INDEX_WINDOW + 1, STRIDE):
            windows.add(n[i : i + INDEX_WINDOW])
    return windows, "\x00".join(pieces)


def scan_text(text: str, windows: set[str], corpus: str) -> list[int]:
    """1-based line numbers of ``text`` that share a >= 30-character normalised run."""
    hits: list[int] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        n = _norm(line)
        for g in GENERIC:
            n = n.replace(g, "#")  # generic text cannot be part of a copied run
        if len(n) < WINDOW:
            continue
        for i in range(0, len(n) - INDEX_WINDOW + 1):
            if n[i : i + INDEX_WINDOW] in windows:
                lo = max(0, i - (WINDOW - INDEX_WINDOW))
                # a real >= 30 run: some 30-char slice around the indexed window is in the corpus
                if any(
                    n[j : j + WINDOW] in corpus
                    for j in range(lo, min(i + 1, len(n) - WINDOW + 1))
                ):
                    hits.append(lineno)
                    break
    return hits


def test_scan_text_detects_a_planted_copy() -> None:
    sample = "Ilse said that the harbour lights over Tomasz boat were unusually bright last night."
    windows, corpus = _build_index([sample])
    assert scan_text(f"note: {sample.upper()}\nother", windows, corpus) == [1]
    other = "Bram bought two loaves of rye bread and a jar of plum jam on Sunday."
    assert scan_text(other, windows, corpus) == []


def test_no_dataset_text_in_tracked_files() -> None:
    dirs = _dataset_dirs()
    if not dirs:
        pytest.skip("no local benchmark dataset on disk (CI): text scan not applicable")
    strings = _dataset_strings(dirs)
    if not strings:
        pytest.skip("local dataset directories hold no readable JSON strings")
    windows, corpus = _build_index(strings)
    offenders: list[str] = []
    for rel in _tracked():
        if rel in ALLOW or Path(rel).suffix.lower() in BINARY_SUFFIXES:
            continue
        path = ROOT / rel
        if path.stat().st_size > MAX_SCAN_FILE:
            continue
        raw = path.read_bytes()
        if b"\x00" in raw[:4096]:
            continue
        for lineno in scan_text(raw.decode("utf-8", "replace"), windows, corpus):
            offenders.append(f"{rel}:{lineno}")
    assert not offenders, (
        "benchmark dataset text found in tracked files (replace with ids / invented examples): "
        + ", ".join(offenders[:60])
        + (f" ... (+{len(offenders) - 60} more)" if len(offenders) > 60 else "")
    )
