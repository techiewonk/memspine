"""Run the behavioural suite on the model-free engine.

    python evals/suite/run_behavioural.py [--write-baseline] [--json]

``--write-baseline`` stores the statuses in ``behavioural_baseline.json`` (the gate's default reference).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from memspine_evals.behavioural import run_behavioural

from suite.gate import BASELINE_PATH


def _quiet() -> None:
    logging.disable(logging.CRITICAL)
    try:
        import structlog

        structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))
    except ImportError:
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    _quiet()
    report = run_behavioural()
    if a.write_baseline:
        BASELINE_PATH.write_text(
            json.dumps(
                {
                    "counts": report.counts(),
                    "statuses": report.statuses(),
                    "details": {r.id: r.detail for r in report.results if r.detail},
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    if a.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        for r in report.results:
            print(f"{r.status.value:14s} {r.id:20s} {r.kind:10s} {r.detail}")
        print(report.counts())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
