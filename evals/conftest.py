"""Make ``memspine_evals`` importable without installing it.

The harness lives outside the wheel (D-35) and must stay runnable in a bare
environment — installing memspine's full dependency set just to check that a
BM25 baseline still ranks correctly would defeat the point.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
