#!/usr/bin/env python3
"""Pass #5 Phase 3 — rebuild ECOSYSTEM_PROMPTS.csv from staging with hub-matching text_anchor."""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STAGING = ROOT / "docs" / "survey" / "_staging"
OUT = ROOT / "docs" / "exports" / "ECOSYSTEM_PROMPTS.csv"

# Reuse staging parser from hub rebuild (same directory contract)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _rebuild_prompts_hub import parse_staging, slugify  # noqa: E402

CSV_FIELDS = [
    "repo",
    "repo_slug",
    "prompt_id",
    "name",
    "role",
    "subsystem",
    "source_file",
    "source_symbol",
    "text_anchor",
    "char_count",
    "line_count",
    "full_text",
]


def main() -> None:
    staging_files = sorted(STAGING.glob("*/PROMPTS.md"))
    if not staging_files:
        raise SystemExit(f"No staging PROMPTS.md files under {STAGING}")

    rows: list[dict[str, str | int]] = []
    for path in staging_files:
        meta, prompts = parse_staging(path)
        repo = meta.get("repo", path.parent.name)
        repo_slug = meta.get("repo_slug", path.parent.name)
        for p in prompts:
            prompt_id = p["prompt_id"]
            # text_anchor MUST match hub HTML ids: slugify(f"{repo_slug}-{prompt_id}")
            text_anchor = slugify(f"{repo_slug}-{prompt_id}")
            body = p["full_text"]
            rows.append(
                {
                    "repo": repo,
                    "repo_slug": repo_slug,
                    "prompt_id": prompt_id,
                    "name": p.get("name", ""),
                    "role": p.get("role", ""),
                    "subsystem": p.get("subsystem", ""),
                    "source_file": p.get("source_file", ""),
                    "source_symbol": p.get("source_symbol", ""),
                    "text_anchor": text_anchor,
                    "char_count": len(body),
                    "line_count": body.count("\n") + 1,
                    "full_text": body,
                }
            )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {OUT.relative_to(ROOT)} ({len(rows)} rows)")
    if rows:
        examples = [r["text_anchor"] for r in rows[:5]]
        print(f"First text_anchor examples: {examples}")


if __name__ == "__main__":
    main()
