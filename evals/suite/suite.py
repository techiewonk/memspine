"""Load and validate ``suite.yaml`` against the dataset registry (G-02).

The loader refuses: an unknown dataset id, LongMemEval in any form, an unknown role, a missing field,
a duplicate entry id, and a suite without the five datasets of the decision (two dev pools, three blind
gates) plus the behavioural gate.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from memspine_evals.datasets.registry import REGISTRY, DatasetEntry

HERE = Path(__file__).resolve().parent
SUITE_PATH = HERE / "suite.yaml"
ROLES = ("dev", "blind", "behavioural")
REQUIRED = {"locomo": "dev", "op_bench": "dev", "memoryagentbench": "blind", "convomem": "blind", "prefeval": "blind"}
FIELDS = ("id", "role", "slice", "metric", "judge", "pass_rule")
EXCLUDED = ("longmemeval",)


class SuiteError(ValueError):
    """The suite file is invalid."""


@dataclass(frozen=True)
class SuiteEntry:
    id: str
    dataset: str | None
    role: str
    slice: str
    metric: str
    judge: str
    pass_rule: str
    categories: tuple[int, ...] = ()


@dataclass(frozen=True)
class Suite:
    entries: tuple[SuiteEntry, ...]

    def by_role(self, role: str) -> tuple[SuiteEntry, ...]:
        return tuple(e for e in self.entries if e.role == role)

    def gate_ids(self) -> tuple[str, ...]:
        """Entry ids that need a paired-gate verdict (everything but the behavioural gate)."""
        return tuple(e.id for e in self.entries if e.role != "behavioural")


def _mentions_excluded(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    flat = value.lower().replace("-", "").replace("_", "").replace(" ", "")
    return any(x in flat for x in EXCLUDED)


def load_suite(path: Path | str = SUITE_PATH, registry: Mapping[str, DatasetEntry] = REGISTRY) -> Suite:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("entries"), list):
        raise SuiteError("suite file needs an 'entries' list")
    entries: list[SuiteEntry] = []
    for item in raw["entries"]:
        for key, val in item.items():
            if _mentions_excluded(val):
                raise SuiteError(f"LongMemEval is excluded from the suite (entry {item.get('id')!r}, field {key!r})")
        missing = [f for f in FIELDS if not item.get(f)]
        if missing:
            raise SuiteError(f"entry {item.get('id')!r} lacks {missing}")
        if item["role"] not in ROLES:
            raise SuiteError(f"entry {item['id']!r}: unknown role {item['role']!r}")
        ds = item.get("dataset")
        if item["role"] == "behavioural":
            if ds is not None:
                raise SuiteError("the behavioural entry is dataset-free (dataset: null)")
        else:
            if ds not in registry:
                raise SuiteError(f"entry {item['id']!r}: dataset {ds!r} is not in the dataset registry")
            if registry[ds].status != "adapted":
                raise SuiteError(f"entry {item['id']!r}: dataset {ds!r} has no adapter (status {registry[ds].status})")
        entries.append(
            SuiteEntry(
                item["id"], ds, item["role"], str(item["slice"]), item["metric"], item["judge"],
                item["pass_rule"], tuple(item.get("categories") or ()),
            )
        )
    ids = [e.id for e in entries]
    if len(set(ids)) != len(ids):
        raise SuiteError("duplicate entry ids")
    for ds, role in REQUIRED.items():
        found = [e for e in entries if e.dataset == ds]
        if not found or found[0].role != role:
            raise SuiteError(f"the suite must include {ds!r} with role {role!r}")
    loco = next(e for e in entries if e.dataset == "locomo")
    if 5 not in loco.categories:
        raise SuiteError("LoCoMo must include category 5")
    if len([e for e in entries if e.role == "behavioural"]) != 1:
        raise SuiteError("the suite needs exactly one behavioural entry")
    declared = {str(x).lower() for x in raw.get("excluded") or ()}
    if not set(EXCLUDED) <= declared:
        raise SuiteError("the 'excluded' list must name longmemeval")
    return Suite(tuple(entries))
