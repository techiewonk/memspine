"""Which benchmarks the harness knows about, and whether each can be run.

An entry records where the data comes from, its licence and the release status. A dataset
whose data is not public is listed with ``status="unreleased"`` and no adapter: the entry
exists so a plan can name it, and ``load`` refuses it instead of inventing a format.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["REGISTRY", "DatasetEntry", "DatasetUnavailable", "entry", "load"]


class DatasetUnavailable(RuntimeError):
    """The dataset is registered but has no public data or no adapter."""


@dataclass(frozen=True, slots=True)
class DatasetEntry:
    dataset_id: str
    status: str  # "adapted" | "unreleased"
    source: str
    licence: str
    adapter: str | None = None  # "module:Class" under memspine_evals.datasets
    notes: str = ""


REGISTRY: dict[str, DatasetEntry] = {
    e.dataset_id: e
    for e in (
        DatasetEntry(
            "locomo",
            "adapted",
            "github snap-research/locomo (locomo10.json)",
            "check the release before publishing",
            "locomo:LoCoMoDataset",
        ),
        DatasetEntry(
            "longmemeval",
            "adapted",
            "github xiaowu0162/LongMemEval",
            "MIT (cleaned release); verify the file held",
            "longmemeval:LongMemEvalDataset",
        ),
        DatasetEntry(
            "locomo_plus",
            "adapted",
            "github xjtuleeyf/Locomo-Plus @ 059f4e3",
            "no licence in repo; run only",
            "locomo_plus:LoCoMoPlusDataset",
        ),
        DatasetEntry(
            "memoryagentbench",
            "adapted",
            "HF ai-hyz/MemoryAgentBench @ 7ea0669",
            "MIT",
            "memoryagentbench:MemoryAgentBenchDataset",
            "Conflict_Resolution split (FactConsolidation) only",
        ),
        DatasetEntry(
            "convomem",
            "adapted",
            "HF Salesforce/ConvoMem @ e3e9b39",
            "CC BY-NC 4.0 (data); research only",
            "convomem:ConvoMemDataset",
        ),
        DatasetEntry(
            "halumem",
            "adapted",
            "HF IAAR-Shanghai/HaluMem @ cb04336",
            "CC BY-NC-ND 4.0; research only, no redistribution",
            "halumem:HaluMemDataset",
            "Memory QA task only",
        ),
        DatasetEntry(
            "statemembench",
            "unreleased",
            "arXiv 2608.19652 ('Can Agent Memory Systems Track Evolving State?')",
            "unknown (no public data as of 2026-10-05)",
            None,
            "234 multi-session scenarios, 3 domains; answers graded current / superseded / "
            "neither. Adapter waits for the data release.",
        ),
    )
}


def entry(dataset_id: str) -> DatasetEntry:
    try:
        return REGISTRY[dataset_id]
    except KeyError:
        raise DatasetUnavailable(f"unknown dataset {dataset_id!r}") from None


def load(dataset_id: str, *args: Any, **kwargs: Any) -> Any:
    """Construct the adapter for ``dataset_id``; refuses an unreleased dataset."""
    item = entry(dataset_id)
    if item.status != "adapted" or item.adapter is None:
        raise DatasetUnavailable(f"{dataset_id} is {item.status}: {item.notes}")
    import importlib

    module, cls = item.adapter.split(":")
    return getattr(importlib.import_module(f"{__package__}.{module}"), cls)(*args, **kwargs)
