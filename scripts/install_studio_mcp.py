#!/usr/bin/env python3
"""TS-4: register the Tuning Studio MCP server (``memspine-studio``) with the Claude desktop app and Claude Code.

    python scripts/install_studio_mcp.py                    # print the plan and the equivalent `claude mcp add`; change nothing
    python scripts/install_studio_mcp.py --dry-run          # the same, plus the diff of the desktop config
    python scripts/install_studio_mcp.py --apply-desktop    # edit %APPDATA%/Claude/claude_desktop_config.json (backup first)
    python scripts/install_studio_mcp.py --apply-claude-code  # run `claude mcp add --scope user ...`

The server entry points at the MAIN restructure checkout (``--root``), never at a worktree, and at the
shared venv's python. Idempotent: an entry that is already identical is left alone (no backup, no write);
every other key of the config is preserved. Nothing here installs a package.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

NAME = "memspine-studio"
DEFAULT_ROOT = "C:/Users/hembad/Documents/GitHub/memspine-restructure"
DEFAULT_PYTHON = "C:/Users/hembad/Documents/GitHub/memspine/.venv/Scripts/python.exe"


def desktop_config_path() -> Path:
    base = os.environ.get("APPDATA")
    if not base:
        raise SystemExit("APPDATA is not set; pass --config <claude_desktop_config.json>")
    return Path(base) / "Claude" / "claude_desktop_config.json"


def build_entry(root: str, python: str, allow_gpu: bool = False, cwd: bool = False) -> dict:
    root = root.rstrip("/\\")
    args = ["-m", "memspine_evals.studio.server"] + (["--allow-gpu"] if allow_gpu else [])
    entry: dict = {
        "command": python,
        "args": args,
        "env": {"PYTHONPATH": os.pathsep.join([f"{root}/src", f"{root}/evals"])},
    }
    if cwd:
        entry["cwd"] = f"{root}/evals"
    return entry


def claude_add_command(entry: dict) -> list[str]:
    # the name goes before the options: ``-e`` is variadic and would swallow it otherwise
    cmd = ["claude", "mcp", "add", NAME, "--scope", "user", "--transport", "stdio"]
    for k, v in entry.get("env", {}).items():
        cmd += ["-e", f"{k}={v}"]
    return cmd + ["--", entry["command"], *entry["args"]]


def render(cfg: dict) -> str:
    return json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"


def load_config(path: Path) -> tuple[dict, str]:
    if not path.exists():
        return {}, ""
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        return {}, text
    try:
        cfg = json.loads(text)
    except ValueError as exc:
        raise SystemExit(
            f"{path} is not valid JSON ({exc}); fix it by hand, nothing was changed"
        ) from exc
    if not isinstance(cfg, dict):
        raise SystemExit(f"{path}: top level is not an object; nothing was changed")
    return cfg, text


def plan(path: Path, entry: dict) -> tuple[dict, str, str, str]:
    """-> (new config, old text, new text, state) with state in {'add', 'update', 'unchanged'}."""
    cfg, old_text = load_config(path)
    servers = cfg.get("mcpServers")
    if servers is not None and not isinstance(servers, dict):
        raise SystemExit(f"{path}: mcpServers is not an object; nothing was changed")
    new = dict(cfg)
    new["mcpServers"] = dict(servers or {})
    state = (
        "unchanged"
        if new["mcpServers"].get(NAME) == entry
        else ("update" if NAME in new["mcpServers"] else "add")
    )
    new["mcpServers"][NAME] = entry
    return new, old_text, render(new), state


def apply_desktop(path: Path, entry: dict) -> str:
    new, old_text, new_text, state = plan(path, entry)
    if state == "unchanged":
        return f"unchanged: {path} already has mcpServers.{NAME}"
    if path.exists():
        backup = path.with_name(path.name + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
        note = f"backup {backup}"
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        note = "no earlier file, created"
    path.write_text(new_text, encoding="utf-8", newline="\n")
    verb = {"add": "added", "update": "updated"}.get(state, state)
    return f"{verb} mcpServers.{NAME} in {path} ({note}); restart the Claude desktop app"


def apply_claude_code(entry: dict) -> int:
    exe = shutil.which("claude")
    if not exe:
        print(
            "claude CLI not found on PATH; run the printed `claude mcp add` command yourself",
            file=sys.stderr,
        )
        return 1
    have = subprocess.run([exe, "mcp", "get", NAME], capture_output=True, text=True, check=False)
    if have.returncode == 0:
        subprocess.run(
            [exe, "mcp", "remove", "--scope", "user", NAME],
            capture_output=True,
            text=True,
            check=False,
        )
    cmd = claude_add_command(entry)
    cmd[0] = exe
    done = subprocess.run(cmd, capture_output=True, text=True, check=False)
    print((done.stdout + done.stderr).strip())
    return done.returncode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print the desktop config diff and the claude command; change nothing",
    )
    ap.add_argument(
        "--apply-desktop", action="store_true", help="edit the desktop app config (backup first)"
    )
    ap.add_argument(
        "--apply-claude-code", action="store_true", help="run `claude mcp add --scope user ...`"
    )
    ap.add_argument(
        "--config",
        help="desktop config path (default %%APPDATA%%/Claude/claude_desktop_config.json)",
    )
    ap.add_argument(
        "--root", default=DEFAULT_ROOT, help="the MAIN restructure checkout the server runs from"
    )
    ap.add_argument("--python", default=DEFAULT_PYTHON, help="python of the shared venv")
    ap.add_argument(
        "--allow-gpu",
        action="store_true",
        help="start the server with --allow-gpu (studio_screen may then launch the queue)",
    )
    ap.add_argument(
        "--cwd", action="store_true", help="also write a cwd key (the desktop app may ignore it)"
    )
    a = ap.parse_args(argv)

    path = Path(a.config) if a.config else desktop_config_path()
    entry = build_entry(a.root, a.python, a.allow_gpu, a.cwd)
    _, old_text, new_text, state = plan(path, entry)
    print(
        f"desktop config: {path} ({'exists' if path.exists() else 'absent'}); mcpServers.{NAME}: {state}"
    )
    print("entry:", json.dumps(entry, indent=2))
    print(
        "\nClaude Code (user scope):\n  "
        + " ".join(shlex.quote(c) for c in claude_add_command(entry))
    )
    if state != "unchanged":
        diff = "".join(
            difflib.unified_diff(
                old_text.splitlines(True),
                new_text.splitlines(True),
                f"{path.name} (current)",
                f"{path.name} (new)",
            )
        )
        print("\ndiff:\n" + (diff or "(new file)\n" + new_text))
    rc = 0
    if a.apply_desktop and not a.dry_run:
        print("\n" + apply_desktop(path, entry))
    if a.apply_claude_code and not a.dry_run:
        rc = apply_claude_code(entry)
    if not (a.apply_desktop or a.apply_claude_code) and not a.dry_run:
        print("\n(nothing changed: pass --apply-desktop and/or --apply-claude-code)")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
