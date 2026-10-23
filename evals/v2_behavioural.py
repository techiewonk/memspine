"""Behavioural-suite check of one v2 arm (sixth gate), model-free, for the v2 queue.

    python v2_behavioural.py ARM [--ref scr-pool-protect-jina] [--json-out PATH]

Builds the golden-harness engine (hash embedder, no rerank) with the arm's *flag overlay*: the keys of the arm file
that differ from the reference arm, in the ``read`` / ``write`` / ``graph`` sections only (model, embedding and
storage settings never apply: the suite is model-free). Compares the report with ``suite/behavioural_baseline.json``
through ``suite.gate`` and prints one final line:

    behavioural: OK | SKIPPED | REGRESSION <ids> | SAFETY_REJECT <ids> | ERROR <why>

The queue treats SAFETY_REJECT as a hard stop for the batch; REGRESSION blocks adoption (PROMOTE becomes HOLD).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
SECTIONS = (
    "read",
    "write",
    "graph",
    "offline",
)  # offline: prerequisites of read.standing_v2 etc. (FU-REQS)
#: Keys that decide which candidates reach a reranker (INV-FILLER, R3-07). The overlay is a diff against the
#: reference arm, so an arm that only adds ``rerank_v2`` would be gated on an engine whose pool is exactly
#: ``top_k``: the reranker then reorders five hits and cannot recover a relevant record the filler pushed out.
#: The reference arm sets these; the model-free engine must carry them or it is not the arm being gated.
POOL_KEYS = ("pool_protect_per_leg", "candidate_pool")


def overlay(arm: dict[str, Any], ref: dict[str, Any]) -> dict[str, Any]:
    """Keys of ``arm`` (restricted to SECTIONS) whose value differs from ``ref``; nested dicts are diffed."""

    def diff(a: Any, b: Any) -> Any:
        if isinstance(a, dict):
            b = b if isinstance(b, dict) else {}
            out = {k: diff(v, b.get(k)) for k, v in a.items() if v != b.get(k)}
            return {k: v for k, v in out.items() if v != {}}
        return a

    return {s: d for s in SECTIONS if (d := diff(arm.get(s, {}), ref.get(s, {})))}


def with_pool_shape(ov: dict[str, Any], arm: dict[str, Any]) -> dict[str, Any]:
    """When the overlay turns ``read.rerank_v2`` on, add the arm's pool-shape keys (:data:`POOL_KEYS`)."""
    if not ov.get("read", {}).get("rerank_v2"):
        return ov
    pool = {k: v for k in POOL_KEYS if (v := arm.get("read", {}).get(k)) is not None}
    return {**ov, "read": {**pool, **ov["read"]}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("arm")
    ap.add_argument("--ref", default="scr-pool-protect-jina")
    ap.add_argument("--json-out", type=Path, default=None)
    a = ap.parse_args(argv)
    load = lambda n: json.loads((HERE / "arms" / f"{n}.json").read_text(encoding="utf-8"))
    arm = load(a.arm)
    ov = with_pool_shape(overlay(arm, load(a.ref)), arm)
    if ("graph" in ov or "offline" in ov) and arm.get("memories"):
        # a graph / sleep overlay needs the arm's memory types (associative for the graph) or the
        # model-free engine refuses the config before any case runs
        ov = {**ov, "memories": arm["memories"]}
    if not ov:
        print("behavioural: SKIPPED (arm has no read/write/graph difference from the reference)")
        return 0
    try:
        from memspine_evals.behavioural import default_engine_factory, run_behavioural
        from suite import gate

        def factory(storage_dir: Path) -> Any:
            from memspine import Engine

            kw: dict[str, Any] = dict(
                template="core",
                dotenv_path=None,
                storage={"path": str(storage_dir / "behavioural.db")},
                model={"embedding": {"provider": "hash"}, "rerank": {"chain": []}},
                read={"record_access": False},
                memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
            )
            for sec, vals in ov.items():
                if isinstance(kw.get(sec), dict):
                    kw[sec] = {**kw[sec], **vals}
                else:
                    kw[sec] = vals
            return Engine(**kw)

        _ = default_engine_factory  # the base configuration this overlay extends
        report = run_behavioural(factory)
        ref = gate.load_baseline()
        safety = sorted(
            r.id
            for r in report.results
            if r.status.value == "FAIL" and r.id.startswith(gate.BLOCKING_SAFETY)
        )
        regs = [c for c in gate.regressions(report, ref) if c not in safety]
        if a.json_out:
            a.json_out.parent.mkdir(parents=True, exist_ok=True)
            a.json_out.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"behavioural: ERROR {type(exc).__name__}: {exc}")
        return 1
    print("overlay:", json.dumps(ov, sort_keys=True))
    if safety:
        print("behavioural: SAFETY_REJECT " + ",".join(safety))
    elif regs:
        print("behavioural: REGRESSION " + ",".join(regs))
    else:
        print("behavioural: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
