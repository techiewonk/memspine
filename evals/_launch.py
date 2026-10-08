"""Launch memspine_evals without evals/ first on sys.path.

evals/datasets/ (legacy) would shadow the HuggingFace `datasets` package once
sentence-transformers installs it, and LanceDB then fails registering its converter.
"""
import sys
from pathlib import Path

sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != Path(__file__).parent.resolve()]
sys.path.append(str(Path(__file__).parent))
from memspine_evals.cli import main  # noqa: E402

raise SystemExit(main())
