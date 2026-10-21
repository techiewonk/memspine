"""Refresh the single Markdown task register and its read-only HTML view.

Only docs/PROVIDER_TODO.md owns editable status. This command never rebuilds
legacy v2 package plans, publishes files, or writes a second task-status JSON.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs/PROVIDER_TODO.md"
START = "<!-- TASK_REGISTER_START -->"
END = "<!-- TASK_REGISTER_END -->"
SUMMARY_START = "<!-- CURRENT_SUMMARY_START -->"
SUMMARY_END = "<!-- CURRENT_SUMMARY_END -->"
STATUSES = {
    "DONE",
    "OPEN",
    "IN_PROGRESS",
    "VERIFY",
    "PLANNED",
    "OPEN_REVIEW",
    "REVIEW",
    "PROPOSED",
    "DEPLOYMENT_CHECK",
    "NEEDS_INPUT",
    "NEEDS_TOOLCHAIN",
    "DEFERRED",
    "EXCLUDED",
    "BLOCKED",
}


def tasks(source: str) -> list[list[str]]:
    if source.count(START) != 1 or source.count(END) != 1:
        raise ValueError("one canonical task-register block is required")
    block = source.split(START, 1)[1].split(END, 1)[0]
    rows = []
    seen = set()
    for line in block.splitlines():
        if not line.startswith("| "):
            continue
        cells = [part.strip() for part in line.strip("|").split("|")]
        if cells[0] == "ID":
            continue
        if len(cells) != 8 or not re.fullmatch(r"(?:PA|BI|DS|RS|RM)-\d{2,3}", cells[0]):
            raise ValueError(f"invalid canonical task row: {line}")
        if cells[0] in seen or cells[1] not in STATUSES:
            raise ValueError(f"duplicate ID or invalid status: {cells[0]}")
        if any(not cell for cell in cells):
            raise ValueError(f"missing task detail: {cells[0]}")
        seen.add(cells[0])
        rows.append(cells)
    if not rows:
        raise ValueError("canonical task register is empty")
    return rows


def summary(rows: list[list[str]]) -> str:
    statuses = Counter(row[1] for row in rows)
    programs = Counter(row[0].split("-")[0] for row in rows)
    lines = ["| Current status | All work items |", "|---|---:|"]
    lines.extend(f"| {key} | {count} |" for key, count in sorted(statuses.items()))
    lines.extend(["", "| Program | Items |", "|---|---:|"])
    lines.extend(f"| {key} | {count} |" for key, count in sorted(programs.items()))
    lines.append(
        f"\nTotal: **{len(rows)} unique task IDs**. Provider-only historical denominator "
        "stays 136; the combined total also includes BI, DS, RS and imported review suggestions.\n"
    )
    return "\n".join(lines)


def render(source: str, rows: list[list[str]]) -> str:
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    heads = [
        "ID",
        "Current status",
        "Priority",
        "Owner",
        "Task",
        "Completed / evidence",
        "Next milestone",
        "Dependencies",
    ]
    header = "".join(f"<th>{html.escape(value)}</th>" for value in heads)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(value)}</td>" for value in row) + "</tr>"
        for row in rows
    )
    return f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MemSpine shared task register</title>
<style>body{{font:15px system-ui;margin:24px;color:#172238;background:#f7f9fc}}
h1{{margin-bottom:8px}}p{{max-width:1000px}}input{{padding:12px;width:min(95%,600px);margin:12px 0}}
.wrap{{overflow:auto;max-height:76vh;border:1px solid #cdd6e3;background:white}}
table{{border-collapse:collapse;min-width:1700px;width:100%}}th,td{{text-align:left;vertical-align:top;padding:10px;border-bottom:1px solid #dae1ec}}
th{{position:sticky;top:0;background:#eaf0f8}}td:first-child{{font-weight:700;white-space:nowrap}}
small{{overflow-wrap:anywhere}}tr[hidden]{{display:none}}</style>
<h1>MemSpine shared task register</h1>
<p><strong>Read-only generated view.</strong> Claude and Codex edit only <code>docs/PROVIDER_TODO.md</code>.
No browser status or notes are saved here. Refresh with <code>python scripts/refresh_tracker.py</code>.</p>
<p>{len(rows)} tasks. <small>Source SHA256: {digest}</small></p>
<input id="filter" aria-label="Filter tasks" placeholder="Filter ID, status, owner or task">
<div class="wrap"><table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table></div>
<script>document.getElementById('filter').addEventListener('input', function() {{
const query=this.value.toLowerCase();document.querySelectorAll('tbody tr').forEach(row=>{{
row.hidden=!row.textContent.toLowerCase().includes(query);}});}});</script></html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate totals/view without writing")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/v2/tracker.html")
    args = parser.parse_args()
    source = SOURCE.read_text(encoding="utf-8")
    rows = tasks(source)
    prefix, tail = source.split(SUMMARY_START, 1)
    _, suffix = tail.split(SUMMARY_END, 1)
    updated = prefix + SUMMARY_START + "\n" + summary(rows) + SUMMARY_END + suffix
    page = render(updated, rows)
    if args.check:
        if source != updated:
            raise SystemExit("task summary is stale; run python scripts/refresh_tracker.py")
        if not args.output.exists() or args.output.read_text(encoding="utf-8") != page:
            raise SystemExit("read-only view is stale; run python scripts/refresh_tracker.py")
    else:
        SOURCE.write_text(updated, encoding="utf-8")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(page, encoding="utf-8")
    print(f"{'Checked' if args.check else 'Refreshed'} {len(rows)} tasks from {SOURCE}")
    print(dict(Counter(row[1] for row in rows)))


if __name__ == "__main__":
    main()
