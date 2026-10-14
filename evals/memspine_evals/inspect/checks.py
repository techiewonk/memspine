"""TI-6: invariant checks over an inspect bundle.

    python -m memspine_evals.inspect.checks <bundle_dir> [--fail-on error|warn|never]

A bundle is a directory of optional jsonl files (turns, windows, envelopes, records, links, sleep,
questions) plus an optional manifest.json (schema: docs/v2/TRACE_INSPECTOR_PLAN.md section 1).
Every check is a small registered function that declares the files it needs. A check whose inputs are
missing (or that finds no applicable row) reports ``skipped``; it never fails. Results go to
``checks.jsonl`` (check_id, scope, id, ok, detail, plus item/level/status) and ``checks_summary.md``
(counts per check and the first 10 failing ids, one section per item).

The module reads rows only; it never imports the engine and never needs a model.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from collections.abc import Callable, Iterable

FILES = ("turns", "windows", "envelopes", "records", "links", "sleep", "questions")
SESSION_END_CUTS = {"session_end", "session", "end_of_session", "session_close"}
SKIPPED_WINDOW = {"skip", "skipped", "no_extract"}
BASE_CHANNELS = {None, "", "verbatim", "turn", "raw"}
DEAD_STATUS = {"deleted", "erased", "purged"}
WALL_CLOCK_KEYS = ("run_started_at", "run_time", "created_at", "started_at", "run_date")
FIRST_N = 10


@dataclass
class CheckResult:
    check_id: str
    scope: str
    id: str
    ok: bool
    detail: str = ""
    item: str = "_"
    level: str = "error"          # error | warn
    status: str = "ok"            # ok | fail | skipped

    def row(self) -> dict:
        return asdict(self)


class Bundle:
    """Parsed bundle: rows per file name (None when the file is absent), manifest, parse errors."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.errors: list[tuple[str, int, str]] = []
        self.rows: dict[str, list[dict] | None] = {name: self._load(name) for name in FILES}
        self.manifest: dict = {}
        mf = self.path / "manifest.json"
        if mf.is_file():
            try:
                self.manifest = json.loads(mf.read_text(encoding="utf-8"))
            except ValueError as e:
                self.errors.append(("manifest", 0, str(e)))

    def _load(self, name: str) -> list[dict] | None:
        p = self.path / f"{name}.jsonl"
        if not p.is_file():
            return None
        out: list[dict] = []
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except ValueError as e:
                self.errors.append((name, n, str(e)))
                continue
            if isinstance(obj, dict):
                out.append(obj)
            else:
                self.errors.append((name, n, "row is not an object"))
        return out

    def get(self, name: str) -> list[dict]:
        return self.rows.get(name) or []


@dataclass
class _Check:
    check_id: str
    scope: str
    needs: tuple[str, ...]
    level: str
    fn: Callable[[Bundle], Iterable[tuple]]   # yields (item, id, ok, detail)


REGISTRY: dict[str, _Check] = {}


def check(check_id: str, scope: str, needs: tuple[str, ...] = (), level: str = "error"):
    """Register a check. The function yields (item, id, ok, detail) per inspected object."""
    def deco(fn):
        if check_id in REGISTRY:
            raise ValueError(f"duplicate check id {check_id}")
        REGISTRY[check_id] = _Check(check_id, scope, needs, level, fn)
        return fn
    return deco


# ----------------------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------------------

def _it(row: dict) -> str:
    return str(row.get("item", "_"))


def _parse_ts(v):
    if not v or not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _norm(s) -> str:
    return " ".join(str(s or "").lower().split())


def _turn_keys(b: Bundle) -> dict[tuple[str, str], dict]:
    return {(_it(t), str(t.get("turn_id"))): t for t in b.get("turns")}


def _records(b: Bundle) -> dict[tuple[str, str, str], dict]:
    """(item, phase, record_id) -> record."""
    return {(_it(r), str(r.get("phase", "")), str(r.get("record_id"))): r for r in b.get("records")}


def _is_derived(r: dict) -> bool:
    return bool(r.get("parents") or r.get("parent_turns")) or r.get("channel") not in BASE_CHANNELS


# ----------------------------------------------------------------------------------------------
# bundle
# ----------------------------------------------------------------------------------------------

@check("bundle_wellformed", "bundle")
def _bundle_wellformed(b: Bundle):
    """Invariant: every line of every bundle file parses as a JSON object."""
    for name, n, msg in b.errors:
        yield "_", f"{name}:{n}", False, msg
    if not b.errors:
        yield "_", "*", True, "all files parse"


# ----------------------------------------------------------------------------------------------
# turns
# ----------------------------------------------------------------------------------------------

@check("turn_written_once", "turn", ("turns",))
def _turn_written_once(b: Bundle):
    """Invariant: each (item, turn_id) appears once, has a record_id unless quarantined/dropped, and no
    record_id is shared by two turns."""
    seen: dict[tuple[str, str], int] = defaultdict(int)
    by_rid: dict[tuple[str, str], list[str]] = defaultdict(list)
    for t in b.get("turns"):
        k = (_it(t), str(t.get("turn_id")))
        seen[k] += 1
        if t.get("record_id"):
            by_rid[(_it(t), str(t["record_id"]))].append(k[1])
    for t in b.get("turns"):
        k = (_it(t), str(t.get("turn_id")))
        probs = []
        if seen[k] > 1:
            probs.append(f"turn listed {seen[k]} times")
        rid = t.get("record_id")
        if not rid:
            fw = t.get("firewall") or {}
            if not (fw.get("quarantined") or str(t.get("deposit_action", "")).lower() in {"dropped", "quarantined", "skipped"}):
                probs.append("no record_id and not quarantined/dropped")
        elif len(by_rid[(k[0], str(rid))]) > 1:
            probs.append(f"record {rid} shared by turns {by_rid[(k[0], str(rid))]}")
        yield k[0], k[1], not probs, "; ".join(probs) or "written once"


@check("turn_in_vector_index", "turn", ("turns",))
def _turn_in_vector_index(b: Bundle):
    """Invariant: every written (record_id set) turn is in the vector index (indexed.vector is true)."""
    for t in b.get("turns"):
        if not t.get("record_id"):
            continue
        v = (t.get("indexed") or {}).get("vector")
        yield _it(t), str(t.get("turn_id")), bool(v), "vector indexed" if v else f"indexed.vector={v!r}"


@check("turn_in_lexical_index", "turn", ("turns",))
def _turn_in_lexical_index(b: Bundle):
    """Invariant: when hybrid is on (some turn reports indexed.lexical), every written turn is lexically indexed."""
    ts = [t for t in b.get("turns") if t.get("record_id")]
    if not any((t.get("indexed") or {}).get("lexical") is not None for t in ts):
        return
    for t in ts:
        v = (t.get("indexed") or {}).get("lexical")
        yield _it(t), str(t.get("turn_id")), bool(v), "lexical indexed" if v else f"indexed.lexical={v!r}"


# ----------------------------------------------------------------------------------------------
# windows
# ----------------------------------------------------------------------------------------------

def _window_skipped(w: dict) -> bool:
    return (str((w.get("prepass") or {}).get("verdict", "")).lower() in SKIPPED_WINDOW
            or str(w.get("status", "")).lower() in SKIPPED_WINDOW)


@check("window_has_members", "window", ("windows",))
def _window_has_members(b: Bundle):
    """Invariant: every non-skipped window lists at least one member turn."""
    for w in b.get("windows"):
        if _window_skipped(w):
            continue
        ok = bool(w.get("member_turns"))
        yield _it(w), str(w.get("window_id")), ok, "members present" if ok else "no member_turns"


@check("window_call_outcome", "window", ("windows",))
def _window_call_outcome(b: Bundle):
    """Invariant: every non-skipped window records a model call outcome (call has tokens, cached, ms or error)."""
    for w in b.get("windows"):
        if _window_skipped(w):
            continue
        c = w.get("call") or {}
        ok = any(k in c for k in ("tokens", "cached", "error", "ms"))
        yield _it(w), str(w.get("window_id")), ok, "call outcome recorded" if ok else "empty call"


@check("window_sections_counted", "window", ("windows",))
def _window_sections_counted(b: Bundle):
    """Invariant: every non-skipped window has integer counts for sections facts, edges and episode."""
    for w in b.get("windows"):
        if _window_skipped(w):
            continue
        s = w.get("sections") or {}
        bad = [k for k in ("facts", "edges", "episode") if not isinstance(s.get(k), int) or isinstance(s.get(k), bool)]
        yield _it(w), str(w.get("window_id")), not bad, "sections counted" if not bad else f"uncounted sections: {bad}"


@check("window_single_session_at_session_end", "window", ("windows", "turns"))
def _window_single_session(b: Bundle):
    """Invariant: a window cut by session end does not mix members from two sessions."""
    tk = _turn_keys(b)
    for w in b.get("windows"):
        if str(w.get("cut_reason", "")).lower() not in SESSION_END_CUTS:
            continue
        sess = {str(tk[(_it(w), str(m))].get("session")) for m in w.get("member_turns") or [] if (_it(w), str(m)) in tk}
        ok = len(sess) <= 1
        yield _it(w), str(w.get("window_id")), ok, "one session" if ok else f"spans sessions {sorted(sess)}"


# ----------------------------------------------------------------------------------------------
# envelopes
# ----------------------------------------------------------------------------------------------

@check("envelope_terminal", "envelope", ("envelopes",))
def _envelope_terminal(b: Bundle):
    """Invariant: every envelope (by occ) ends deposited with a record_id, or dropped with a reason code."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for e in b.get("envelopes"):
        groups[(_it(e), str(e.get("occ")))].append(e)
    for (item, occ), rows in groups.items():
        dropped = [e for e in rows if e.get("status") == "dropped"]
        if dropped:
            ok = all(e.get("reason") for e in dropped)
            yield item, occ, ok, "dropped with reason" if ok else "dropped without reason"
            continue
        dep = [e for e in rows if e.get("stage") == "deposit" and e.get("record_id")]
        yield item, occ, bool(dep), "deposited" if dep else "neither deposited nor dropped"


@check("supersede_has_target", "envelope", ("envelopes",))
def _supersede_has_target(b: Bundle):
    """Invariant: an envelope whose integrate.op is supersede names a target."""
    for e in b.get("envelopes"):
        ig = e.get("integrate") or {}
        if ig.get("op") == "supersede":
            ok = bool(ig.get("target"))
            yield _it(e), f"{e.get('occ')}@{e.get('stage')}", ok, "target set" if ok else "supersede without target"


# ----------------------------------------------------------------------------------------------
# records
# ----------------------------------------------------------------------------------------------

@check("derived_has_live_parent_and_channel", "record", ("records",))
def _derived_parent_channel(b: Bundle):
    """Invariant: every derived record has a channel and at least one live parent (a stored, non-erased
    record in the same phase, or a known source turn)."""
    recs = _records(b)
    tk = _turn_keys(b)
    for r in b.get("records"):
        if not _is_derived(r):
            continue
        item, phase = _it(r), str(r.get("phase", ""))
        probs = []
        if not r.get("channel"):
            probs.append("no channel")
        live = [p for p in r.get("parents") or []
                if (item, phase, str(p)) in recs and str(recs[(item, phase, str(p))].get("status", "")).lower() not in DEAD_STATUS]
        live_turns = [t for t in r.get("parent_turns") or [] if (item, str(t)) in tk]
        if not live and not live_turns:
            probs.append("no live parent")
        yield item, f"{phase}:{r.get('record_id')}", not probs, "; ".join(probs) or "ok"


@check("no_parent_cycles", "record", ("records",))
def _no_parent_cycles(b: Bundle):
    """Invariant: the parent relation (records[].parents) is acyclic."""
    graph = {k: [(k[0], k[1], str(p)) for p in r.get("parents") or []] for k, r in _records(b).items()}
    color: dict = {}
    cyclic: set = set()
    for start in graph:
        if start in color:
            continue
        color[start] = 1
        path = [start]
        stack = [iter(graph[start])]
        while stack:
            nxt = next(stack[-1], None)
            if nxt is None:
                color[path.pop()] = 2
                stack.pop()
                continue
            if nxt not in graph:
                continue
            if color.get(nxt) == 1:
                cyclic.update(path[path.index(nxt):])
            elif nxt not in color:
                color[nxt] = 1
                path.append(nxt)
                stack.append(iter(graph[nxt]))
    for k in graph:
        yield k[0], f"{k[1]}:{k[2]}", k not in cyclic, "acyclic" if k not in cyclic else "on a parent cycle"


@check("supersede_target_and_time_order", "record", ("records",))
def _supersede_time(b: Bundle):
    """Invariant: a superseded record (evolve_to set) points at a stored record, and the successor's
    valid_from is not before the predecessor's valid_from nor before the predecessor's valid_to."""
    recs = _records(b)
    for r in b.get("records"):
        tgt = r.get("evolve_to")
        if not tgt and str(r.get("status", "")).lower() != "superseded":
            continue
        item, phase = _it(r), str(r.get("phase", ""))
        rid = f"{phase}:{r.get('record_id')}"
        succ = recs.get((item, phase, str(tgt))) if tgt else None
        if succ is None:
            yield item, rid, False, f"evolve_to {tgt!r} not found"
            continue
        a, s = _parse_ts(r.get("valid_from")), _parse_ts(succ.get("valid_from"))
        end = _parse_ts(r.get("valid_to"))
        bad = []
        if a and s and s < a:
            bad.append("successor valid_from precedes predecessor valid_from")
        if end and s and s < end:
            bad.append("successor valid_from precedes predecessor valid_to")
        yield item, rid, not bad, "; ".join(bad) or "ordered"


@check("duplicate_record_content", "turn", ("records",), level="warn")
def _dup_content(b: Bundle):
    """Invariant: one source turn does not yield two derived records (same phase) with identical normalised content."""
    turn_of_rid = {(_it(t), str(t["record_id"])): str(t.get("turn_id")) for t in b.get("turns") if t.get("record_id")}
    groups: dict[tuple, set[str]] = defaultdict(set)
    for r in b.get("records"):
        if not r.get("content") or not _is_derived(r):
            continue
        item, rid = _it(r), str(r.get("record_id"))
        turns = [str(t) for t in r.get("parent_turns") or []] or ([turn_of_rid[(item, rid)]] if (item, rid) in turn_of_rid else [])
        for t in turns:
            groups[(item, str(r.get("phase", "")), t, _norm(r["content"]))].add(rid)
    for (item, phase, t, _), rids in groups.items():
        ok = len(rids) == 1
        yield item, f"{phase}:{t}", ok, "unique" if ok else f"duplicate content in records {sorted(rids)}"


@check("derived_parent_turns_in_item", "record", ("records", "turns"))
def _parent_turns_in_item(b: Bundle):
    """Invariant: every parent_turns entry of a derived record is a turn of the same item."""
    tk = _turn_keys(b)
    for r in b.get("records"):
        if not _is_derived(r) or not r.get("parent_turns"):
            continue
        item = _it(r)
        out = [str(t) for t in r["parent_turns"] if (item, str(t)) not in tk]
        yield item, f"{r.get('phase', '')}:{r.get('record_id')}", not out, "in item" if not out else f"outside item: {out}"


def _run_date(b: Bundle):
    for k in WALL_CLOCK_KEYS:
        v = b.manifest.get(k)
        if isinstance(v, str) and len(v) >= 10:
            return v[:10]
    return None


@check("no_wall_clock_in_derived", "record", ("records",))
def _no_wall_clock(b: Bundle):
    """Invariant: no derived record carries the run date as valid_from/recorded time when its source
    turns are on another date (R14-decon-2 / time-2). Needs a run time in manifest.json."""
    run = _run_date(b)
    if run is None:
        return
    ts_date = {(_it(t), str(t.get("turn_id"))): str(t.get("ts") or (t.get("stamp") or {}).get("source_at") or "")[:10]
               for t in b.get("turns")}
    for r in b.get("records"):
        if not _is_derived(r):
            continue
        rid = f"{r.get('phase', '')}:{r.get('record_id')}"
        hits = [k for k in ("valid_from", "recorded_at", "created_at", "recorded") if str(r.get(k) or "")[:10] == run]
        if not hits:
            yield _it(r), rid, True, "no run-time stamp"
            continue
        src = {ts_date.get((_it(r), str(t)), "") for t in r.get("parent_turns") or []}
        ok = run in src
        yield _it(r), rid, ok, ("run date equals a source turn date" if ok
                                else f"{hits} equals run date {run}, source dates {sorted(src - {''})}")


# ----------------------------------------------------------------------------------------------
# links
# ----------------------------------------------------------------------------------------------

@check("graph_edge_endpoints_exist", "record", ("links", "records"))
def _edge_endpoints(b: Bundle):
    """Invariant: every edge endpoint is a stored record, a source turn or a declared node (kind 'node');
    for 'mentions' edges only src is required."""
    known: dict[str, set[str]] = defaultdict(set)
    for r in b.get("records"):
        known[_it(r)].add(str(r.get("record_id")))
    for t in b.get("turns"):
        known[_it(t)].add(str(t.get("turn_id")))
        if t.get("record_id"):
            known[_it(t)].add(str(t["record_id"]))
    for ln in b.get("links"):
        if ln.get("kind") == "node":
            known[_it(ln)].add(str(ln.get("src") or ln.get("id")))
    for n, ln in enumerate(b.get("links")):
        kind = ln.get("kind")
        if kind == "node":
            continue
        ends = [("src", ln.get("src"))] + ([] if kind == "mentions" else [("dst", ln.get("dst"))])
        miss = [f"{k}={v}" for k, v in ends if str(v) not in known[_it(ln)]]
        yield _it(ln), f"{kind}:{ln.get('src')}->{ln.get('dst')}#{n}", not miss, "endpoints exist" if not miss else f"missing {miss}"


# ----------------------------------------------------------------------------------------------
# sleep
# ----------------------------------------------------------------------------------------------

@check("sleep_idempotent", "record", ("sleep",))
def _sleep_idempotent(b: Bundle):
    """Invariant: a later sleep cycle over the same stage and session adds nothing (empty output_ids)."""
    cycles: dict[tuple, set] = defaultdict(set)
    for s in b.get("sleep"):
        if isinstance(s.get("cycle"), int):
            cycles[(_it(s), s.get("stage"), s.get("session_key"))].add(s["cycle"])
    for s in b.get("sleep"):
        k = (_it(s), s.get("stage"), s.get("session_key"))
        cyc = s.get("cycle")
        if len(cycles[k]) < 2 or not isinstance(cyc, int) or cyc <= min(cycles[k]):
            continue
        out = s.get("output_ids") or []
        yield k[0], f"{k[1]}/{k[2]}/cycle{cyc}", not out, "adds nothing" if not out else f"later cycle added {len(out)} item(s)"


# ----------------------------------------------------------------------------------------------
# questions
# ----------------------------------------------------------------------------------------------

@check("question_has_plan", "question", ("questions",))
def _q_plan(b: Bundle):
    """Invariant: every question carries a non-empty plan."""
    for q in b.get("questions"):
        ok = bool(q.get("plan"))
        yield _it(q), str(q.get("query_id")), ok, "plan present" if ok else "no plan"


@check("question_has_leg", "question", ("questions",))
def _q_leg(b: Bundle):
    """Invariant: every question ran at least one retrieval leg."""
    for q in b.get("questions"):
        ok = bool(q.get("legs"))
        yield _it(q), str(q.get("query_id")), ok, f"{len(q.get('legs') or {})} leg(s)" if ok else "no legs"


@check("question_context_within_budget", "question", ("questions",))
def _q_budget(b: Bundle):
    """Invariant: the packed context tokens (context_tokens, else the sum of packed[].tokens) do not exceed
    the budget (question.budget, else manifest context_budget / budget_tokens)."""
    default = b.manifest.get("context_budget") or b.manifest.get("budget_tokens")
    for q in b.get("questions"):
        budget = q.get("budget") or q.get("context_budget") or default
        used = q.get("context_tokens")
        if used is None and q.get("packed"):
            used = sum(int(p.get("tokens") or 0) for p in q["packed"])
        if budget is None or used is None:
            continue
        yield _it(q), str(q.get("query_id")), used <= budget, f"{used}/{budget} tokens"


def _gold_stage(q: dict, turn) -> str | None:
    """Last stage (leg, fused, pool, rerank, packed) in which the turn appears in the question's lists."""
    t = str(turn)

    def has(rows):
        return any(str((x or {}).get("turn", (x or {}).get("rid"))) == t for x in rows or [])
    last = "leg" if any(has(v) for v in (q.get("legs") or {}).values()) else None
    for name in ("fused", "pool", "rerank", "packed"):
        if has(q.get(name)):
            last = name
    return last


@check("gold_path_matches_stage_lists", "question", ("questions",))
def _gold_path(b: Bundle):
    """Invariant: gold_path covers every gold turn, and each last_stage_reached equals the last stage
    (leg, fused, pool, rerank, packed) in which that turn appears in the question's own stage lists, the
    same attribution forensics_report.py uses for its stage counts."""
    for q in b.get("questions"):
        gp = q.get("gold_path")
        if gp is None or not q.get("gold_turns"):
            continue
        by_turn = {str(g.get("turn")): g.get("last_stage_reached") for g in gp}
        probs = []
        for t in q["gold_turns"]:
            if str(t) not in by_turn:
                probs.append(f"{t}: no gold_path entry")
                continue
            want, got = _gold_stage(q, t), by_turn[str(t)]
            if got in (None, "none", "ingest"):
                got = None
            if got != want:
                probs.append(f"{t}: reported {by_turn[str(t)]!r}, lists say {want!r}")
        yield _it(q), str(q.get("query_id")), not probs, "; ".join(probs) or "consistent"


# ----------------------------------------------------------------------------------------------
# runner
# ----------------------------------------------------------------------------------------------

def summarise(results: list[CheckResult]) -> str:
    """Markdown summary: per item, counts per check and the first 10 failing ids."""
    errs = sum(1 for r in results if r.status == "fail" and r.level == "error")
    warns = sum(1 for r in results if r.status == "fail" and r.level == "warn")
    lines = ["# Inspect bundle checks", "", f"Total: {errs} error failure(s), {warns} warning(s).", ""]
    items = sorted({r.item for r in results if r.item != "_"}) or ["_"]
    for item in items:
        lines += [f"## Item {item}", "", "| check | level | ok | fail | first failing ids |", "|---|---|---|---|---|"]
        for cid in REGISTRY:
            rows = [r for r in results if r.check_id == cid and r.item == item and r.status != "skipped"]
            if rows:
                bad = [r.id for r in rows if not r.ok][:FIRST_N]
                lines.append(f"| {cid} | {REGISTRY[cid].level} | {sum(r.ok for r in rows)} | "
                             f"{sum(not r.ok for r in rows)} | {', '.join(bad)} |")
        lines.append("")
    bundle_fail = [r for r in results if r.item == "_" and r.status == "fail"]
    if bundle_fail:
        lines += ["## Bundle-level failures", ""] + [f"- {r.check_id} {r.id}: {r.detail}" for r in bundle_fail[:FIRST_N]] + [""]
    skipped = [r for r in results if r.status == "skipped"]
    if skipped:
        lines += ["## Skipped", ""] + [f"- {r.check_id}: {r.detail}" for r in skipped] + [""]
    return "\n".join(lines)


def run_checks(bundle_dir, write: bool = True) -> list[CheckResult]:
    """Run every registered check; write checks.jsonl and checks_summary.md into the bundle unless write=False."""
    b = Bundle(Path(bundle_dir))
    out: list[CheckResult] = []
    for c in REGISTRY.values():
        missing = [n for n in c.needs if b.rows.get(n) is None]
        if missing:
            out.append(CheckResult(c.check_id, c.scope, "*", True, f"skipped: missing {', '.join(missing)}", "_", c.level, "skipped"))
            continue
        n0 = len(out)
        for item, rid, ok, detail in c.fn(b):
            out.append(CheckResult(c.check_id, c.scope, str(rid), bool(ok), detail, item, c.level, "ok" if ok else "fail"))
        if len(out) == n0:
            out.append(CheckResult(c.check_id, c.scope, "*", True, "skipped: no applicable rows", "_", c.level, "skipped"))
    if write:
        d = Path(bundle_dir)
        with (d / "checks.jsonl").open("w", encoding="utf-8", newline="\n") as f:
            for r in out:
                f.write(json.dumps(r.row(), ensure_ascii=False) + "\n")
        (d / "checks_summary.md").write_text(summarise(out), encoding="utf-8", newline="\n")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m memspine_evals.inspect.checks",
                                 description="Run the TI-6 invariant checks over an inspect bundle.")
    ap.add_argument("bundle_dir")
    ap.add_argument("--fail-on", choices=("error", "warn", "never"), default="never",
                    help="exit 1 when a check of this level fails (error: errors only; warn: errors or warnings)")
    a = ap.parse_args(argv)
    d = Path(a.bundle_dir)
    if not d.is_dir():
        print(f"not a directory: {d}", file=sys.stderr)
        return 2
    res = run_checks(d)
    fails = [r for r in res if r.status == "fail"]
    e = sum(r.level == "error" for r in fails)
    print(f"{len(res)} results: {e} error failure(s), {len(fails) - e} warning(s), "
          f"{sum(r.status == 'skipped' for r in res)} skipped -> {d / 'checks.jsonl'}")
    if a.fail_on == "error" and e:
        return 1
    if a.fail_on == "warn" and fails:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
