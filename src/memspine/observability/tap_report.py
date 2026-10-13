"""Wiretap gap analysis (WT-01): what went wrong or was missing in each traced request.

Gap rules are data (``RULES``): an event stage glob list plus field predicates. To cover a new
event a wiring agent adds one :class:`Rule`; no analysis code changes. Usage::

    python -m memspine.observability.tap_report <dir|file.jsonl|tap.sqlite> [--request ID]
        [--html out.html] [--since 2h|1d|2026-10-13] [--json]
"""

from __future__ import annotations

import argparse
import fnmatch
import html
import json
import sqlite3
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

__all__ = [
    "RULES",
    "SLO_MS",
    "Cond",
    "Gap",
    "Rule",
    "analyse",
    "flatten",
    "load_traces",
    "main",
    "render_html",
    "render_text",
    "summary_table",
]


@dataclass(frozen=True, slots=True)
class Cond:
    """``field op value``; ops: eq ne gt ge lt le truthy falsy present absent in."""

    field: str
    op: str
    value: Any = None

    def holds(self, fields: dict[str, Any]) -> bool:
        has = self.field in fields
        v = fields.get(self.field)
        match self.op:
            case "present":
                return has and v is not None
            case "absent":
                return not has or v is None
            case "truthy":
                return bool(v)
            case "falsy":
                return has and not v
            case "in":
                return has and v in self.value
        if not has or v is None:
            return False
        try:
            return {
                "eq": v == self.value,
                "ne": v != self.value,
                "gt": v > self.value,
                "ge": v >= self.value,
                "lt": v < self.value,
                "le": v <= self.value,
            }[self.op]
        except TypeError:
            return False


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    severity: str  # info | warn | error
    stages: tuple[str, ...]  # fnmatch globs on the event stage
    message: str
    when: tuple[Cond, ...] = ()  # all must hold; empty = every matching event is a gap
    detail_fields: tuple[str, ...] = ("reason", "id", "ids", "n", "count", "error", "leg")


_LEG = ("read.leg.*", "read.legs.*", "read.*.leg", "read.*.leg.*")
RULES: tuple[Rule, ...] = (
    Rule("empty_leg", "warn", _LEG, "retrieval leg returned nothing", (Cond("n", "eq", 0),)),
    Rule("empty_leg", "warn", _LEG, "retrieval leg returned nothing", (Cond("count", "eq", 0),)),
    Rule(
        "leg_skipped",
        "info",
        ("read.*.skipped", "read.leg.skipped", "read.*.skip"),
        "stage or leg skipped",
    ),
    Rule(
        "dropped",
        "info",
        ("*.dropped", "*.cut", "*.drop", "read.forensics.cut", "*.gate.*dropped*"),
        "items dropped (gate / floor / budget / visibility)",
    ),
    Rule("unscored", "warn", ("*.unscored", "*.unscored.*"), "items reached ranking unscored"),
    Rule(
        "unscored",
        "warn",
        ("read.*",),
        "items reached ranking unscored",
        (Cond("unscored", "gt", 0),),
    ),
    Rule(
        "rerank_skipped",
        "info",
        ("read.rerank", "read.rerank.*"),
        "rerank skipped",
        (Cond("skipped", "truthy"),),
    ),
    Rule("rerank_skipped", "info", ("read.rerank.skipped",), "rerank skipped"),
    Rule(
        "missing_capability",
        "warn",
        ("*.capability.missing", "*.missing_capability", "*.partial", "*.partial.*"),
        "capability missing / partial read",
    ),
    Rule(
        "missing_capability",
        "warn",
        ("*",),
        "capability missing / partial read",
        (Cond("missing_capabilities", "present"),),
    ),
    Rule("partial_read", "warn", ("read.*",), "partial read", (Cond("partial", "truthy"),)),
    Rule(
        "abstention",
        "info",
        ("*.abstain", "*.abstain.*", "*.abstained", "read.forensics.abstain*"),
        "read abstained",
    ),
    Rule("abstention", "info", ("read.*",), "read abstained", (Cond("abstained", "truthy"),)),
    Rule(
        "model_error",
        "error",
        ("*",),
        "model call error",
        (Cond("outcome", "in", ("error", "failed")),),
    ),
    Rule("model_error", "error", ("*",), "model call error", (Cond("error", "present"),)),
    Rule(
        "model_timeout",
        "error",
        ("*",),
        "model call timed out",
        (Cond("outcome", "eq", "timeout"),),
    ),
    Rule("model_timeout", "error", ("*",), "model call timed out", (Cond("timeout", "truthy"),)),
    Rule("model_retry", "warn", ("*",), "model call retried", (Cond("retries", "gt", 0),)),
    Rule("model_retry", "warn", ("*",), "model call retried", (Cond("outcome", "eq", "retried"),)),
    Rule(
        "quarantine",
        "warn",
        ("*.quarantine", "*.quarantine.*", "*.quarantined"),
        "record quarantined",
    ),
    Rule(
        "extraction_rejected",
        "warn",
        ("*.extraction.rejected", "ingest.*.rejected", "write.*.rejected", "*.rejected"),
        "extraction / write rejected",
    ),
    Rule(
        "coverage_hole",
        "warn",
        ("*.coverage_hole", "*.coverage.hole", "*.coverage.missing"),
        "coverage hole",
    ),
    Rule(
        "coverage_hole",
        "warn",
        ("*.coverage*",),
        "coverage hole",
        (Cond("covered", "eq", False),),
    ),
    Rule(
        "field_rejected",
        "warn",
        ("*",),
        "event carried free text or non-id values (dropped by the guard)",
        (Cond("field_rejected", "present"),),
        ("field_rejected",),
    ),
    # -- WT-02: write and derive paths ---------------------------------------------------
    Rule(
        "extraction_rejected",
        "warn",
        ("write.extract.facts", "write.extract.edges", "write.extract.episode"),
        "extraction items rejected (invalid / ungrounded / uncited)",
        (Cond("invalid", "gt", 0),),
        ("invalid", "ungrounded", "uncited", "returned"),
    ),
    Rule(
        "extraction_rejected",
        "warn",
        ("write.extract.facts", "write.extract.edges", "write.extract.episode"),
        "extraction items rejected (invalid / ungrounded / uncited)",
        (Cond("ungrounded", "gt", 0),),
        ("invalid", "ungrounded", "uncited", "returned"),
    ),
    Rule(
        "extraction_rejected",
        "warn",
        ("write.extract.facts", "write.extract.episode"),
        "extraction items rejected (invalid / ungrounded / uncited)",
        (Cond("uncited", "gt", 0),),
        ("invalid", "ungrounded", "uncited", "returned"),
    ),
    Rule(
        "extraction_failed",
        "error",
        ("write.extract.failed", "write.window.job_failed"),
        "window extraction call failed",
    ),
    Rule(
        "stage_failed",
        "error",
        ("job.stage.end",),
        "background stage failed",
        (Cond("failed", "truthy"),),
        ("stage", "status", "error"),
    ),
    Rule(
        "stage_failed",
        "error",
        ("job.stage.end",),
        "background stage raised",
        (Cond("status", "in", ("error", "raised")),),
        ("stage", "status", "error"),
    ),
    Rule(
        "stage_partial",
        "warn",
        ("job.stage.end",),
        "background stage partial",
        (Cond("status", "eq", "partial"),),
        ("stage", "status"),
    ),
    Rule(
        "stage_skipped",
        "info",
        ("job.stage.end",),
        "background stage skipped",
        (Cond("skipped", "truthy"),),
        ("stage", "status"),
    ),
    Rule(
        "stage_skipped",
        "info",
        ("write.*.skipped",),
        "write step skipped",
        (),
        ("reason", "turn", "role"),
    ),
    Rule(
        "firewall_flag",
        "warn",
        ("write.firewall.verdict",),
        "firewall flagged the write (anomaly / instruction)",
        (Cond("anomalous", "truthy"),),
        ("id", "reasons", "trust"),
    ),
    Rule(
        "firewall_flag",
        "warn",
        ("write.firewall.verdict",),
        "firewall flagged the write (anomaly / instruction)",
        (Cond("instruction_flag", "truthy"),),
        ("id", "reasons", "trust"),
    ),
    Rule(
        "write_replayed",
        "info",
        ("write.intake.received",),
        "idempotent replay: nothing new was written",
        (Cond("replayed", "truthy"),),
        ("source_id", "kind", "segments"),
    ),
    Rule(
        "time_unanchored",
        "warn",
        ("write.time.parsed",),
        "event time without a timezone (assumed)",
        (Cond("unknown_tz", "truthy"),),
        ("assumed",),
    ),
    Rule("time_unparsed", "warn", ("write.time.unparsed",), "event time not parseable"),
    Rule(
        "window_anomaly",
        "info",
        ("write.window.cut",),
        "window cut with a single turn",
        (Cond("turns", "lt", 2),),
        ("reason", "turns", "tokens"),
    ),
    Rule(
        "window_anomaly",
        "warn",
        ("write.window.skipped",),
        "window had no live turns",
        (),
        ("reason", "turns"),
    ),
)

#: slow-stage thresholds in ms by stage glob (first match wins); events with ``duration_ms``
#: are checked.
SLO_MS: tuple[tuple[str, float], ...] = (
    ("model.call", 60000.0),
    ("job.stage.*", 120000.0),
    ("*.rerank*", 800.0),
    ("*.embed*", 500.0),
    ("*.llm*", 8000.0),
    ("*.extract*", 8000.0),
    ("read.*", 300.0),
    ("*", 2000.0),
)


@dataclass(slots=True)
class Gap:
    request_id: str
    kind: str
    seq: int
    stage: str
    rule: str
    severity: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


# -- loading ---------------------------------------------------------------------------------
def _parse_since(text: str | None) -> datetime | None:
    if not text:
        return None
    unit = {"m": "minutes", "h": "hours", "d": "days"}.get(text[-1])
    if unit and text[:-1].isdigit():
        return datetime.now(UTC) - timedelta(**{unit: int(text[:-1])})
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _load_sqlite(path: Path) -> list[dict[str, Any]]:
    db = sqlite3.connect(path)
    try:
        reqs: dict[str, dict[str, Any]] = {}
        for r in db.execute(
            "SELECT id,parent_id,kind,scope_hash,started,duration_ms,fields FROM requests"
        ):
            reqs[r[0]] = {
                "id": r[0],
                "parent_id": r[1],
                "kind": r[2],
                "scope_hash": r[3],
                "started": r[4],
                "duration_ms": r[5],
                "fields": json.loads(r[6] or "{}"),
                "events": [],
                "children": [],
            }
        for rid, seq, t, stage, f in db.execute(
            "SELECT request_id,seq,t_ms,stage,fields FROM events ORDER BY request_id,seq"
        ):
            if rid in reqs:
                reqs[rid]["events"].append(
                    {"seq": seq, "t_ms": t, "stage": stage, "fields": json.loads(f or "{}")}
                )
    finally:
        db.close()
    roots: list[dict[str, Any]] = []
    for r in reqs.values():
        parent = reqs.get(r["parent_id"]) if r["parent_id"] else None
        (parent["children"] if parent else roots).append(r)
    return roots


def load_traces(path: str | Path, *, since: str | None = None) -> list[dict[str, Any]]:
    """Load root traces from a jsonl file, or a directory of ``tap-*.jsonl`` / ``*.sqlite``."""
    p = Path(path)
    out: list[dict[str, Any]] = []
    files = [p] if p.is_file() else sorted(p.glob("tap-*.jsonl")) + sorted(p.glob("*.sqlite"))
    for f in files:
        if f.suffix == ".sqlite":
            out.extend(_load_sqlite(f))
        else:
            with f.open(encoding="utf-8") as fh:
                out.extend(json.loads(line) for line in fh if line.strip())
    cut = _parse_since(since)
    if cut:
        out = [t for t in out if t.get("started") and datetime.fromisoformat(t["started"]) >= cut]
    return out


def flatten(traces: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Roots and all nested children as one list (each keeps ``parent_id``)."""
    out: list[dict[str, Any]] = []

    def walk(t: dict[str, Any]) -> None:
        out.append(t)
        for c in t.get("children", []):
            walk(c)

    for t in traces:
        walk(t)
    return out


# -- analysis --------------------------------------------------------------------------------
def _slo_for(stage: str) -> float:
    for glob, ms in SLO_MS:
        if fnmatch.fnmatch(stage, glob):
            return ms
    return float("inf")


def analyse(
    traces: Iterable[dict[str, Any]], rules: Sequence[Rule] = RULES
) -> dict[str, list[Gap]]:
    """Map request id to its gaps (every request present, possibly with an empty list)."""
    result: dict[str, list[Gap]] = {}
    for t in flatten(traces):
        gaps = result.setdefault(t["id"], [])
        if not t["events"] and not t.get("children"):
            gaps.append(
                Gap(
                    t["id"], t["kind"], -1, "-", "no_events", "warn",
                    "request recorded no events (unwired path?)",
                )
            )  # fmt: skip
        for ev in t["events"]:
            stage, f = ev["stage"], ev["fields"]
            seen: set[str] = set()
            for r in rules:
                if r.id in seen or not any(fnmatch.fnmatch(stage, g) for g in r.stages):
                    continue
                if all(c.holds(f) for c in r.when):
                    seen.add(r.id)
                    detail = {k: f[k] for k in r.detail_fields if k in f}
                    gaps.append(
                        Gap(
                            t["id"],
                            t["kind"],
                            ev["seq"],
                            stage,
                            r.id,
                            r.severity,
                            r.message,
                            detail,
                        )
                    )
            ms = f.get("duration_ms")
            if isinstance(ms, (int, float)) and ms > (slo := _slo_for(stage)):
                gaps.append(
                    Gap(
                        t["id"], t["kind"], ev["seq"], stage, "slow_stage", "warn",
                        f"{ms:.0f} ms over the {slo:.0f} ms SLO", {"duration_ms": ms},
                    )
                )  # fmt: skip
    return result


def summary_table(
    traces: Sequence[dict[str, Any]], gaps: dict[str, list[Gap]]
) -> list[dict[str, Any]]:
    """One row per gap rule: requests affected, occurrences, severity."""
    total = len(flatten(traces))
    rows: dict[str, dict[str, Any]] = {}
    for rid, gl in gaps.items():
        for g in gl:
            row = rows.setdefault(
                g.rule,
                {"rule": g.rule, "severity": g.severity, "occurrences": 0, "requests": set()},
            )
            row["occurrences"] += 1
            row["requests"].add(rid)
    out = [{**r, "requests": len(r["requests"]), "of": total} for r in rows.values()]
    return sorted(out, key=lambda r: (-r["requests"], r["rule"]))


def _aggregate_drops(gl: list[Gap]) -> list[str]:
    reasons: Counter[str] = Counter()
    ids: dict[str, list[str]] = {}
    for g in gl:
        if g.rule != "dropped":
            continue
        r = str(g.detail.get("reason", g.stage))
        n = g.detail.get("n") or g.detail.get("count") or 1
        reasons[r] += int(n) if isinstance(n, (int, float)) else 1
        listed = [g.detail["id"]] if "id" in g.detail else g.detail.get("ids", [])
        ids.setdefault(r, []).extend(str(i) for i in listed)
    return [f"dropped by {r}: {n} ({', '.join(ids.get(r, [])[:8])})" for r, n in reasons.items()]


def render_text(traces: Sequence[dict[str, Any]], gaps: dict[str, list[Gap]]) -> str:
    lines: list[str] = []
    for t in flatten(traces):
        gl = gaps.get(t["id"], [])
        lines.append(
            f"{t['id']} {t['kind']:<6} {t['duration_ms']:>8.1f} ms  events={len(t['events'])}  "
            f"gaps={len(gl)}  {t['started']}"
        )
        for g in gl:
            if g.rule != "dropped":
                extra = f" {g.detail}" if g.detail else ""
                lines.append(f"    [{g.severity}] {g.rule} @ {g.stage}: {g.message}{extra}")
        lines.extend(f"    [info] {d}" for d in _aggregate_drops(gl))
    lines.append("")
    lines.append(f"{'rule':<22}{'severity':<9}{'requests':>9}{'occurrences':>13}")
    for r in summary_table(traces, gaps):
        req = f"{r['requests']}/{r['of']}"
        lines.append(f"{r['rule']:<22}{r['severity']:<9}{req:>9}{r['occurrences']:>13}")
    return "\n".join(lines)


_CSS = (
    "body{font:14px system-ui,sans-serif;margin:1.5rem;color:#222}table{border-collapse:collapse}"
    "td,th{border:1px solid #ccc;padding:2px 6px;text-align:left;vertical-align:top}"
    "details{margin:.4rem 0}summary{cursor:pointer}tr.gap td,summary.gap{background:#fff3cd}"
    "tr.error td,summary.error{background:#f8d7da}code{font-size:12px}"
)


def _cls(gaps: list[Gap]) -> str:
    if any(g.severity == "error" for g in gaps):
        return "error"
    return "gap" if gaps else ""


def render_html(traces: Sequence[dict[str, Any]], gaps: dict[str, list[Gap]]) -> str:
    """A single self-contained HTML file: summary table, then every request's stage timeline."""
    e = html.escape
    out = [
        f"<!doctype html><meta charset=utf-8><title>memspine wiretap</title><style>{_CSS}</style>",
        "<h1>memspine wiretap report</h1><h2>Summary</h2><table>",
        "<tr><th>rule<th>severity<th>requests<th>occurrences</tr>",
    ]
    for r in summary_table(traces, gaps):
        out.append(
            f"<tr><td>{e(r['rule'])}<td>{e(r['severity'])}"
            f"<td>{r['requests']}/{r['of']}<td>{r['occurrences']}</tr>"
        )
    out.append("</table><h2>Requests</h2>")
    for t in flatten(traces):
        gl = gaps.get(t["id"], [])
        by_seq: dict[int, list[Gap]] = {}
        for g in gl:
            by_seq.setdefault(g.seq, []).append(g)
        out.append(
            f"<details id={e(t['id'])}><summary class='{_cls(gl)}'>{e(t['id'])} {e(t['kind'])} "
            f"{t['duration_ms']:.1f} ms, {len(t['events'])} events, {len(gl)} gaps "
            f"({e(t['started'])})</summary>"
        )
        parent = f" parent: {e(t['parent_id'])}" if t.get("parent_id") else ""
        out.append(f"<p>fields: <code>{e(json.dumps(t['fields']))}</code>{parent}</p>")
        out.append("<table><tr><th>#<th>t ms<th>stage<th>fields<th>gaps</tr>")
        for ev in t["events"]:
            here = by_seq.get(ev["seq"], [])
            notes = "; ".join(f"{g.rule}: {g.message}" for g in here)
            out.append(
                f"<tr class='{_cls(here)}'><td>{ev['seq']}<td>{ev['t_ms']}<td>{e(ev['stage'])}"
                f"<td><code>{e(json.dumps(ev['fields']))}</code><td>{e(notes)}</tr>"
            )
        for g in by_seq.get(-1, []):
            out.append(f"<tr class=gap><td colspan=5>{e(g.rule)}: {e(g.message)}</tr>")
        out.append("</table></details>")
    return "".join(out)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tap_report", description="memspine wiretap gap report")
    ap.add_argument("path", help="tap directory, tap-*.jsonl file or tap.sqlite")
    ap.add_argument("--request", help="only this request id")
    ap.add_argument("--html", help="write a self-contained HTML report here")
    ap.add_argument("--since", help="2h | 1d | ISO date")
    ap.add_argument("--json", action="store_true", help="print gaps as JSON")
    a = ap.parse_args(argv)
    traces = load_traces(a.path, since=a.since)
    if a.request:
        traces = [t for t in flatten(traces) if t["id"] == a.request]
    gaps = analyse(traces)
    if a.html:
        Path(a.html).write_text(render_html(traces, gaps), encoding="utf-8")
    if a.json:
        print(
            json.dumps({k: [g.__dict__ for g in v] for k, v in gaps.items()}, indent=1, default=str)
        )
    else:
        print(render_text(traces, gaps))
    return 0


if __name__ == "__main__":
    sys.exit(main())
