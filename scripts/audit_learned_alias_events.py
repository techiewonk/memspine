#!/usr/bin/env python3
"""Read-only RM-66 diagnostic: learned.alias events that may carry another namespace's names.

Before RM-18 (commit 6fba3c69) the pre-pass kept one engine-wide learned-alias set, and
``LearnedState.flush(namespace)`` wrote every pending alias into a ``learned.alias`` marker
labelled with whichever namespace flushed first. The payload is ``{"marker": "learned.alias",
"names": [...]}``: the names carry no namespace of their own, and reload now trusts the event
namespace. A name learned from namespace A can therefore sit in an event labelled B.

This script opens a SQLite event log read-only and, for every ``learned.alias`` name that a
later ``learned.alias.reset`` of the same namespace has not dropped, checks where the name
occurs in the non-marker event payloads:

* ``own``: the name occurs in the event's own namespace (consistent with RM-18 semantics);
* ``foreign_only``: the name occurs only in other namespaces (a likely pre-fix leak: a hit);
* ``unseen``: the name occurs nowhere in the retained log (rolling log, erased source or
  content kept outside the payload), so the log cannot decide.

Matching is a case-insensitive substring test over payload strings: a review aid, not proof.
Nothing is written. Exit status: 0 when there are no hits, 1 when there are hits, 2 on a
usage or read error. When hits exist the owner decides; the remedy is ``erase_namespace``
for the affected namespace (no migration shim, RM-66).

    python scripts/audit_learned_alias_events.py path/to/memspine.sqlite
    python scripts/audit_learned_alias_events.py sqlite:///path/to/memspine.sqlite --json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

MARKER_KIND = "memory.marker"
ALIAS_MARKER = "learned.alias"
RESET_MARKER = "learned.alias.reset"


@dataclass
class AliasReport:
    namespaces: set[str] = field(default_factory=set)
    alias_events: int = 0
    reset_events: int = 0
    names_total: int = 0
    names_reset: int = 0
    status: Counter[str] = field(default_factory=Counter)
    by_namespace: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    hits: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "namespaces": len(self.namespaces),
            "alias_events": self.alias_events,
            "reset_events": self.reset_events,
            "names_total": self.names_total,
            "names_dropped_by_reset": self.names_reset,
            "own": self.status["own"],
            "foreign_only": self.status["foreign_only"],
            "unseen": self.status["unseen"],
            "by_namespace": {ns: dict(c) for ns, c in sorted(self.by_namespace.items())},
            "hits": self.hits,
        }


def sqlite_path(target: str) -> Path:
    """Accept a file path or a ``sqlite:///`` / ``sqlite+aiosqlite:///`` storage URL."""
    if "://" not in target:
        return Path(target)
    parsed = urlparse(target)
    if not parsed.scheme.startswith("sqlite"):
        raise ValueError(f"only SQLite event logs are supported, got scheme {parsed.scheme!r}")
    path = unquote(parsed.path)
    if len(path) >= 3 and path[0] == "/" and path[2] == ":":  # sqlite:///C:/x on Windows
        path = path[1:]
    if not path or path in {"/", "/:memory:", ":memory:"}:
        raise ValueError("an in-memory database has no persisted event log")
    return Path(path)


def _decoder() -> Any:
    import zstandard  # core dependency (D-32); only needed for compressed payloads

    return zstandard.ZstdDecompressor()


def _strings(value: Any, out: list[str]) -> None:
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _strings(item, out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _strings(item, out)


def scan(path: Path) -> AliasReport:
    """Scan one SQLite event log read-only."""
    if not path.is_file():
        raise FileNotFoundError(f"no such event log: {path}")
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    decoder: Any = None
    try:
        rows = conn.execute(
            "SELECT seq, kind, namespace, payload, compressed FROM memory_events ORDER BY seq"
        )
        report = AliasReport()
        # namespace -> name -> seq of the latest alias event naming it
        live: dict[str, dict[str, int]] = defaultdict(dict)
        texts: dict[str, list[str]] = defaultdict(list)
        for seq, kind, namespace, raw, compressed in rows:
            report.namespaces.add(namespace)
            if compressed:
                decoder = decoder or _decoder()
                raw = decoder.decompress(raw)
            payload = json.loads(raw)
            if kind == MARKER_KIND and isinstance(payload, dict):
                marker = payload.get("marker")
                if marker == ALIAS_MARKER:
                    report.alias_events += 1
                    for name in payload.get("names", []):
                        report.names_total += 1
                        live[namespace][str(name)] = seq
                    continue
                if marker == RESET_MARKER:
                    report.reset_events += 1
                    report.names_reset += len(live.pop(namespace, {}))
                    continue
                continue
            found: list[str] = []
            _strings(payload, found)
            if found:
                texts[namespace].append("\n".join(found).casefold())
    finally:
        conn.close()

    corpus = {ns: "\n".join(parts) for ns, parts in texts.items()}
    for namespace, names in sorted(live.items()):
        for name, seq in sorted(names.items()):
            needle = name.casefold()
            own = needle in corpus.get(namespace, "")
            elsewhere = sorted(
                ns for ns, text in corpus.items() if ns != namespace and needle in text
            )
            status = "own" if own else "foreign_only" if elsewhere else "unseen"
            report.status[status] += 1
            report.by_namespace[namespace][status] += 1
            if status == "foreign_only":
                report.hits.append(
                    {"namespace": namespace, "name": name, "seq": seq, "found_in": elsewhere}
                )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("target", help="SQLite event-log path or sqlite:/// storage URL")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    try:
        report = scan(sqlite_path(args.target))
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    data = report.as_dict()
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print(
            f"namespaces={data['namespaces']} alias_events={data['alias_events']} "
            f"reset_events={data['reset_events']} names={data['names_total']} "
            f"dropped_by_reset={data['names_dropped_by_reset']}"
        )
        print(f"own={data['own']} foreign_only={data['foreign_only']} unseen={data['unseen']}")
        for ns, counts in data["by_namespace"].items():
            print(f"  {ns}: " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        for hit in data["hits"]:
            print(
                f"  HIT namespace={hit['namespace']} seq={hit['seq']} name={hit['name']!r} "
                f"found_in={','.join(hit['found_in'])}"
            )
        if data["foreign_only"]:
            print("Hits found: owner reviews; erase_namespace for affected namespaces (RM-66).")
    return 1 if data["foreign_only"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
