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
            "Conflict_Resolution split (FactConsolidation) only; gold facts recovered by "
            "mab_gold (798/800 questions), supersession order via "
            "mab_gold.supersession_order_rate",
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
            "op_bench",
            "adapted",
            "github yulinlp/OP-Bench @ 17c7efd",
            "no licence chosen; run locally only",
            "op_bench:OPBenchDataset",
            "retrieval proxies only: persona-turn injection rate, context repetition",
        ),
        DatasetEntry(
            "perltqa",
            "adapted",
            "github Elvin-Yiming-Du/PerLTQA @ 8d9e198",
            "CC BY-NC 4.0; research only",
            "perltqa:PerLTQADataset",
            "Reference Memory R@k per memory type (en_v2 default)",
        ),
        DatasetEntry(
            "prefeval",
            "adapted",
            "github amazon-science/PrefEval (main)",
            "CC BY-NC 4.0; research only",
            "prefeval:PrefEvalDataset",
            "preference-turn R@k; filler capped at 24 local LMSYS convs",
        ),
        DatasetEntry(
            "beam",
            "adapted",
            "HF Mohammadta/BEAM @ 3205395 (100K split local)",
            "CC BY-SA 4.0",
            "beam:BEAMDataset",
            "retrieval-only: source_chat_ids R@k; KU gold = updated_info; QA judge not run",
        ),
        DatasetEntry(
            "tofu",
            "adapted",
            "HF locuslab/TOFU @ 324592d",
            "MIT",
            "tofu_muse:TOFUDataset",
            "memory-erasure probe: residual R@k after hard delete; retain R@k",
        ),
        DatasetEntry(
            "muse_news",
            "adapted",
            "HF muse-bench/MUSE-News @ 506bd5b",
            "CC BY 4.0",
            "tofu_muse:MUSENewsDataset",
            "privleak/verbmem passages as erasure probe; knowmem not adapted",
        ),
        DatasetEntry(
            "personabench",
            "adapted",
            "github SalesforceAIResearch/personabench @ 151e8c9",
            "CC BY-NC-SA 4.0; research only",
            "personabench:PersonaBenchDataset",
            "segment_id R@k per noise level; Subjective excluded officially",
        ),
        DatasetEntry(
            "lamp2",
            "adapted",
            "ciir.cs.umass.edu LaMP (LaMP-2 dev)",
            "no licence file; research use only",
            "lamp:LaMP2Dataset",
            "retrieval-only kNN tag vote; proxy gold = same-tag profile items",
        ),
        DatasetEntry(
            "memorycd",
            "adapted",
            "HF WZDavid/MemoryCD @ 14b934c",
            "Amazon Reviews 2023 derived, no licence; research only",
            "memorycd:MemoryCDDataset",
            "kNN rating MAE proxy; query = review title",
        ),
        DatasetEntry(
            "halumem_proxy",
            "adapted",
            "HF IAAR-Shanghai/HaluMem @ cb04336",
            "CC BY-NC-ND 4.0; research only, no redistribution",
            "halumem_proxy:HaluMemProxyDataset",
            "lexical memory-point -> turn proxy gold (overlap >= 0.5)",
        ),
        DatasetEntry(
            "cpb_live",
            "adapted",
            "github lxy1134/iclr_2027",
            "MIT",
            "cpb:CPBLiveDataset",
            "Live replay; true R@k, false-retrieval rate, true-above-false order",
        ),
        DatasetEntry(
            "asb",
            "adapted",
            "github agiresearch/ASB @ 544540f",
            "MIT",
            "asb:ASBDataset",
            "firewall_rates TPR/FPR; injection templates reconstructed from the card",
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
