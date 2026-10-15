"""Launch memspine_evals without evals/ first on sys.path.

The legacy folder evals/datasets (now evals/legacy_datasets, ENV-2) used to shadow the
HuggingFace `datasets` package once sentence-transformers installed it, and LanceDB then
failed registering its converter.
"""
import sys
from pathlib import Path

sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != Path(__file__).parent.resolve()]
sys.path.append(str(Path(__file__).parent))
from memspine_evals.cli import main  # noqa: E402

raise SystemExit(main())
