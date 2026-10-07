"""OP-Bench adapter: over-personalisation probes on the LoCoMo conversations (retrieval proxies).

Sources, checked 2026-10-07:

- code + data: github ``yulinlp/OP-Bench`` @ ``17c7efd`` (``data/locomo10.json`` and
  ``data/locomo10_overpersonalized.json``); paper arXiv 2601.13722 (accepted to EMNLP 2026).
- **Licence: none chosen yet** (``NOTICE.md``: "project owner should add the license"). Run
  locally only; never redistribute the task file or derived copies. It reuses LoCoMo.

Format (``locomo10_overpersonalized.json``): a list aligned by index with ``locomo10.json``;
entry ``i`` maps each of the conversation's two speakers to::

    {"topics": [...], "profile": str, "observation": [str, ...],
     "tasks": {"irrelevance_easy": [{"topic", "question"}],
               "irrelevance_hard": [{"question", "type", "explanation"}],
               "sycophancy": [{"question", "type", "explanation"}],
               "diversity": [{"questions": [{"topic", "question"}]}]}}

The official workflow (``src/opbench/types.py``) evaluates the **first** speaker of each
conversation unless ``use_both_personas`` is set; its memory is the whole LoCoMo conversation
(``agents/common.py``). This adapter does the same (``both_personas`` mirrors the flag).

Mapping to the harness contract:

- one ``EvalItem`` per (conversation, persona); the history is the LoCoMo conversation built by
  ``LoCoMoDataset`` (same turn ids, ``"D<session>:<n>"``), so a run's store is LoCoMo's;
- one ``Query`` per task question, ``gold = None`` and ``gold_turn_ids = ()``: these probes
  have no supporting evidence by construction, so R@k is not defined;
- ``type_label`` = ``"<task>/<subtype>"`` (``irrelevance_easy/fully_irrelevant``,
  ``irrelevance_hard/subject_confusion``, ``sycophancy/fine-grained``, ``diversity``);
- ``meta["persona_turn_ids"]`` = the ids of every turn the persona spoke (one shared tuple
  per item), ``meta["injection_probe"]`` marks the irrelevance tasks, ``meta["false_premise"]``
  marks the memory-level sycophancy probes (fine- / coarse-grained: "do you remember when I…"
  about something that never happened), and ``meta["diversity_group"]`` groups the
  repetition probes of one persona.

Free (retrieval-level) measures, computed by the pure functions below from a row's
``retrieved_ids`` and the query's ``meta``:

- :func:`profile_injected` / :func:`injection_rate`: share of irrelevance probes whose
  returned evidence contains at least one of the persona's own turns. This is a proxy for the
  paper's "memory hijacking": a system that always returns top-k scores 1.0 by construction,
  so the number separates systems only when retrieval can return nothing (a floor or gate);
  :func:`persona_share` gives the graded version (share of returned turns that are personal).
- :func:`context_repetition`: mean pairwise Jaccard overlap of the retrieved id sets within
  one diversity group (high = the same personal turns resurface for distinct questions).

**Gaps, stated rather than guessed:** the paper's scores are response-level LLM-judge scores
(``scoring.py``), not reproduced here; "observation" and "profile" strings are generated
summaries, not turns, so injection is measured over raw turns; the paper's 1,700 verified
instances are a filtered subset of the task file and the filter is not released.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..contracts import DatasetInfo, EvalItem, Query
from .locomo import LoCoMoDataset

__all__ = [
    "OPBENCH_TASKS",
    "OPBenchDataset",
    "context_repetition",
    "injection_rate",
    "persona_share",
    "profile_injected",
]

OPBENCH_TASKS = ("irrelevance_easy", "irrelevance_hard", "sycophancy", "diversity")
_FALSE_PREMISE = frozenset({"fine-grained", "coarse-grained"})


class OPBenchDataset:
    """OP-Bench probes over a local OP-Bench checkout's ``data/`` folder. Never downloads."""

    def __init__(
        self,
        root: str | Path,
        revision_id: str,
        tasks: tuple[str, ...] = OPBENCH_TASKS,
        both_personas: bool = False,
        licence: str = "no licence chosen (yulinlp/OP-Bench NOTICE.md); run locally only",
    ) -> None:
        root = Path(root)
        data = root / "data" if (root / "data").is_dir() else root
        self.task_path = data / "locomo10_overpersonalized.json"
        self.locomo_path = data / "locomo10.json"
        for path in (self.task_path, self.locomo_path):
            if not path.exists():
                raise FileNotFoundError(f"{path} not found; fetch OP-Bench first (evals/README.md)")
        unknown = set(tasks) - set(OPBENCH_TASKS)
        if unknown:
            raise ValueError(f"unknown OP-Bench tasks {sorted(unknown)}")
        self.tasks, self.both_personas, self.licence = tasks, both_personas, licence
        digest = hashlib.sha256()
        for path in (self.locomo_path, self.task_path):
            digest.update(path.name.encode() + b"\0" + path.read_bytes())
        self._sha = digest.hexdigest()
        self.revision_id = f"sha256:{self._sha[:16]}" if revision_id == "auto" else revision_id
        self._items = list(self._build())

    def info(self) -> DatasetInfo:
        return DatasetInfo(
            dataset_id="op_bench",
            revision_id=self.revision_id,
            licence=self.licence,
            source_path=str(self.task_path.parent),
            content_sha256=self._sha,
            n_items=len(self._items),
            n_queries=sum(len(i.queries) for i in self._items),
            subset=f"tasks={','.join(self.tasks)},both_personas={self.both_personas}",
            notes=(
                "retrieval proxies only (persona-turn injection, context repetition); no "
                "R@k gold by construction; official scores are LLM-judged"
            ),
        )

    def items(self) -> Iterator[EvalItem]:
        yield from self._items

    def _build(self) -> Iterator[EvalItem]:
        locomo = list(LoCoMoDataset(self.locomo_path, revision_id="op_bench").items())
        tasks_raw: list[dict[str, Any]] = json.loads(self.task_path.read_text(encoding="utf-8"))
        for index, entry in enumerate(tasks_raw):
            if index >= len(locomo) or not isinstance(entry, dict):
                continue
            conv = locomo[index]
            people = list(entry.items())
            if not self.both_personas:
                people = people[:1]
            for persona, data in people:
                persona_ids = tuple(t.turn_id for t in conv.history if t.speaker == persona)
                queries = tuple(self._queries(conv.item_id, persona, data, persona_ids))
                if queries:
                    yield EvalItem(
                        item_id=f"{conv.item_id}:{persona}",
                        history=conv.history,
                        queries=queries,
                        meta={
                            "persona": persona,
                            "locomo_sample": conv.item_id,
                            "profile": data.get("profile", ""),
                            "n_observations": len(data.get("observation") or []),
                        },
                    )

    def _queries(
        self, sample: str, persona: str, data: Mapping[str, Any], persona_ids: tuple[str, ...]
    ) -> Iterator[Query]:
        tasks = data.get("tasks") or {}
        for task in self.tasks:
            entries: list[Any] = list(tasks.get(task) or [])
            if task == "diversity":
                groups = [g for g in entries if isinstance(g, dict)]
                entries = [q for g in groups for q in g.get("questions") or []]
            for n, raw in enumerate(entries):
                text = raw.get("question") if isinstance(raw, dict) else raw
                if not text:
                    continue
                if task == "irrelevance_easy":
                    subtype = "fully_irrelevant"
                elif task == "diversity":
                    subtype = None
                else:
                    subtype = str(raw.get("type") or "unknown")
                group = f"{sample}:{persona}:diversity" if task == "diversity" else None
                yield Query(
                    query_id=f"{sample}:{persona}:{task}:{n}",
                    text=str(text).strip(),
                    type_label=task if subtype is None else f"{task}/{subtype}",
                    meta={
                        "benchmark": "op_bench",
                        "task": task,
                        "subtype": subtype,
                        "topic": raw.get("topic") if isinstance(raw, dict) else None,
                        "persona": persona,
                        "persona_turn_ids": persona_ids,
                        "injection_probe": task.startswith("irrelevance"),
                        "false_premise": subtype in _FALSE_PREMISE,
                        "diversity_group": group,
                    },
                )


def profile_injected(retrieved_ids: Sequence[str], meta: Mapping[str, Any]) -> bool | None:
    """Whether any returned turn is the persona's own; ``None`` for a non-injection probe."""
    if not meta.get("injection_probe"):
        return None
    personal = set(meta.get("persona_turn_ids") or ())
    return any(tid in personal for tid in retrieved_ids)


def persona_share(retrieved_ids: Sequence[str], meta: Mapping[str, Any]) -> float | None:
    """Share of returned turns spoken by the persona; ``None`` when nothing was returned."""
    if not retrieved_ids:
        return None
    personal = set(meta.get("persona_turn_ids") or ())
    return sum(tid in personal for tid in retrieved_ids) / len(retrieved_ids)


def injection_rate(rows: Iterable[tuple[Sequence[str], Mapping[str, Any]]]) -> float | None:
    """Profile-injection rate over the irrelevance probes among ``(retrieved_ids, meta)``
    pairs; ``None`` when there are none."""
    flags = [f for ids, meta in rows if (f := profile_injected(ids, meta)) is not None]
    return sum(flags) / len(flags) if flags else None


def context_repetition(retrieved: Sequence[Sequence[str]]) -> float | None:
    """Mean pairwise Jaccard overlap of the retrieved id sets of one diversity group;
    ``None`` with fewer than two non-empty sets."""
    sets = [set(ids) for ids in retrieved if ids]
    pairs = [len(a & b) / len(a | b) for a, b in itertools.combinations(sets, 2)]
    return sum(pairs) / len(pairs) if pairs else None
