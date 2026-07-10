#!/usr/bin/env python3
"""Rebuild ECOSYSTEM_ALGORITHMS / PACKAGE_GAPS / PACKAGE_ADOPTION CSVs from design docs."""

from __future__ import annotations

import csv
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXPORTS = ROOT / "docs" / "exports"
CATALOG = ROOT / "docs" / "PACKAGE_CATALOG.md"
DEP_ANALYSIS = ROOT / "docs" / "DEPENDENCY_ANALYSIS.md"

# memspine decisions from structure plan / DEPENDENCY_ANALYSIS §0 + cross-ref table
ADOPTED = {
    "lancedb": ("D-09 vector", "EverMemOS, cognee, honcho, SimpleMem"),
    "fastembed": ("D-08 embedder", "mem0, cognee"),
    "onnxruntime": ("D-08 embedder", "cognee"),
    "kuzu": ("D-26 graph alt", "graphiti, cognee"),
    "rank-bm25": ("D-25 lexical", "A-mem, powermem, SimpleMem, MemOS, ReMe"),
    "rank_bm25": ("D-25 lexical", "A-mem, powermem, SimpleMem, MemOS, ReMe"),
    "datasketch": ("D-27 dedup", "MemOS"),
    "gliner2": ("D-28 NER", "graphiti"),
    "chonkie": ("D-29 ingest", "MemOS"),
    "markitdown": ("D-29 ingest", "MemOS, hindsight"),
    "instructor": ("D-31 structured", "cognee, MemMachine, honcho"),
    "json-repair": ("D-31 structured", "honcho, MemoryBear"),
    "zstandard": ("D-32 cold compress", "MemOS, ReMe, SimpleMem"),
    "llmlingua": ("E5 compress", "LightMem, SimpleMem"),
    "litellm": ("D-33 llm gateway", "A-mem, mem0, cognee, hindsight, telemem"),
    "structlog": ("observability", "EverMemOS, cognee"),
    "pydantic-settings": ("config", "powermem, EverMemOS, cognee"),
    "langfuse": ("observability", "honcho, cognee"),
    "prometheus-client": ("observability", "EverMemOS, MemOS, honcho"),
    "xxhash": ("D-37 hashing", "MemoryBear"),
    "graspologic": ("D-40 communities", "MemoryBear"),
    "python-frontmatter": ("file-native", "ReMe, EverMemOS"),
    "mistletoe": ("file-native", "ReMe"),
    "watchfiles": ("file-native", "ReMe, EverMemOS"),
    "fakeredis": ("D-41 test double", "cognee"),
    "sqlalchemy": ("D-36 storage", "memspine core"),
    "alembic": ("D-36 migrations", "memspine core"),
    "aiosqlite": ("D-44 async storage", "memspine core"),
}

REJECTED = {
    "celery": "D-16 workers — MemoryBear uses; memspine prefers DBOS/taskiq/inline",
    "jieba": "D-34 reversed — CJK not v0.1 goal",
    "rjieba": "D-34 reversed — CJK not v0.1 goal",
    "nltk": "core reject — heavy corpora; FTS5/Tantivy suffice",
    "rake-nltk": "reject — tags from NER/LLM",
    "cn2an": "D-34 CJK reject",
    "xpinyin": "D-34 CJK reject",
    "hanziconv": "D-34 CJK reject",
    "igraph": "not adopted",
    "mmh3": "superseded by xxhash D-37",
    "sentence-transformers": "heavy torch default; fastembed D-08 instead",
    "neo4j": "server graph — embedded default sqlite_adjacency/kuzu D-26",
    "chromadb": "vector alt stub only",
    "faiss-cpu": "in-proc index alt; lancedb D-09 default",
}

ALGORITHMS = [
    ("hybrid_retrieval", "RRF fusion k=60 + optional MMR/cross-encoder rerank", "graphiti, powermem, SimpleMem", "memspine read.hybrid D-53"),
    ("minhash_lsh_dedup", "datasketch MinHash-LSH stage-1 + cosine confirm", "MemOS pref-mem", "D-27 policies/dedup.py"),
    ("ebbinghaus_decay", "strength × time decay dormancy→decay→clearance", "MemoryBear, powermem", "M6 lifecycle"),
    ("bitemporal_graph", "valid_at/invalid_at on edges and nodes", "graphiti", "substrate bitemporal fields"),
    ("hierarchical_leiden", "graspologic community → summary parent", "MemoryBear graphrag", "D-40 optional [community]"),
    ("add_update_delete_none", "LLM infer terminal state NONE", "mem0 v2", "policies/infer.py"),
    ("zstd_cold_tier", "zstandard compress low-salience memories", "MemOS, ReMe, SimpleMem", "D-32 compression.py"),
    ("llmlingua_compress", "prompt/context compression ~20×", "LightMem", "E5 [compress]"),
    ("instructor_structured", "pydantic-validated LLM JSON + retries", "cognee, langmem", "D-31 services/llm"),
    ("gliner2_ner", "local zero-shot entity extraction CPU", "graphiti [gliner2]", "D-28 entities.py"),
    ("markitdown_chonkie_ingest", "multi-format → markdown → chunk", "MemOS mem-reader", "D-29 extraction.py"),
    ("hmm_me_alignment", "hierarchical memory + personal alignment", "Second-Me", "research — not core"),
    ("redis_streams_scheduler", "per-scope queue isolation async ingest", "MemOS MemScheduler", "D-42 workers pattern"),
    ("graph_reorganizer", "background merge/summary parent nodes", "MemOS tree_text_memory", "D-42 associative consolidate"),
    ("trust_quarantine", "write-path trust scoring + quarantine tier", "memspine E1", "OWASP ASI06 — novel vs surveyed"),
    ("file_native_profile", "frontmatter + mistletoe + watchfiles SoT", "ReMe, EverMemOS", "optional profile"),
    ("evermemos_vendored_algo", "everalgo-* closed wheels", "EverMemOS", "not inspectable — gap"),
]


def parse_repo_packages(catalog_text: str) -> dict[str, list[str]]:
    repos: dict[str, list[str]] = {}
    current: str | None = None
    for line in catalog_text.splitlines():
        m = re.match(r"^### (.+?) \(", line)
        if m:
            current = m.group(1).strip()
            repos[current] = []
            continue
        if current and line.startswith("**Design"):
            pkgs = re.split(r"[·,]", line.split(":", 1)[-1])
            for p in pkgs:
                p = p.strip().strip("*").split()[0] if p.strip() else ""
                if p and p not in {"Design", "⭐"}:
                    repos[current].append(p.lower())
    return repos


def write_algorithms(path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["algorithm_id", "description", "ecosystem_proof", "memspine_slot"])
        for row in ALGORITHMS:
            w.writerow(row)
    print(f"Wrote {path.name} ({len(ALGORITHMS)} rows)")


def write_adoption(path: Path) -> None:
    rows = []
    for pkg, (slot, proof) in sorted(ADOPTED.items()):
        rows.append([pkg, "adopted", slot, proof, "memspine default or extra"])
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["package", "status", "memspine_decision", "ecosystem_proof", "notes"])
        w.writerows(rows)
    print(f"Wrote {path.name} ({len(rows)} rows)")


def write_gaps(path: Path, catalog_text: str) -> None:
    repos = parse_repo_packages(catalog_text)
    memspine_pkgs = {p.lower() for p in ADOPTED}
    rows: list[list[str]] = []
    for repo, pkgs in sorted(repos.items()):
        for pkg in pkgs:
            pkg_l = pkg.lower().split("[")[0]
            if pkg_l in REJECTED:
                rows.append([repo, pkg_l, "rejected", REJECTED[pkg_l], ""])
            elif pkg_l not in memspine_pkgs and pkg_l not in {
                "torch",
                "transformers",
                "fastapi",
                "uvicorn",
                "pydantic",
                "openai",
                "numpy",
                "sqlalchemy",
                "redis",
                "neo4j",
                "networkx",
                "tiktoken",
                "httpx",
                "pytest",
                "ruff",
            }:
                rows.append([repo, pkg_l, "surveyed_not_default", "", "candidate or alt adapter"])
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["repo", "package", "gap_type", "rationale", "notes"])
        w.writerows(rows)
    print(f"Wrote {path.name} ({len(rows)} rows)")


def main() -> None:
    EXPORTS.mkdir(parents=True, exist_ok=True)
    catalog = CATALOG.read_text(encoding="utf-8") if CATALOG.exists() else ""
    write_algorithms(EXPORTS / "ECOSYSTEM_ALGORITHMS.csv")
    write_adoption(EXPORTS / "ECOSYSTEM_PACKAGE_ADOPTION.csv")
    write_gaps(EXPORTS / "ECOSYSTEM_PACKAGE_GAPS.csv", catalog)


if __name__ == "__main__":
    main()
