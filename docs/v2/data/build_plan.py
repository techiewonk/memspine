"""Build docs/v2/data/plan.json and docs/v2/PLAN.md: the work-package plan with status.

    python docs/v2/data/build_plan.py

Every one of the 155 decisions must be covered by at least one package (checked here). Status values:
Not started, Ready, In progress, Blocked, Needs measurement, Done, Dropped.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
STATUSES = ("Not started", "Ready", "In progress", "Blocked", "Needs measurement", "Done", "Dropped")

# (id, phase, title, decisions, depends_on, kind, gate, status, note)
# kind: "move" = pure restructure (tests + CPU golden); "behaviour" = needs a GPU screen through the paired gate;
# "measure" = a run or screen; "infra" = tooling.
from packages import LANES, REVIEW, WP  # noqa: E402  (the package list lives in packages.py)


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


def main() -> None:
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
                     "lane": _lane(wid, kind, files), "agent": "sonnet",
                     "components": sorted({verify[d]["v2_component"] for d in dec if d in verify})[:4],
                     "review": REVIEW.get(wid, [])})
    ids = {r["id"] for r in rows}
    for r in rows:
        for d in r["depends_on"]:
            assert d in ids, (r["id"], d)
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
           "dependencies are Done.", "",
           "| Status | Count |", "|---|---|"] + [f"| {s} | {c.get(s, 0)} |" for s in STATUSES]
    for ph in sorted(phases):
        out += ["", f"## {phases[ph]}", "",
                "| Id | Work package | Layers | Ops | Decisions | Review | Depends on | Kind | Agent lane | Gate | Status | Note |",
                "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in rows:
            if r["phase"] == ph:
                out.append(f"| {r['id']} | {r['title']} | {' '.join(r['layers']) or '-'} | {' '.join(r['operators']) or '-'} | "
                           f"{', '.join(r['decisions'])} | {', '.join(r['review']) or '-'} | {', '.join(r['depends_on']) or '-'} | {r['kind']} | {r['lane']} | "
                           f"{r['gate']} | **{r['status']}** | {r['note']} |")
    (HERE.parent / "PLAN.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"{len(rows)} packages, all {len(titles)} decisions covered; {dict(c)}")


if __name__ == "__main__":
    main()
