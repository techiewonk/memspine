"""R5-10: golden guard for the ``profile="simple"`` defaults.

Every opt-in feature (H1-H24, the firewall, MTI, the decision port) promises to leave the
default engine unchanged. This snapshot makes that promise checkable: a changed default in
the read / firewall / decision / integrity config sections, or in any bindable policy's
options, fails here and has to be accepted on purpose.

To accept an intended change, regenerate the snapshot and review the diff::

    MEMSPINE_UPDATE_GOLDENS=1 pytest tests/unit/test_simple_profile_golden.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from memspine.config.schema import MemspineConfig
from memspine.core.policies.assembly import AssemblyOptions
from memspine.core.policies.community import CommunityOptions
from memspine.core.policies.compression import CompressionOptions
from memspine.core.policies.conflict import ConflictOptions
from memspine.core.policies.consolidation import ConsolidationOptions
from memspine.core.policies.decay import DecayOptions
from memspine.core.policies.dedup import DedupOptions
from memspine.core.policies.retention import RetentionOptions
from memspine.core.policies.scoring import ScoringOptions
from memspine.core.policies.trust import TrustOptions
from memspine.memories.semantic.write_pipeline import SemanticWriteOptions

GOLDEN = Path(__file__).parent / "golden" / "simple_profile_defaults.json"
# Read at import: the autouse conftest fixture scrubs MEMSPINE_* before each test.
_UPDATE = os.environ.get("MEMSPINE_UPDATE_GOLDENS") == "1"

_SECTIONS = ("read", "write", "firewall", "decision", "integrity", "retention", "audit", "consent", "rest")
_OPTIONS = {
    "assembly": AssemblyOptions,
    "community": CommunityOptions,
    "compression": CompressionOptions,
    "conflict": ConflictOptions,
    "consolidation": ConsolidationOptions,
    "decay": DecayOptions,
    "dedup": DedupOptions,
    "retention": RetentionOptions,
    "scoring": ScoringOptions,
    # #32: ``memories.semantic.policies.write`` (reflexion on by default).
    "semantic_write": SemanticWriteOptions,
    "trust": TrustOptions,
}


def _snapshot() -> dict[str, Any]:
    config = MemspineConfig().model_dump(mode="json")
    return {
        "config": {section: config[section] for section in _SECTIONS},
        "policy_options": {
            name: options().model_dump(mode="json") for name, options in _OPTIONS.items()
        },
    }


def test_simple_profile_defaults_match_golden() -> None:
    current = _snapshot()
    if _UPDATE:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert current == expected, (
        "a profile='simple' default changed; if intended, regenerate with "
        "MEMSPINE_UPDATE_GOLDENS=1 and review the diff of " + GOLDEN.name
    )
