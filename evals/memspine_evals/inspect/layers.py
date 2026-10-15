# ruff: noqa: E501
"""Layer view of a saved step-mode bundle: what each memory layer holds and where it came from.

    python -m memspine_evals.inspect.layers <bundle-dir>                 # L0..L5 table, both phases
    python -m memspine_evals.inspect.layers <bundle-dir> --layer L2      # every L2 record, with parents
    python -m memspine_evals.inspect.layers <bundle-dir> --layer L1 --phase post_ingest --limit 5

The bundle is the folder ``save`` writes in step mode (``...--bundle``). Reads ``records.jsonl`` and
``links.jsonl`` only; never starts an engine. ``--layer`` lists each record's channel, status, trust,
parent turns (lineage) and the start of its text, so a record can be traced back to the turns it
came from. Manual testing guide: ``docs/v2/MANUAL_TESTING.md``.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

LAYERS = ("L0", "L1", "L2", "L3", "L4", "L5")
#: what each layer is, and which producer fills it (store/layers.py is the source of truth)
MEANING = {
    "L0": "raw turns, resources (episodic)",
    "L1": "facts (window extraction 'mining', user-written semantic)",
    "L2": "episodes and summaries (consolidation, session_close, window_episode, multires)",
    "L3": "graph-derived (extract_graph, entity_summary, community_summary); compiled edges live in links",
    "L4": "observations, reflections (patterns, reflect_profile)",
    "L5": "standing answers, procedures, plans",
}


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("bundle", type=Path)
    ap.add_argument("--layer", choices=LAYERS)
    ap.add_argument("--phase", choices=("post_ingest", "post_sleep"), help="default: post_sleep if present")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args(argv)

    records = _rows(args.bundle / "records.jsonl")
    links = _rows(args.bundle / "links.jsonl")
    if not records:
        print(f"no records.jsonl in {args.bundle} (run `save` in step mode first)")
        return 1
    phases = {r.get("phase") for r in records}
    phase = args.phase or ("post_sleep" if "post_sleep" in phases else "post_ingest")
    recs = [r for r in records if r.get("phase") == phase]
    by_id = {r["record_id"]: r for r in recs}

    if args.layer is None:
        print(f"bundle {args.bundle.name}  phase={phase}  records={len(recs)}")
        print(f"{'layer':5}  {'count':>5}  channels")
        for layer in LAYERS:
            mine = [r for r in recs if r.get("layer") == layer]
            ch = Counter(f"{r.get('memory_type')}/{r.get('channel')}" for r in mine)
            print(f"{layer:5}  {len(mine):5d}  {dict(ch) if ch else '-'}")
            print(f"{'':5}  {'':5}  ({MEANING[layer]})")
        edges = Counter(lk.get("kind") for lk in links if lk.get("phase") == phase)
        print(f"links (lineage, graph edges): {dict(edges)}")
        return 0

    parents: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for lk in links:
        if lk.get("phase") == phase and lk.get("kind") == "lineage":
            parents[lk["src"]].append((lk["dst"], lk.get("rel", "")))
    mine = [r for r in recs if r.get("layer") == args.layer]
    print(f"{args.layer}: {len(mine)} record(s) in {phase}  ({MEANING[args.layer]})")
    for r in mine[: args.limit]:
        ps = parents.get(r["record_id"], [])
        turns = [by_id.get(p, {}).get("turn_id") or p[:8] for p, _ in ps]
        print(f"- {r['record_id'][:8]}  {r.get('memory_type')}/{r.get('channel')}  status={r.get('status')} "
              f"trust={r.get('trust')}  turn={r.get('turn_id')}")
        if ps:
            print(f"    parents ({len(ps)}): {', '.join(str(t) for t in turns[:12])}"
                  f"{' ...' if len(turns) > 12 else ''}  roles={sorted({rel for _, rel in ps})}")
        print(f"    text: {str(r.get('content') or '')[:110]}")
    if len(mine) > args.limit:
        print(f"... {len(mine) - args.limit} more (--limit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
