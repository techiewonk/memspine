#!/usr/bin/env python3
"""Pass #5 Phase 2 — extract verbatim prompts from ecosystem repos into staging PROMPTS.md."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import textwrap
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
STAGING = Path(__file__).resolve().parent
REPOS_JSON = STAGING / "_repos.json"
CLONE_ROOT = Path("/tmp/mem-survey-repos")

PROMPT_NAME_RE = re.compile(r"prompt", re.IGNORECASE)
PROMPT_VAR_RE = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*)"
    r"\s*=\s*(?:[rf])?(?:'''|\"\"\")(.+?)(?:'''|\"\"\")",
    re.MULTILINE | re.DOTALL,
)

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    "dist",
    "build",
    ".mypy_cache",
    "tests",
    "test",
}


@dataclass
class PromptRecord:
    prompt_id: str
    name: str
    role: str
    source_file: str
    source_symbol: str
    full_text: str
    subsystem: str = ""


def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[-\s_]+", "-", text)
    return text.strip("-")


def prompt_id_from_name(name: str) -> str:
    base = re.sub(r"(?i)_?prompts?$", "", name)
    base = re.sub(r"(?i)^prompt_?", "", base)
    return slugify(base.replace("_", "-"))


def infer_role(path: Path, name: str) -> str:
    parts = "/".join(path.parts).lower()
    name_l = name.lower()
    hints = [
        ("mem_reader", ("mem_reader", "reader", "extract", "ingest")),
        ("mem_search", ("mem_search", "search", "retrieve", "query")),
        ("mem_scheduler", ("scheduler", "schedule")),
        ("mem_feedback", ("feedback",)),
        ("dedup", ("dedup", "dedupe")),
        ("summarize", ("summar", "summary")),
        ("reflect", ("reflect", "reflection")),
        ("entity", ("entity", "ner", "node", "edge", "graph")),
        ("infer", ("infer", "update", "judge")),
        ("consolidate", ("consolid", "reorgan", "community")),
        ("eval", ("eval", "judge")),
        ("tool", ("tool", "skill")),
        ("dream", ("dream", "motive", "reasoning")),
    ]
    hay = f"{parts} {name_l}"
    for role, keys in hints:
        if any(k in hay for k in keys):
            return role
    return "general"


def clone_repo(github: str) -> Path:
    CLONE_ROOT.mkdir(parents=True, exist_ok=True)
    name = github.split("/")[-1]
    dest = CLONE_ROOT / name
    if dest.exists():
        return dest
    url = f"https://github.com/{github}.git"
    subprocess.run(
        ["git", "clone", "--depth", "1", url, str(dest)],
        check=True,
        capture_output=True,
        text=True,
    )
    return dest


def _ast_string_assignments(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return out
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
            continue
        out.append((target.id, node.value.value))
    return out


def extract_python_prompts(repo_root: Path) -> list[PromptRecord]:
    records: list[PromptRecord] = []
    seen: set[tuple[str, str]] = set()
    for path in repo_root.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        rel = path.relative_to(repo_root).as_posix()
        if "/test" in rel or rel.startswith("test/"):
            continue
        rel_l = rel.lower()
        in_prompt_area = any(
            k in rel_l for k in ("prompt", "template", "dream", "mos_", "mem_")
        )
        if not in_prompt_area:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        candidates: list[tuple[str, str]] = _ast_string_assignments(text)
        if not candidates:
            for match in PROMPT_VAR_RE.finditer(text):
                candidates.append((match.group(1), match.group(2)))
        for var, body in candidates:
            body = body.strip()
            key = (rel, var)
            if key in seen or len(body) < 40:
                continue
            if not PROMPT_NAME_RE.search(var) and not PROMPT_NAME_RE.search(rel):
                continue
            seen.add(key)
            pid = prompt_id_from_name(var) or slugify(var)
            records.append(
                PromptRecord(
                    prompt_id=pid,
                    name=var,
                    role=infer_role(path, var),
                    source_file=rel,
                    source_symbol=var,
                    full_text=body,
                    subsystem=path.parent.name,
                )
            )
    return records


def extract_text_prompts(repo_root: Path) -> list[PromptRecord]:
    records: list[PromptRecord] = []
    seen: set[str] = set()
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".txt", ".jinja2", ".j2"}:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        rel = path.relative_to(repo_root).as_posix()
        if "prompt" not in rel.lower() and "/prompts/" not in rel.lower():
            continue
        try:
            body = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if len(body) < 40 or rel in seen:
            continue
        seen.add(rel)
        stem = path.stem
        pid = slugify(stem)
        records.append(
            PromptRecord(
                prompt_id=pid,
                name=stem,
                role=infer_role(path, stem),
                source_file=rel,
                source_symbol=stem,
                full_text=body,
                subsystem=path.parent.name,
            )
        )
    return records


def extract_graphiti_source(repo_root: Path) -> list[PromptRecord]:
    """Extract graphiti prompt text by evaluating prompt modules in isolation."""
    records: list[PromptRecord] = []
    prompts_pkg = repo_root / "graphiti_core" / "prompts"
    if not prompts_pkg.exists():
        return records
    loader_code = textwrap.dedent(
        """
        import importlib.util, json, sys, types
        from pathlib import Path

        pkg_root = Path(sys.argv[1])
        prompts_dir = pkg_root / "graphiti_core" / "prompts"
        # Minimal stub package tree so relative imports work without graphiti_core.__init__
        if "graphiti_core" not in sys.modules:
            pkg = types.ModuleType("graphiti_core")
            pkg.__path__ = [str(pkg_root / "graphiti_core")]
            sys.modules["graphiti_core"] = pkg
        helpers_path = prompts_dir / "prompt_helpers.py"
        spec_h = importlib.util.spec_from_file_location(
            "graphiti_core.prompts.prompt_helpers", helpers_path
        )
        mod_h = importlib.util.module_from_spec(spec_h)
        sys.modules["graphiti_core.prompts.prompt_helpers"] = mod_h
        spec_h.loader.exec_module(mod_h)

        models_path = prompts_dir / "models.py"
        spec_m = importlib.util.spec_from_file_location(
            "graphiti_core.prompts.models", models_path
        )
        mod_m = importlib.util.module_from_spec(spec_m)
        sys.modules["graphiti_core.prompts.models"] = mod_m
        spec_m.loader.exec_module(mod_m)

        snippets_path = prompts_dir / "snippets.py"
        if snippets_path.exists():
            spec_s = importlib.util.spec_from_file_location(
                "graphiti_core.prompts.snippets", snippets_path
            )
            mod_s = importlib.util.module_from_spec(spec_s)
            sys.modules["graphiti_core.prompts.snippets"] = mod_s
            spec_s.loader.exec_module(mod_s)

        mock = {
            "entity_types": [{"entity_type_id": 0, "name": "Person", "description": "A person"}],
            "previous_episodes": [],
            "episode_content": "Alice: I moved to Denver last month.",
            "episodes": [{"content": "Alice: I moved to Denver."}],
            "extracted_nodes": [{"name": "Alice", "summary": "A person named Alice"}],
            "extracted_edges": [],
            "existing_nodes": [],
            "existing_edges": [],
            "node": {"name": "Alice", "summary": "A person named Alice"},
            "edge": {"source": "Alice", "target": "Denver", "fact": "Alice moved to Denver"},
            "nodes": [{"name": "Alice"}, {"name": "Denver"}],
            "edges": [{"source": "Alice", "target": "Denver"}],
            "saga": "Alice moved to Denver",
            "query": "Where did Alice move?",
            "answer": "Denver",
            "edge_types": "[]",
            "existing_edges_context": "[]",
            "existing_nodes_context": "[]",
            "new_nodes": "[]",
            "new_edges": "[]",
        }
        out = []
        for path in sorted(prompts_dir.glob("*.py")):
            if path.name in {"__init__.py", "lib.py", "models.py", "prompt_helpers.py", "snippets.py"}:
                continue
            mod_name = f"graphiti_core.prompts.{path.stem}"
            spec = importlib.util.spec_from_file_location(mod_name, path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            try:
                spec.loader.exec_module(mod)
            except Exception:
                continue
            versions = getattr(mod, "versions", None)
            if not isinstance(versions, dict):
                continue
            for version_name, fn in versions.items():
                if not callable(fn):
                    continue
                try:
                    messages = fn(mock)
                except Exception:
                    continue
                parts = []
                for m in messages:
                    role = getattr(m, "role", "unknown")
                    content = getattr(m, "content", str(m))
                    parts.append(f"[{role}]\\n{content}")
                body = "\\n\\n".join(parts).strip()
                if len(body) < 40:
                    continue
                out.append({
                    "prompt_id": f"{path.stem}-{version_name}".replace("_", "-"),
                    "name": f"{path.stem}.{version_name}",
                    "role": path.stem.replace("_", "-"),
                    "source_file": f"graphiti_core/prompts/{path.name}",
                    "source_symbol": version_name,
                    "full_text": body,
                    "subsystem": path.stem,
                })
        print(json.dumps(out))
        """
    )
    try:
        proc = subprocess.run(
            ["python3", "-c", loader_code, str(repo_root)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            print(f"WARN graphiti source loader: {proc.stderr[:400]}")
            return extract_python_prompts(prompts_pkg)
        data = json.loads(proc.stdout)
        for item in data:
            records.append(PromptRecord(**item))
    except (json.JSONDecodeError, subprocess.TimeoutExpired, OSError):
        return extract_python_prompts(prompts_pkg)
    return records


def extract_graphiti_runtime(repo_root: Path) -> list[PromptRecord]:
    """Invoke graphiti prompt registry with minimal mock context."""
    return extract_graphiti_source(repo_root)


def extract_memspine_defaults(repo_root: Path) -> list[PromptRecord]:
    records: list[PromptRecord] = []
    prompts_dir = repo_root / "src" / "memspine" / "prompts" / "defaults"
    if not prompts_dir.exists():
        return records
    for path in sorted(prompts_dir.glob("*.yaml")):
        body = path.read_text(encoding="utf-8")
        if len(body) < 20:
            continue
        stem = path.stem
        records.append(
            PromptRecord(
                prompt_id=slugify(stem),
                name=stem,
                role=stem.split("_")[0] if "_" in stem else "general",
                source_file=path.relative_to(repo_root).as_posix(),
                source_symbol=stem,
                full_text=body,
                subsystem="defaults",
            )
        )
    return records


def dedupe_records(records: list[PromptRecord]) -> list[PromptRecord]:
    seen: set[str] = set()
    out: list[PromptRecord] = []
    for rec in sorted(records, key=lambda r: (r.prompt_id, r.source_file)):
        key = rec.prompt_id
        if key in seen:
            key = f"{rec.subsystem}-{rec.prompt_id}" if rec.subsystem else f"{rec.name}-{rec.prompt_id}"
            rec = PromptRecord(
                prompt_id=slugify(key),
                name=rec.name,
                role=rec.role,
                source_file=rec.source_file,
                source_symbol=rec.source_symbol,
                full_text=rec.full_text,
                subsystem=rec.subsystem,
            )
        if rec.prompt_id in seen:
            continue
        seen.add(rec.prompt_id)
        out.append(rec)
    return out


def render_staging_md(repo: str, repo_slug: str, records: list[PromptRecord]) -> str:
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        "---",
        f"repo: {repo}",
        f"repo_slug: {repo_slug}",
        f"prompt_count: {len(records)}",
        f"generated: {ts}",
        "pass: 5-phase-2-extract",
        "---",
        "",
        f"# {repo} — Prompt Inventory",
        "",
        "<!-- STAGING: verbatim prompt bodies for Pass #5 Phase 3 MERGE -->",
        "",
    ]
    for rec in records:
        lines.extend(
            [
                f"## {rec.role} · {rec.prompt_id}",
                "",
                "| Field | Value |",
                "|-------|-------|",
                f"| prompt_id | `{rec.prompt_id}` |",
                f"| name | `{rec.name}` |",
                f"| role | `{rec.role}` |",
                f"| subsystem | `{rec.subsystem}` |",
                f"| source_file | `{rec.source_file}` |",
                f"| source_symbol | `{rec.source_symbol}` |",
                "",
                "### full_text",
                "",
                "```text",
                rec.full_text,
                "```",
                "",
            ]
        )
    return "\n".join(lines)


def extract_repo(repo: str, meta: dict) -> list[PromptRecord]:
    slug = meta["slug"]
    if repo == "unimem":
        return extract_memspine_defaults(ROOT)
    github = meta.get("github")
    if not github:
        return []
    try:
        repo_root = clone_repo(github)
    except subprocess.CalledProcessError:
        print(f"WARN: clone failed for {repo} ({github})")
        return []
    records: list[PromptRecord] = []
    records.extend(extract_python_prompts(repo_root))
    records.extend(extract_text_prompts(repo_root))
    if repo == "graphiti":
        runtime = extract_graphiti_runtime(repo_root)
        if runtime:
            records = runtime + [r for r in records if r.prompt_id not in {x.prompt_id for x in runtime}]
    return dedupe_records(records)


def main() -> None:
    repos: dict[str, dict] = json.loads(REPOS_JSON.read_text(encoding="utf-8"))
    for repo, meta in repos.items():
        slug = meta["slug"]
        out_dir = STAGING / slug
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "PROMPTS.md"
        records = extract_repo(repo, meta)
        if not records:
            print(f"SKIP {repo}: no prompts extracted")
            continue
        out_path.write_text(render_staging_md(repo, slug, records), encoding="utf-8")
        print(f"OK {repo}: {len(records)} prompts -> {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
