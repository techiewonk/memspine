"""Coordinator helper: merge a work-package branch into restructure, auto-resolving generated-file conflicts.

usage: python scripts/merge_wp.py <branch> "<merge message>"

Resolves conflicts in:
- tests/unit/config/data/config_dump_baseline.json  -> regenerated (scripts/dump_config_baseline.py)
- tests/unit/golden/simple_profile_defaults.json     -> regenerated (MEMSPINE_UPDATE_GOLDENS=1)
- docs/USAGE.md                                      -> union of both sides' lines inside each conflict hunk
- tests/unit/test_docs_config_surface.py             -> ours
Any other conflict stops with a non-zero exit and leaves the merge in progress for manual resolution.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT.parent / "memspine" / ".venv" / "Scripts" / "python.exe")
BASELINE = "tests/unit/config/data/config_dump_baseline.json"
PROFILE = "tests/unit/golden/simple_profile_defaults.json"
GENERATED = {BASELINE, PROFILE}


def run(*cmd: str, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, check=check, env=env)


def union_hunks(path: Path) -> None:
    s = path.read_text(encoding="utf-8")

    def fix(m: re.Match) -> str:
        out, seen = [], set()
        for line in (m.group(1) + m.group(2)).splitlines(keepends=True):
            if line not in seen:
                out.append(line)
                seen.add(line)
        return "".join(out)

    path.write_text(
        re.sub(r"<<<<<<< [^\n]*\n(.*?)=======\n(.*?)>>>>>>> [^\n]*\n", fix, s, flags=re.S),
        encoding="utf-8",
    )


def main() -> int:
    branch, msg = sys.argv[1], sys.argv[2]
    r = run("git", "merge", "--no-ff", branch, "-m", msg, check=False)
    conflicts = run("git", "diff", "--name-only", "--diff-filter=U").stdout.split()
    if r.returncode != 0 and not conflicts:
        print(r.stdout, r.stderr)
        return 1
    unresolved = []
    for f in conflicts:
        if f in GENERATED or f == "tests/unit/test_docs_config_surface.py":
            run("git", "checkout", "--ours", f)
        elif f == "docs/USAGE.md":
            union_hunks(ROOT / f)
        else:
            unresolved.append(f)
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    (ROOT / BASELINE).write_text(
        run(PY, "scripts/dump_config_baseline.py", env=env).stdout, encoding="utf-8"
    )
    run(
        PY,
        "-m",
        "pytest",
        "tests/unit/test_simple_profile_golden.py",
        "-q",
        "-p",
        "no:cacheprovider",
        "-o",
        "addopts=",
        check=False,
        env=dict(env, MEMSPINE_UPDATE_GOLDENS="1"),
    )
    if unresolved:
        print("UNRESOLVED:", unresolved)
        return 2
    run("git", "add", "-A", "--", *conflicts, BASELINE, PROFILE)
    if conflicts:
        run("git", "commit", "-q", "--no-edit")
    else:
        st = run("git", "status", "--porcelain", "--", BASELINE, PROFILE).stdout
        if st.strip():
            run("git", "commit", "-q", "-m", f"{msg}: regenerated config baselines")
    print("merged", branch, "conflicts:", conflicts)
    return 0


sys.exit(main())
