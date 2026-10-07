"""BEAM adapter, retrieval-only ("Beyond a Million Tokens", ICLR 2026).

Sources, checked 2026-10-07:

- data: Hugging Face ``Mohammadta/BEAM`` @ ``3205395`` (splits 100K / 500K / 1M; one parquet
  file per split, ``data/<split>-00000-of-00001.parquet``). Locally only the 100K split is
  held (20 conversations, 400 probes). ``Mohammadta/BEAM-10M`` is a separate repo, not fetched.
- **Licence: CC BY-SA 4.0.** Derived copies carry the same licence.

Format (one row per conversation): ``conversation_id``, ``chat`` (a list of sessions, each a
list of messages ``{id, index, role, content, question_type, time_anchor}``; ``id`` is unique
within the conversation and ``time_anchor`` such as ``"March-15-2024"`` is set on a session's
first message only), and ``probing_questions``, a *stringified* dict (Python literal) mapping
each of ten abilities to a list of probes. Probes carry ``source_chat_ids``: a list of message
ids, or a dict for three abilities (``knowledge_update``: ``original_info`` / ``updated_info``;
``contradiction_resolution``: ``first_statement`` / ``second_statement``;
``temporal_reasoning``: ``first_event`` / ``second_event``). Abstention probes have none.

Mapping to the harness contract:

- one ``EvalItem`` per conversation (``item_id`` = ``conversation_id``); every message is a
  ``Turn`` (``turn_id`` = ``"<conversation_id>:<id>"``, ``session_id`` = ``"s<n>"``, speaker
  = role, timestamp = the session's time anchor carried forward, as an ISO date);
- one ``Query`` per probe (``query_id`` = ``"<cid>:<ability>:<n>"``), all asked after the
  whole history; ``type_label`` = the ability; ``meta["sub_type"]`` = the probe's own
  ``*_type`` field;
- ``gold_turn_ids`` = the ``source_chat_ids``, so the harness's coverage and R@k apply.
  **Knowledge update:** gold is ``updated_info`` only (the evidence for the right answer);
  ``original_info`` (the stale value) is kept in ``meta["stale_turn_ids"]``. Contradiction
  and temporal probes need both statements: their gold is both, read with ``R_all@k``;
- ``gold`` = ``answer`` / ``ideal_answer`` / ``ideal_response`` / ``ideal_summary`` /
  ``expected_compliance``, whichever the ability uses (it is not a substring target: the
  official scorer is an LLM rubric judge).

Free measures (no model call) beyond R@k: :func:`update_order` (the current value ranked
above the stale one: the KU order rate) and :func:`contradiction_pair_recall` (both sides of
a contradiction retrieved within k).

**Gaps:** the official QA score is an LLM judge over ``rubric`` (not reproduced); event
ordering is scored on the answer's order, not on retrieval; abstention has no retrieval gold
(``meta["abstention"]`` is true and gold ids are empty).
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from ..contracts import DatasetInfo, EvalItem, Query, Turn
from .locomo import file_sha256

__all__ = [
    "BEAM_ABILITIES",
    "BEAM_SOURCE",
    "BEAMDataset",
    "contradiction_pair_recall",
    "update_order",
]

BEAM_SOURCE = {
    "data": "https://huggingface.co/datasets/Mohammadta/BEAM",
    "data_revision": "3205395",
    "paper": "Beyond a Million Tokens (ICLR 2026)",
    "licence": "CC BY-SA 4.0",
}

BEAM_ABILITIES = (
    "abstention",
    "contradiction_resolution",
    "event_ordering",
    "information_extraction",
    "instruction_following",
    "knowledge_update",
    "multi_session_reasoning",
    "preference_following",
    "summarization",
    "temporal_reasoning",
)

_GOLD_KEYS = ("answer", "ideal_answer", "ideal_response", "ideal_summary", "expected_compliance")


def _flat(value: Any) -> Iterator[Any]:
    if isinstance(value, dict):
        for v in value.values():
            yield from _flat(v)
    elif isinstance(value, list | tuple):
        for v in value:
            yield from _flat(v)
    elif value is not None:
        yield value


def _probes(raw: Any) -> dict[str, list[dict[str, Any]]]:
    if isinstance(raw, dict):
        return raw
    text = str(raw or "{}")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return ast.literal_eval(text)  # the release stores a Python-literal dict


def _iso(anchor: str | None) -> str | None:
    if not anchor:
        return None
    for fmt in ("%B-%d-%Y", "%b-%d-%Y", "%B %d, %Y"):
        try:
            return datetime.strptime(anchor, fmt).date().isoformat()
        except ValueError:
            continue
    return anchor


def update_order(retrieved_ids: Sequence[str], query: Query) -> bool | None:
    """KU order: True when the first current (``updated_info``) turn ranks above the first
    stale (``original_info``) turn; a retrieved current turn with no stale one counts True.
    None when the probe is not a knowledge update or neither side was retrieved."""
    stale = set(query.meta.get("stale_turn_ids") or ())
    current = set(query.gold_turn_ids)
    if not stale or not current:
        return None
    first_current = next((i for i, t in enumerate(retrieved_ids) if t in current), None)
    first_stale = next((i for i, t in enumerate(retrieved_ids) if t in stale), None)
    if first_current is None and first_stale is None:
        return None
    if first_stale is None:
        return True
    return first_current is not None and first_current < first_stale


def contradiction_pair_recall(retrieved_ids: Sequence[str], query: Query, k: int) -> bool | None:
    """Both statements of a contradiction probe within the top ``k``; None otherwise."""
    pair = query.meta.get("contradiction_pair")
    if not pair:
        return None
    top = set(retrieved_ids[:k])
    return all(any(t in top for t in side) for side in pair)


class BEAMDataset:
    """One BEAM split, read from its local parquet file. Never downloads."""

    def __init__(
        self,
        path: str | Path,
        revision_id: str,
        abilities: tuple[str, ...] | None = None,
        max_conversations: int | None = None,
        licence: str = "CC BY-SA 4.0 (Mohammadta/BEAM)",
    ) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"{self.path} not found; fetch BEAM first (evals/README.md)")
        self._sha = file_sha256(self.path)
        self.revision_id = f"sha256:{self._sha[:16]}" if revision_id == "auto" else revision_id
        self.licence = licence
        self.abilities = abilities
        self.max_conversations = max_conversations
        self.split = self.path.name.split("-")[0]
        import pyarrow.parquet as pq  # eval-only dependency

        self._rows: list[dict[str, Any]] = pq.read_table(self.path).to_pylist()
        self._items = list(self._build())

    def info(self) -> DatasetInfo:
        subset = self.split
        if self.abilities:
            subset += f",abilities={'/'.join(self.abilities)}"
        if self.max_conversations is not None:
            subset += f",max_conversations={self.max_conversations}"
        return DatasetInfo(
            dataset_id=f"beam_{self.split.lower()}",
            revision_id=self.revision_id,
            licence=self.licence,
            source_path=str(self.path),
            content_sha256=self._sha,
            n_items=len(self._items),
            n_queries=sum(len(i.queries) for i in self._items),
            subset=subset,
            notes="retrieval-only: source_chat_ids R@k; KU gold = updated_info; QA judge not run",
        )

    def items(self) -> Iterator[EvalItem]:
        yield from self._items

    def _build(self) -> Iterator[EvalItem]:
        for n_row, row in enumerate(self._rows):
            if self.max_conversations is not None and n_row >= self.max_conversations:
                return
            cid = str(row.get("conversation_id") or n_row)
            history: list[Turn] = []
            anchor: str | None = None
            for s_index, session in enumerate(row.get("chat") or []):
                for msg in session or []:
                    anchor = _iso(msg.get("time_anchor")) or anchor
                    history.append(
                        Turn(
                            turn_id=f"{cid}:{msg.get('id')}",
                            session_id=f"s{s_index}",
                            speaker=str(msg.get("role", "")),
                            text=str(msg.get("content", "")),
                            timestamp=anchor,
                            meta={"index": msg.get("index")},
                        )
                    )
            known = {t.turn_id for t in history}

            def tids(ids: Any, known: set[str] = known, cid: str = cid) -> tuple[str, ...]:
                out = (f"{cid}:{i}" for i in _flat(ids))
                return tuple(dict.fromkeys(t for t in out if t in known))

            queries: list[Query] = []
            for ability, probes in _probes(row.get("probing_questions")).items():
                if self.abilities and ability not in self.abilities:
                    continue
                for p_index, probe in enumerate(probes or []):
                    source = probe.get("source_chat_ids")
                    meta: dict[str, Any] = {
                        "benchmark": "beam",
                        "sub_type": next(
                            (v for k, v in probe.items() if k.endswith("_type")), None
                        ),
                        "difficulty": probe.get("difficulty"),
                        "rubric": list(probe.get("rubric") or []),
                        "abstention": ability == "abstention",
                    }
                    if ability == "knowledge_update" and isinstance(source, dict):
                        gold_ids = tids(source.get("updated_info"))
                        meta["stale_turn_ids"] = list(tids(source.get("original_info")))
                    else:
                        gold_ids = tids(source)
                    if ability == "contradiction_resolution" and isinstance(source, dict):
                        meta["contradiction_pair"] = [
                            list(tids(source.get("first_statement"))),
                            list(tids(source.get("second_statement"))),
                        ]
                    gold = next((probe[k] for k in _GOLD_KEYS if probe.get(k)), None)
                    queries.append(
                        Query(
                            query_id=f"{cid}:{ability}:{p_index}",
                            text=str(probe.get("question", "")),
                            gold=None if gold is None else str(gold),
                            gold_turn_ids=gold_ids,
                            type_label=ability,
                            meta=meta,
                        )
                    )
            if history and queries:
                yield EvalItem(
                    item_id=cid,
                    history=tuple(history),
                    queries=tuple(queries),
                    meta={
                        "split": self.split,
                        "category": (row.get("conversation_seed") or {}).get("category"),
                    },
                )
