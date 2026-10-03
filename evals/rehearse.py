"""Offline rehearsal of a paid run plan with a cost projection (G5/G6).

    python evals/rehearse.py --plan evals/plans/aamas_runs.json --path data/locomo10.json \
        --price bedrock/converse/qwen.qwen3-32b-v1:0=0.15,0.60

See ``memspine_evals.rehearsal`` for what is checked and how the projection is made.
No network: LiteLLM's transport is replaced by a local stub.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from memspine_evals.rehearsal import main

if __name__ == "__main__":
    sys.exit(main())
