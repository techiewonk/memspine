"""MemoryAgentBench adapter (arXiv 2507.05257; HF ai-hyz/MemoryAgentBench @ 7ea0669, MIT).

Currently the **Conflict_Resolution** split (FactConsolidation, single- and multi-hop, at
several context sizes): the competency where verbatim content, visible order and supersession
matter (selective forgetting, "SF"). Each row is one context: a numbered fact list in which a
larger serial number means a newer fact (later facts can contradict earlier ones), followed by
questions with alias lists.

Mapping to the harness contract:
- one ``EvalItem`` per row; each numbered fact is a ``Turn`` (``turn_id`` = ``"<row>:<n>"``)
  with a synthetic event time that increases with the serial number, so newer facts are
  later in time (the benchmark's own ordering signal, made visible to temporal machinery);
- one ``Query`` per question; ``gold`` = the aliases joined by ``" || "`` (scored by the
  benchmark's substring match, see ``judge.AliasContainsJudge``);
- ``type_label`` = the qa_pair_id prefix, e.g. ``factconsolidation_mh_6k``;
- ``gold_turn_ids`` = the supporting *current* fact(s), recovered by ``mab_gold`` (relation
  templates + newest-serial-wins + a shortest chain for multi-hop), so R@k / coverage are
  computable; ``meta["stale_turn_ids"]`` / ``meta["stale_by_gold"]`` hold the superseded
  versions for the supersession-order metric (``mab_gold.supersession_order_rate``);
  ``meta["gold_mapping"]`` is ``mapped`` / ``ambiguous`` / ``unmapped`` (unmapped keeps empty
  gold rather than a guess). ``map_gold=False`` restores the gold-free behaviour.

Reading parquet needs ``pyarrow`` (an eval-only dependency, imported lazily).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..contracts import DatasetInfo, EvalItem, Query, Turn
from .locomo import file_sha256
from .mab_gold import Fact, build_index_mapper, parse_fact

__all__ = ["MemoryAgentBenchDataset"]

_FACT = re.compile(r"^\s*(\d+)\.\s+(.*\S)\s*$")
_BASE = datetime(2024, 1, 1, tzinfo=UTC)


class MemoryAgentBenchDataset:
    def __init__(
        self,
        path: str | Path,
        revision_id: str,
        split: str = "Conflict_Resolution",
        licence: str = "MIT (ai-hyz/MemoryAgentBench)",
        map_gold: bool = True,
        max_rows: int | None = None,
    ) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"{self.path} not found; fetch MemoryAgentBench first")
        if split != "Conflict_Resolution":
            raise NotImplementedError("only the Conflict_Resolution split is adapted so far")
        self.split = split
        self._sha = file_sha256(self.path)
        self.revision_id = f"sha256:{self._sha[:16]}" if revision_id == "auto" else revision_id
        self.licence = licence
        self.map_gold = map_gold
        import pyarrow.parquet as pq  # eval-only dependency

        self._rows: list[dict[str, Any]] = pq.read_table(self.path).to_pylist()[:max_rows]
        self._max_rows_note = None if max_rows is None else f"{split},max_rows={max_rows}"
        self._items = list(self._build())

    def info(self) -> DatasetInfo:
        return DatasetInfo(
            dataset_id=f"memoryagentbench_{self.split.lower()}",
            revision_id=self.revision_id,
            licence=self.licence,
            source_path=str(self.path),
            content_sha256=self._sha,
            n_items=len(self._items),
            n_queries=sum(len(i.queries) for i in self._items),
            subset=self.split if self._max_rows_note is None else self._max_rows_note,
            notes="gold=mab_gold-v1 (template parse, newest serial wins)" if self.map_gold else "",
        )

    def items(self) -> Iterator[EvalItem]:
        yield from self._items

    def _build(self) -> Iterator[EvalItem]:
        for row_index, row in enumerate(self._rows):
            history: list[Turn] = []
            facts: list[Fact] = []
            for line in str(row.get("context") or "").splitlines():
                m = _FACT.match(line)
                if not m:
                    continue
                serial = int(m.group(1))
                parsed = parse_fact(m.group(2))
                if parsed is not None:
                    facts.append(Fact(serial, f"{row_index}:{serial}", *parsed))
                history.append(
                    Turn(
                        turn_id=f"{row_index}:{serial}",
                        session_id=f"ctx{row_index}",
                        speaker="fact",
                        text=f"{serial}. {m.group(2)}",
                        timestamp=(_BASE + timedelta(minutes=serial)).isoformat(),
                    )
                )
            mapper = build_index_mapper(facts) if self.map_gold else None
            meta = row.get("metadata") or {}
            ids = list(meta.get("qa_pair_ids") or [])
            queries = []
            for q_index, (question, aliases) in enumerate(
                zip(row.get("questions") or [], row.get("answers") or [], strict=False)
            ):
                qid = ids[q_index] if q_index < len(ids) else f"{row_index}-{q_index}"
                label = re.sub(r"_no\d+$", "", qid)
                alias_list = [str(a) for a in (aliases or [])]
                gold_ids: tuple[str, ...] = ()
                q_meta: dict[str, Any] = {"benchmark": "memoryagentbench"}
                if mapper is not None:
                    mapping = mapper(str(question), alias_list, "_mh_" in label)
                    gold_ids = mapping.gold
                    q_meta.update(
                        gold_mapping=mapping.status,
                        hops=mapping.hops,
                        gold_facts=list(mapping.gold),
                        stale_turn_ids=list(mapping.stale),
                        stale_by_gold={g: list(st) for g, st in mapping.stale_by_gold},
                    )
                queries.append(
                    Query(
                        query_id=qid,
                        text=str(question),
                        gold=" || ".join(alias_list),
                        gold_turn_ids=gold_ids,
                        type_label=label,
                        meta=q_meta,
                    )
                )
            if os.environ.get("MEMSPINE_EVAL_MAX_QUERIES"):  # debugging: first N questions per item
                queries = queries[: int(os.environ["MEMSPINE_EVAL_MAX_QUERIES"])]
            if history and queries:
                label = queries[0].type_label or f"row{row_index}"
                yield EvalItem(item_id=label, history=tuple(history), queries=tuple(queries))
