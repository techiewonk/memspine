#!/usr/bin/env python3
"""Pass #5 Phase 3 MERGE — rebuild docs/ECOSYSTEM_PROMPTS.md hub from staging PROMPTS.md files."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STAGING = ROOT / "docs" / "survey" / "_staging"
OUT = ROOT / "docs" / "ECOSYSTEM_PROMPTS.md"

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
TABLE_FIELD_RE = re.compile(r"\| (\w+) \| `([^`]*)` \|")


def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[-\s_]+", "-", text)
    return text.strip("-")


def parse_frontmatter(text: str) -> dict[str, str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta


def parse_table(table: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for m in TABLE_FIELD_RE.finditer(table):
        fields[m.group(1)] = m.group(2)
    return fields
def extract_codeblock(body_part: str) -> str | None:
    open_m = re.search(r"```(?:text|txt)?\n", body_part)
    if not open_m:
        return None
    start = open_m.end()
    end_marker = body_part.rfind("\n```")
    if end_marker <= start:
        return None
    return body_part[start:end_marker]


def parse_section(section: str) -> dict[str, str] | None:
    header_m = re.match(r"^## (?P<role>[^·\n]+) · (?P<prompt_id>[^\n]+)\n", section)
    if not header_m:
        return None
    ft_idx = section.find("### full_text")
    if ft_idx < 0:
        return None
    table_part = section[:ft_idx]
    body_part = section[ft_idx:]
    table_m = re.search(
        r"\| Field \| Value \|\n\|[-| ]+\|\n(?P<table>(?:\|[^\n]+\|\n)+)",
        table_part,
    )
    if not table_m:
        return None
    body = extract_codeblock(body_part)
    if body is None:
        return None
    fields = parse_table(table_m.group("table"))
    prompt_id = header_m.group("prompt_id").strip()
    fields.setdefault("prompt_id", prompt_id)
    fields.setdefault("role", header_m.group("role").strip())
    fields["full_text"] = body
    return fields


def parse_staging(path: Path) -> tuple[dict[str, str], list[dict[str, str]]]:
    text = path.read_text(encoding="utf-8")
    meta = parse_frontmatter(text)
    body = FRONTMATTER_RE.sub("", text, count=1)
    prompts: list[dict[str, str]] = []
    repo_slug = meta.get("repo_slug", path.parent.name)
    repo = meta.get("repo", repo_slug)
    for chunk in re.split(r"(?=^## [^·\n]+ · [^\n]+\n\n\| Field)", body, flags=re.MULTILINE):
        chunk = chunk.strip()
        if not chunk.startswith("## "):
            continue
        fields = parse_section(chunk)
        if not fields:
            continue
        prompt_id = fields["prompt_id"]
        fields["repo"] = repo
        fields["repo_slug"] = repo_slug
        fields["text_anchor"] = slugify(f"{repo_slug}-{prompt_id}")
        prompts.append(fields)
    return meta, prompts


def build_toc(repos: list[tuple[str, str, int]]) -> str:
    lines = ["## Table of contents", ""]
    for repo, slug, count in repos:
        lines.append(f"- [{repo}](#{slug}) — {count} prompts")
    lines.append("")
    return "\n".join(lines)


def render_hub(all_prompts: list[tuple[dict[str, str], list[dict[str, str]]]]) -> str:
    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    total = sum(len(p) for _, p in all_prompts)
    repo_summaries = [(m.get("repo", ""), m.get("repo_slug", ""), len(p)) for m, p in all_prompts]
    lines = [
        "# ECOSYSTEM_PROMPTS — Memory Engine Prompt Hub",
        "",
        f"> **Pass #5 Phase 3 MERGE** · generated {ts} · **{total}** prompts across **{len(all_prompts)}** repos",
        "",
        "Full verbatim prompt bodies inlined below (not staging stubs). Each prompt has an HTML anchor "
        "`id=\"{repo_slug}-{prompt_id}\"` matching `text_anchor` in `docs/exports/ECOSYSTEM_PROMPTS.csv`.",
        "",
        build_toc(repo_summaries),
        "---",
        "",
    ]
    for meta, prompts in all_prompts:
        repo = meta.get("repo", "unknown")
        slug = meta.get("repo_slug", slugify(repo))
        lines.extend(
            [
                f'<a id="{slug}"></a>',
                "",
                f"# {repo}",
                "",
                f"*{len(prompts)} prompts · staging source: `docs/survey/_staging/{slug}/PROMPTS.md`*",
                "",
            ]
        )
        for p in prompts:
            anchor = p["text_anchor"]
            lines.extend(
                [
                    f'<a id="{anchor}"></a>',
                    "",
                    f"## {repo} · {p.get('role', 'general')} · `{p['prompt_id']}`",
                    "",
                    "| Field | Value |",
                    "|-------|-------|",
                    f"| prompt_id | `{p['prompt_id']}` |",
                    f"| name | `{p.get('name', '')}` |",
                    f"| role | `{p.get('role', '')}` |",
                    f"| subsystem | `{p.get('subsystem', '')}` |",
                    f"| source_file | `{p.get('source_file', '')}` |",
                    f"| source_symbol | `{p.get('source_symbol', '')}` |",
                    f"| text_anchor | `{anchor}` |",
                    "",
                    "### full_text",
                    "",
                    "```text",
                    p["full_text"],
                    "```",
                    "",
                ]
            )
        lines.extend(["---", ""])
    return "\n".join(lines)


def main() -> None:
    staging_files = sorted(STAGING.glob("*/PROMPTS.md"))
    if not staging_files:
        raise SystemExit(f"No staging PROMPTS.md files under {STAGING}")
    parsed: list[tuple[dict[str, str], list[dict[str, str]]]] = []
    for path in staging_files:
        meta, prompts = parse_staging(path)
        if not prompts:
            print(f"WARN: no prompts parsed from {path}")
            continue
        parsed.append((meta, prompts))
        print(f"  {meta.get('repo', path.parent.name)}: {len(prompts)} prompts")
    hub = render_hub(parsed)
    OUT.write_text(hub, encoding="utf-8")
    print(f"Wrote {OUT.relative_to(ROOT)} ({len(hub.splitlines())} lines, {len(hub):,} bytes)")


if __name__ == "__main__":
    main()
