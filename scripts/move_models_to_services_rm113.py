"""RM-113 P2: move model ports and adapters from ``memspine.models`` to ``memspine.services``.

Usage::

    python scripts/move_models_to_services_rm113.py [--dry-run]

Owner decision 2026-10-23 (RM-113): compute adapters live under ``services/<slot>/`` like every
other slot, weight sharing moves to ``clients/weights``, and there are no aliases or re-export
shims (ADR-068). The move reuses ``scripts/codemod_move.py`` (``git mv`` plus the AST-guided
rewrite of every reference in ``src``, ``tests``, ``evals``, ``scripts`` and ``pyproject.toml``)
in two phases:

1. whole packages move, file names unchanged (``models/providers/llm`` -> ``services/llm``, ...);
2. modules inside them are renamed to the ``services/<slot>/<provider>.py`` convention, and the
   single modules (GLiNER runtime, escalation, shared weights, concurrency, embedding profiles,
   the second rerank factory) move to their homes.

Then the moved tests follow their sources, the emptied ``models`` sub-packages are deleted, and
``docs/**/*.md`` (which ``codemod_move.py`` never scans) is rewritten for both dotted names and
``models/...`` paths. What stays in ``memspine.models`` is pure model metadata (``profiles``,
``embedding_profiles``, ``tiers``, ``resolve``) and the rule-based query analysis
(``encoders/query``, no model; ``core`` depends on it and may not import ``services``).

Idempotent: a second run moves nothing and rewrites nothing.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import codemod_move as cm

# This script names the old modules on purpose: never rewrite it.
cm.SKIP_PREFIX = (*cm.SKIP_PREFIX, "scripts/move_models_to_services_rm113.py")

#: Phase 1: whole packages, file names unchanged.
PACKAGES: dict[str, str] = {
    "memspine.models.encoders.embedding": "memspine.services.embedding",
    "memspine.models.providers.llm": "memspine.services.llm",
    "memspine.models.providers.rerank": "memspine.services.rerank",
    "memspine.models.providers.decision": "memspine.services.decider",
    "memspine.models.cache": "memspine.services.cache",
}

#: Phase 2: module renames inside the moved packages and the single-module moves.
MODULES: dict[str, str] = {
    # embedding: services/embedding/<provider name>.py
    "memspine.services.embedding.fastembed_local": "memspine.services.embedding.fastembed",
    "memspine.services.embedding.hash_local": "memspine.services.embedding.hash",
    "memspine.services.embedding.litellm_embed": "memspine.services.embedding.litellm",
    "memspine.services.embedding.st_local": "memspine.services.embedding.st",
    "memspine.services.embedding.static_local": "memspine.services.embedding.static",
    # embedding profiles are pure model metadata: they stay in models
    "memspine.services.embedding.profiles": "memspine.models.embedding_profiles",
    # llm
    "memspine.services.llm.litellm_llm": "memspine.services.llm.litellm",
    "memspine.services.llm.llama_cpp": "memspine.services.llm.llamacpp",
    "memspine.models.runtime.escalation": "memspine.services.llm.escalation",
    # rerank (the second factory collapses into the descriptor module)
    "memspine.services.rerank.fastembed_rerank": "memspine.services.rerank.fastembed",
    "memspine.services.rerank.flashrank_rerank": "memspine.services.rerank.flashrank",
    "memspine.services.rerank.jina_rerank": "memspine.services.rerank.jina",
    "memspine.services.rerank.litellm_rerank": "memspine.services.rerank.litellm",
    "memspine.services.rerank.qwen3_rerank": "memspine.services.rerank.qwen3",
    "memspine.services.rerank.factory": "memspine.providers.builtin.rerank",
    # decider (owner naming 2026-10-23: the slot is model.decider, not "decision")
    "memspine.services.decider.gliner2_decision": "memspine.services.decider.gliner2",
    # the OpenDecider-nano forward-pass port and its snapshot loader are the weights library
    "memspine.services.decider.opendecider_nano": "memspine.clients.weights.opendecider",
    # cache adapters
    "memspine.services.cache.cashews_cache": "memspine.services.cache.cashews",
    # extraction (GLiNER) and the cross-cutting pieces
    "memspine.models.runtime.gliner_runtime": "memspine.services.extraction.gliner2",
    "memspine.models.runtime.shared_weights": "memspine.clients.weights.shared",
    "memspine.models.concurrency": "memspine.services.concurrency",
}

#: Tests follow their sources (tests mirror src 1:1).
TEST_MOVES: dict[str, str] = {
    "tests/unit/models/runtime/test_gliner_runtime.py": "tests/unit/services/extraction/test_gliner2.py",
    "tests/unit/models/test_escalation.py": "tests/unit/services/llm/test_escalation.py",
    "tests/unit/services/decision": "tests/unit/services/decider",
}

#: Packages left empty by the moves (only their ``__init__.py`` remains).
EMPTIED = ("src/memspine/models/runtime", "src/memspine/models/providers")


def _compose(first: dict[str, str], second: dict[str, str]) -> dict[str, str]:
    """Old dotted name -> final dotted name, for the docs (they see both phases at once)."""
    out: dict[str, str] = {}
    for old, mid in first.items():
        out[old] = mid
    for mid, new in second.items():
        origin = next((o for o, m in first.items() if mid.startswith(m + ".")), None)
        if origin is not None:
            out[origin + mid[len(first[origin]) :]] = new
        else:
            out[mid] = new
    return out


def _phase(moves: dict[str, str], dry: bool) -> list[str]:
    report: list[str] = []
    moved_now: dict[str, str] = {}
    for src, dst in moves.items():
        if cm.git_mv(ROOT, src, dst, dry, report):
            moved_now[src] = dst
    touched = cm.process(ROOT, moves, moved_now, dry, set())
    report.extend(f"rewrote {p.relative_to(ROOT).as_posix()}" for p in touched)
    return report


def _move_tests(dry: bool) -> list[str]:
    report: list[str] = []
    for src, dst in TEST_MOVES.items():
        s, d = ROOT / src, ROOT / dst
        if not s.exists():
            continue
        if not dry:
            d.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "mv", str(s), str(d)], cwd=ROOT, check=True)
        report.append(f"moved {src} -> {dst}")
    return report


def _drop_emptied(dry: bool) -> list[str]:
    report: list[str] = []
    for rel in EMPTIED:
        pkg = ROOT / rel
        if not pkg.is_dir():
            continue
        left = [p for p in pkg.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
        if all(p.name == "__init__.py" for p in left):
            if not dry:
                subprocess.run(["git", "rm", "-q", "-r", str(pkg)], cwd=ROOT, check=True)
            report.append(f"removed empty package {rel}")
    return report


def _path_forms(moves: dict[str, str]) -> list[tuple[re.Pattern[str], str]]:
    """``memspine/models/x/y.py`` and bare ``models/x/y.py`` (or ``models/x/``) path forms."""
    rules: list[tuple[re.Pattern[str], str]] = []
    for old in sorted(moves, key=len, reverse=True):
        new = moves[old]
        o = old.removeprefix("memspine.").replace(".", "/")
        n = new.removeprefix("memspine.").replace(".", "/")
        for suffix in (".py", "/"):
            pattern = re.compile(r"(?<![\w.\-])((?:src/)?(?:memspine/)?)" + re.escape(o + suffix))
            rules.append((pattern, r"\g<1>" + n.replace("\\", "/") + suffix))
    return rules


def _rewrite_docs(moves: dict[str, str], dry: bool) -> list[str]:
    dotted = cm.build_dotted_regex(moves)
    paths = _path_forms(moves)
    report: list[str] = []
    for doc in sorted((ROOT / "docs").rglob("*.md")):
        text = doc.read_bytes().decode("utf-8")
        new = dotted.sub(lambda m: moves[m.group(1)], text)
        for pattern, repl in paths:
            new = pattern.sub(repl, new)
        if new != text:
            report.append(f"rewrote {doc.relative_to(ROOT).as_posix()}")
            if not dry:
                doc.write_bytes(new.encode("utf-8"))
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    report = _phase(PACKAGES, args.dry_run)
    report += _phase(MODULES, args.dry_run)
    report += _move_tests(args.dry_run)
    report += _drop_emptied(args.dry_run)
    report += _rewrite_docs(_compose(PACKAGES, MODULES), args.dry_run)
    print("\n".join(report))
    print(f"{len(report)} actions" + (" (dry run)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
