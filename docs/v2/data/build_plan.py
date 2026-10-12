"""Build docs/v2/data/plan.json and docs/v2/PLAN.md: the work-package plan with status.

    python docs/v2/data/build_plan.py                          # regenerate PLAN.md / plan.json, run all checks
    python docs/v2/data/build_plan.py --brief P0-04            # write docs/v2/briefs/P0-04.md from TEMPLATE.md
    python docs/v2/data/build_plan.py --brief P0-04 --force    # ... even with unfinished dependencies / no ownership

Exit code is non-zero on any failed assertion, or when two concurrently runnable packages own overlapping files
(unless the pair is listed in ALLOWED_SHARED in packages.py). Every one of the 155 decisions must be covered by at
least one package (checked here). Status values:
Not started, Ready, In progress, Blocked, Needs measurement, Done, Dropped.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE))
STATUSES = ("Not started", "Ready", "In progress", "Blocked", "Needs measurement", "Done", "Dropped")

# (id, phase, title, decisions, depends_on, kind, gate, status, note)
# kind: "move" = pure restructure (tests + CPU golden); "behaviour" = needs a GPU screen through the paired gate;
# "measure" = a run or screen; "infra" = tooling.
from packages import ALLOWED_SHARED, LANES, OWNS, REVIEW, WP  # noqa: E402  (the package list lives in packages.py)


#: Memory type -> layer of the decided stack (Q1): L0 raw, L1 facts, L2 episodes and summaries, L3 graph,
#: L4 observations and profile, L5 standing answers, procedures and plans. Working memory is context (C), shown as "ctx".
TYPE_LAYER = {"episodic": "L0", "resource": "L0", "semantic": "L1", "associative": "L3", "reflective": "L4",
              "procedural": "L5", "prospective": "L5", "working": "ctx", "shared": "scope"}
OP_ORDER = ["D", "K_C", "K_P", "R", "C", "G"]


def _layers(types: set[str]) -> list[str]:
    out = {TYPE_LAYER[t] for t in types if t in TYPE_LAYER}
    return sorted(out, key=lambda x: (x[0] != "L", x))


def _lane(wid: str, kind: str, files: list[str]) -> str:
    """Agent lane: one agent at a time inside a serial lane; other lanes run in parallel once dependencies are Done."""
    for name, members in LANES.items():
        if wid in members:
            return name
    return {"P0": "infra", "X": "infra", "W2": "write", "O4": "offline", "MD": "models", "T5": "trust", "R6": "release",
            "EX": "exit"}.get(wid.split("-")[0], "other")


def _check_acyclic(rows: list[dict]) -> None:
    deps = {r["id"]: r["depends_on"] for r in rows}
    state: dict[str, int] = {}

    def visit(n: str, path: list[str]) -> None:
        if state.get(n) == 1:
            raise AssertionError("dependency cycle: " + " -> ".join(path + [n]))
        if state.get(n) == 2:
            return
        state[n] = 1
        for d in deps[n]:
            visit(d, path + [n])
        state[n] = 2

    for n in deps:
        visit(n, [])


# ---------------------------------------------------------------------------------------------------------------
# Ownership: overlap, concurrency, collisions, briefs (X-01)
# ---------------------------------------------------------------------------------------------------------------
def repo_files() -> list[str]:
    """Existing repo files (tracked plus untracked-not-ignored), repo-relative with forward slashes."""
    try:
        out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=REPO, capture_output=True, text=True,
                             check=True).stdout
        return [ln for ln in out.splitlines() if ln]
    except (OSError, subprocess.CalledProcessError):
        return [p.relative_to(REPO).as_posix() for p in REPO.rglob("*") if p.is_file() and ".git" not in p.parts]


def glob_regex(pat: str) -> re.Pattern[str]:
    """`**` matches across `/`, `*` and `?` do not; everything else is literal."""
    out, i = [], 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def _is_literal(pat: str) -> bool:
    return not any(c in pat for c in "*?")


def patterns_overlap(a: str, b: str, files: list[str] | None = None) -> bool:
    """True if two glob patterns can name the same file: identical; one matches the other's literal path; both match a
    common existing file; or (for files that do not exist yet) one is a `dir/**` glob and the other lies under `dir`."""
    if a == b:
        return True
    ra, rb = glob_regex(a), glob_regex(b)
    if (_is_literal(b) and ra.match(b)) or (_is_literal(a) and rb.match(a)):
        return True
    if files is not None and any(ra.match(f) and rb.match(f) for f in files):
        return True
    for x, y in ((a, b), (b, a)):
        if x.endswith("/**"):
            base = x[:-3]
            if y == base or y.startswith(base + "/"):
                return True
            # a wildcard path like `src/*/registry.py` may land under `base`: probe with base + its last segment
            probe = base + "/" + y.rsplit("/", 1)[-1].replace("*", "x").replace("?", "x")
            if glob_regex(y).match(probe):
                return True
    return False


def owns_overlap(oa: list[str], ob: list[str], files: list[str] | None = None) -> list[tuple[str, str]]:
    return [(a, b) for a in oa for b in ob if patterns_overlap(a, b, files)]


def transitive_deps(rows: list[dict]) -> dict[str, set[str]]:
    direct = {r["id"]: list(r["depends_on"]) for r in rows}
    memo: dict[str, set[str]] = {}

    def go(n: str) -> set[str]:
        if n not in memo:
            memo[n] = set()
            for d in direct[n]:
                memo[n] |= {d} | go(d)
        return memo[n]

    return {n: go(n) for n in direct}


def serial_lane(wid: str, lanes: dict[str, set[str]] = LANES) -> str | None:
    return next((name for name, members in lanes.items() if wid in members), None)


def concurrently_runnable(a: str, b: str, closure: dict[str, set[str]], lanes: dict[str, set[str]] = LANES) -> bool:
    """Neither depends on the other transitively and they are not in the same serial lane."""
    if a == b or b in closure[a] or a in closure[b]:
        return False
    la, lb = serial_lane(a, lanes), serial_lane(b, lanes)
    return not (la is not None and la == lb)


def find_collisions(rows: list[dict], owns: dict[str, list[str]], allowed: list[tuple[str, str, str]],
                    files: list[str] | None = None, lanes: dict[str, set[str]] = LANES):
    """Return (collisions, allowed_hits): lists of (id_a, id_b, [(pat_a, pat_b), ...]). Done packages are skipped."""
    closure = transitive_deps(rows)
    status = {r["id"]: r["status"] for r in rows}
    ok = {frozenset((a, b)) for a, b, _ in allowed}
    bad, shared = [], []
    ids = [r["id"] for r in rows if r["id"] in owns and status[r["id"]] != "Done"]
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if not concurrently_runnable(a, b, closure, lanes):
                continue
            hit = owns_overlap(owns[a], owns[b], files)
            if hit:
                (shared if frozenset((a, b)) in ok else bad).append((a, b, hit))
    return bad, shared


_KIND_GATE = {
    "move": "Pure move: CPU golden unchanged (byte-identical), `tests/unit` and `evals/tests` green, import-linter clean. No behaviour change.",
    "migration": "Persisted schema change: Alembic upcaster plus replay-equivalence tests; single Alembic head; golden unchanged unless this brief says otherwise.",
    "api": "New contract or lifetime: contract tests added; golden unchanged; public surface documented.",
    "behaviour": "Built flag-off, golden unchanged, GPU screen queued through the paired gate (set status to Needs measurement).",
    "measure": "A run or screen: record the manifest and the paired-gate verdict; no source change beyond tooling.",
    "infra": "Tooling or docs: tests for the tooling, existing suites green; no change to defaults.",
}


def _slug(heading: str) -> str:
    s = re.sub(r"[^\w\s-]", "", heading.strip().lower())
    return re.sub(r"\s", "-", s)


def decision_anchors() -> dict[str, str]:
    """Decision id -> `areas/<file>.md#<slug>` (first heading that starts with the id)."""
    out: dict[str, str] = {}
    for p in sorted((HERE.parent / "areas").glob("*.md")):
        for ln in p.read_text(encoding="utf-8").splitlines():
            m = re.match(r"#{2,4}\s+([A-Z]+-\d+)\b", ln)
            if m and m.group(1) not in out:
                out[m.group(1)] = f"areas/{p.name}#{_slug(ln.lstrip('# '))}"
    return out


def render_brief(wid: str, rows: list[dict], owns: dict[str, list[str]], titles: dict[str, str], allowed, template: str,
                 base: str = "restructure", anchors: dict[str, str] | None = None) -> str:
    byid = {r["id"]: r for r in rows}
    r = byid[wid]
    closure = transitive_deps(rows)
    ok = {frozenset((a, b)) for a, b, _ in allowed}
    anchors = decision_anchors() if anchors is None else anchors
    dec = "\n".join(f"- {d} {titles[d]}" + (f" ([{anchors[d].split('#')[0]}]({anchors[d]}))" if d in anchors else " (no area anchor found)")
                    for d in r["decisions"]) or "- none"
    deps = "\n".join(f"- {d} {byid[d]['title']}: **{byid[d]['status']}**" for d in r["depends_on"]) or "- none"
    forbid: dict[str, list[str]] = {}
    for o in rows:
        oid = o["id"]
        if (oid == wid or o["status"] == "Done" or oid not in owns or frozenset((wid, oid)) in ok
                or not concurrently_runnable(wid, oid, closure)):
            continue
        for pat in owns[oid]:
            forbid.setdefault(pat, []).append(oid)
    forbidden = "\n".join(f"- `{p}` ({', '.join(ids)})" for p, ids in sorted(forbid.items()))
    forbidden += ("\n" if forbidden else "") + "- `evals/runs`, `data/`, the `pinned_run.sh` interface (unless listed under You own)"
    fill = {
        "{{id}}": wid, "{{title}}": r["title"], "{{kind}}": r["kind"], "{{lane}}": r["lane"],
        "{{goal}}": f"<TODO goal: {r['title']}. Registry note: {r['note'] or 'none'}>",
        "{{decisions}}": dec, "{{depends}}": deps, "{{base}}": base,
        "{{owned}}": "\n".join(f"- `{p}`" for p in owns.get(wid, [])) + "\n- the tests for the above",
        "{{forbidden}}": forbidden, "{{gate}}": r["gate"], "{{kind_gate}}": _KIND_GATE.get(r["kind"], ""),
        "{{review}}": ", ".join(r["review"]) or "none",
    }
    for k, v in fill.items():
        template = template.replace(k, v)
    return template


def short_owns(pats: list[str]) -> str:
    if not pats:
        return "-"
    return f"`{pats[0]}`" + (f" +{len(pats) - 1}" if len(pats) > 1 else "")


def _build_rows() -> tuple[list[dict], dict[str, str]]:
    answered = json.loads((HERE / "decisions_answered.json").read_text(encoding="utf-8"))
    titles = {q["id"]: q["title"] for q in answered}
    verify = {}
    for p in sorted(HERE.glob("verify_*.json")):
        for v in json.loads(p.read_text(encoding="utf-8")):
            verify[v["id"]] = v
    rows = []
    for (wid, phase, title, dec, dep, kind, gate, status, note) in WP:
        assert status in STATUSES, (wid, status)
        for d in dec:
            assert d in titles, (wid, d)
        types = {t for d in dec for t in verify.get(d, {}).get("memory_types", [])}
        ops = {o for d in dec for o in verify.get(d, {}).get("operators", [])}
        locs = [loc.split(":")[0] for d in dec for loc in verify.get(d, {}).get("v1_locations", [])]
        files = sorted(set(locs), key=lambda f: (-locs.count(f), f))[:4]
        rows.append({"id": wid, "phase": phase, "title": title, "decisions": dec, "depends_on": dep, "kind": kind,
                     "gate": gate, "status": status, "note": note, "layers": _layers(types),
                     "operators": [o for o in OP_ORDER if o in ops], "files": files,
                     "owns": list(OWNS.get(wid, [])),
                     "lane": _lane(wid, kind, files), "agent": "sonnet",
                     "components": sorted({verify[d]["v2_component"] for d in dec if d in verify})[:4],
                     "review": REVIEW.get(wid, [])})
    return rows, titles


def write_brief(wid: str, force: bool) -> int:
    rows, titles = _build_rows()
    byid = {r["id"]: r for r in rows}
    if wid not in byid:
        print(f"unknown package {wid}", file=sys.stderr)
        return 2
    problems = [f"dependency {d} is {byid[d]['status']}, not Done" for d in byid[wid]["depends_on"] if byid[d]["status"] != "Done"]
    if wid not in OWNS:
        problems.append("no OWNS entry (cannot be briefed)")
    if problems and not force:
        print(f"refusing to brief {wid}: " + "; ".join(problems) + " (use --force)", file=sys.stderr)
        return 1
    try:
        base = subprocess.run(["git", "rev-parse", "--short", "restructure"], cwd=REPO, capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        base = "HEAD"
    text = render_brief(wid, rows, OWNS, titles, ALLOWED_SHARED,
                        (HERE.parent / "briefs" / "TEMPLATE.md").read_text(encoding="utf-8"), base=f"`restructure` at {base}")
    out = HERE.parent / "briefs" / f"{wid}.md"
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out.relative_to(REPO).as_posix()}" + (f" (forced past: {'; '.join(problems)})" if problems else ""))
    return 0


def main() -> int:
    rows, titles = _build_rows()
    ids = {r["id"] for r in rows}
    for r in rows:
        for d in r["depends_on"]:
            assert d in ids, (r["id"], d)
    for k in OWNS:
        assert k in ids, f"OWNS has unknown package {k}"
    _check_acyclic(rows)
    covered = {d for r in rows for d in r["decisions"]}
    er_ids = [f"ER-{i:02d}" for i in range(1, 41)] + ["POL-1", "POL-2"]
    er_missing = [e for e in er_ids if not any(e in r["review"] for r in rows)]
    assert not er_missing, f"review findings without a package: {er_missing}"
    missing = sorted(set(titles) - covered)
    assert not missing, f"decisions without a work package: {missing}"

    (HERE / "plan.json").write_text(json.dumps({"statuses": STATUSES, "packages": rows}, indent=1, ensure_ascii=False) + "\n",
                                    encoding="utf-8")
    phases = {0: "Phase 0: settle and prepare", 1: "Phase 1: foundations (restructure in place)", 2: "Phase 2: write path and models",
              3: "Phase 3: read path", 4: "Phase 4: offline memory", 5: "Phase 5: trust, API, operations", 6: "Phase 6: release and paper"}
    from collections import Counter
    c = Counter(r["status"] for r in rows)
    out = ["# MemSpine restructure plan: work packages and status", "",
           "Generated from `data/build_plan.py` (the source of truth for rows); status is also kept on the live tracker page and synced back here.",
           f"{len(rows)} packages cover all {len(titles)} decisions. Kinds: **move** = pure restructure (tests and CPU golden), **migration** = persisted schema change (upcaster and replay-equivalence tests), **api** = new contract or lifetime (contract tests), **behaviour** = "
           "needs a GPU screen through the paired gate, **measure** = a run, **infra** = tooling.", "",
           "Layers follow the decided stack (Q1): L0 raw, L1 facts, L2 episodes and summaries, L3 graph, L4 observations and "
           "profile, L5 standing answers, procedures and plans; ctx = working context. Ops are the loop operators (D deposit, "
           "K_C consolidate, K_P personalise, R retrieve, C compose, G generate). Each package is one task for one agent "
           "(Sonnet); packages in a `serial` lane run one at a time, different lanes may run in parallel once their "
           "dependencies are Done. Owns = first owned path and the count of further patterns (full list in the ownership "
           "table at the end; `OWNS` in `data/packages.py`).", "",
           "| Status | Count |", "|---|---|"] + [f"| {s} | {c.get(s, 0)} |" for s in STATUSES]
    for ph in sorted(phases):
        out += ["", f"## {phases[ph]}", "",
                "| Id | Work package | Layers | Ops | Decisions | Review | Depends on | Kind | Agent lane | Owns | Gate | Status | Note |",
                "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in rows:
            if r["phase"] == ph:
                out.append(f"| {r['id']} | {r['title']} | {' '.join(r['layers']) or '-'} | {' '.join(r['operators']) or '-'} | "
                           f"{', '.join(r['decisions'])} | {', '.join(r['review']) or '-'} | {', '.join(r['depends_on']) or '-'} | {r['kind']} | {r['lane']} | "
                           f"{short_owns(r['owns'])} | {r['gate']} | **{r['status']}** | {r['note']} |")

    files = repo_files()
    bad, shared = find_collisions(rows, OWNS, ALLOWED_SHARED, files)
    unowned = [r["id"] for r in rows if r["id"] not in OWNS and r["status"] != "Done"]
    out += ["", "## Ownership", "",
            "Files each package may write (glob patterns, repo-relative; includes removal and edit targets). Two packages that can "
            "run at the same time must not own overlapping files (checked by `build_plan.py`; allowed exceptions are listed in "
            "`ALLOWED_SHARED`).", "", "| Id | Owns |", "|---|---|"]
    out += [f"| {r['id']} | {'<br>'.join('`' + p + '`' for p in r['owns'])} |" for r in rows if r["owns"]]
    out += ["", f"No ownership yet (cannot be briefed): {len(unowned)} packages." if unowned else "All packages have ownership."]
    (HERE.parent / "PLAN.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"{len(rows)} packages, all {len(titles)} decisions covered; {dict(c)}")

    for a, b, hit in shared:
        why = next(w for x, y, w in ALLOWED_SHARED if frozenset((x, y)) == frozenset((a, b)))
        print(f"allowed shared: {a} / {b} ({why}); {len(hit)} overlapping pattern pair(s)")
    if unowned:
        print(f"WARNING: no ownership yet (cannot be briefed): {len(unowned)} packages: {', '.join(unowned)}")
    if bad:
        print(f"COLLISIONS: {len(bad)} concurrently runnable pair(s) own overlapping files")
        for a, b, hit in bad:
            print(f"  {a} / {b}: " + "; ".join(f"{x} ~ {y}" for x, y in hit[:4]) + (f" (+{len(hit) - 4} more)" if len(hit) > 4 else ""))
        return 1
    print("ownership check: no collisions")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--brief", metavar="ID", help="write docs/v2/briefs/<ID>.md from the template")
    ap.add_argument("--force", action="store_true", help="brief even if a dependency is not Done or OWNS is missing")
    args = ap.parse_args()
    sys.exit(write_brief(args.brief, args.force) if args.brief else main())
