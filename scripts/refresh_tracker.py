"""Coordinator helper: rebuild PLAN.md/plan.json, refresh the offline tracker and the shared tracker data.

usage: python scripts/refresh_tracker.py [--shared-dir DIR]

- regenerates docs/v2/PLAN.md and docs/v2/data/plan.json (build_plan.py)
- embeds the current plan into docs/v2/tracker.html (offline copy)
- writes plan.json into --shared-dir (the published tracker's data file), when given
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ap = argparse.ArgumentParser()
ap.add_argument("--shared-dir", default=None)
args = ap.parse_args()

subprocess.run(
    [sys.executable, str(ROOT / "docs/v2/data/build_plan.py")],
    check=True,
    stdout=subprocess.DEVNULL,
)
plan = (ROOT / "docs/v2/data/plan.json").read_text(encoding="utf-8")

page = ROOT / "docs/v2/tracker.html"
s = page.read_text(encoding="utf-8")
tag = '<script type="application/json" id="plan-data">'
a = s.index(tag) + len(tag)
b = s.index("</script>", a)
page.write_text(s[:a] + plan.replace("</", "<\\/") + s[b:], encoding="utf-8")

if args.shared_dir:
    (Path(args.shared_dir) / "plan.json").write_text(plan, encoding="utf-8")

import json
from collections import Counter

print(dict(Counter(p["status"] for p in json.loads(plan)["packages"])))
