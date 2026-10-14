"""Licence: locate the LoCoMo file on this machine and re-hydrate question text from ids.

Committed analysis files keep ids, labels and hashes only (never benchmark text). A consumer that
needs the text at runtime calls :func:`locomo_questions` here; it reads ``locomo10.json`` from the
local, gitignored ``data/`` directory (or the path in ``MEMSPINE_LOCOMO_PATH``) and returns
``None`` when the dataset is not on disk, so the caller can skip or degrade instead of failing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[2]


def locomo_path() -> Path | None:
    """The local ``locomo10.json``, or ``None``."""
    env = os.environ.get("MEMSPINE_LOCOMO_PATH")
    candidates = [Path(env)] if env else []
    candidates += [_REPO / "data" / "locomo10.json", _REPO / "evals" / "data" / "locomo10.json"]
    for path in candidates:
        if path.is_file():
            return path
    return None


def load_locomo() -> list[dict[str, Any]] | None:
    path = locomo_path()
    if path is None:
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    return list(raw.values()) if isinstance(raw, dict) else raw


def locomo_questions() -> dict[tuple[str, str], dict[str, Any]] | None:
    """``(sample_id, qa_index) -> qa dict`` for every question, or ``None`` without the dataset.

    ``qa_index`` is the position in the sample's ``qa`` list as a string (the ``<index>`` of a
    ``conv-26#<index>`` id).
    """
    data = load_locomo()
    if data is None:
        return None
    return {(c["sample_id"], str(i)): qa for c in data for i, qa in enumerate(c["qa"])}
