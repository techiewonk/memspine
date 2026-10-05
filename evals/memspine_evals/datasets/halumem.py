"""HaluMem adapter: the Memory QA task only (arXiv 2511.03506).

Sources, checked 2026-10-05:

- data: Hugging Face ``IAAR-Shanghai/HaluMem`` (repo head ``cb04336aa1b7``; the two data
  files ``HaluMem-Medium.jsonl`` and ``HaluMem-Long.jsonl`` were last changed in commit
  ``6c4dbab``). **Licence: CC BY-NC-ND 4.0.** Research use only; never redistribute samples
  or derived copies.
- code: github ``MemTensor/HaluMem``, ``README.md`` (schema) and ``eval/evaluation.py``
  (metric definitions). The commit hash of the code repo was not recorded; pin it when the
  first run is made.

Format, as given in the README: one JSON object per line, one per user::

    {"uuid", "persona_info", "sessions": [
        {"start_time", "end_time", "dialogue_turn_num", "dialogue_token_length",
         "dialogue": [{"role": "user"|"assistant", "content", "timestamp", "dialogue_turn"}],
         "memory_points": [{"index", "memory_content", "memory_type", "memory_source",
                            "is_update", "original_memories", "importance", "timestamp"}],
         "questions": [{"question", "answer", "difficulty", "question_type",
                        "evidence": [{"memory_content", "memory_type"}]}]}]}

Mapping to the harness contract:

- one ``EvalItem`` per user (``item_id`` = ``uuid``); every dialogue turn is a ``Turn``
  (``turn_id`` = ``"<uuid>:s<session>:t<dialogue_turn>"``, ``session_id`` =
  ``"s<session>"``, speaker = the role, timestamp = the turn's own);
- one ``Query`` per question, pinned with ``after_turn`` to the last turn of the session
  that carries it: ``eval/evaluation.py`` iterates users -> sessions -> questions, so a
  session's questions follow that session;
- ``gold`` = ``answer``; ``type_label`` = ``question_type``;
- ``meta["key_memory_points"]`` = the evidence ``memory_content`` lines joined by newlines,
  which the official judge receives next to the reference answer.

**Gaps, stated rather than guessed:**

1. ``gold_turn_ids`` stay empty. Evidence is given as memory points, not turn ids, so turn
   R@k is not computable from the release.
2. The official QA judge is an LLM (``eval_tools.evaluation_for_question``) that labels
   each answer ``Correct``, ``Hallucination`` or ``Omission``; the judge model comes from
   the ``OPENAI_MODEL`` environment variable and is not fixed by the benchmark. Its prompt
   text is not vendored here. The QA metrics are the three label shares, over all
   questions and over validly judged ones. A run must name its judge model and prompt hash.
3. The Memory Extraction and Memory Updating tasks score the system's extracted memory
   list against ``memory_points``. The harness's ``SystemAdapter`` has no verb that exposes
   such a list, so those tasks are not adapted. ``memory_points`` are kept in item meta.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..contracts import DatasetInfo, EvalItem, Query, Turn
from .locomo import file_sha256

__all__ = ["HALUMEM_LABELS", "HALUMEM_SOURCE", "HaluMemDataset"]

#: The official QA judge's three labels (``eval/evaluation.py``).
HALUMEM_LABELS = ("Correct", "Hallucination", "Omission")

HALUMEM_SOURCE = {
    "data": "https://huggingface.co/datasets/IAAR-Shanghai/HaluMem",
    "data_revision": "cb04336aa1b732d4b24f5186c552456b4099806e",
    "code": "https://github.com/MemTensor/HaluMem",
    "paper": "arXiv 2511.03506",
    "licence": "CC BY-NC-ND 4.0",
}

_VARIANTS = {"medium": "HaluMem-Medium.jsonl", "long": "HaluMem-Long.jsonl"}


class HaluMemDataset:
    """HaluMem-Medium or -Long, read from a local ``.jsonl`` file. Never downloads."""

    def __init__(
        self,
        path: str | Path,
        revision_id: str,
        max_users: int | None = None,
        licence: str = "CC BY-NC-ND 4.0 (IAAR-Shanghai/HaluMem); research only, no redistribution",
    ) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"{self.path} not found; fetch HaluMem first (evals/README.md)")
        self._sha = file_sha256(self.path)
        self.revision_id = f"sha256:{self._sha[:16]}" if revision_id == "auto" else revision_id
        self.licence = licence
        self.max_users = max_users
        name = self.path.name
        self.variant = next((k for k, v in _VARIANTS.items() if v == name), "unknown")
        self._items = list(self._build())

    def info(self) -> DatasetInfo:
        return DatasetInfo(
            dataset_id=f"halumem_{self.variant}",
            revision_id=self.revision_id,
            licence=self.licence,
            source_path=str(self.path),
            content_sha256=self._sha,
            n_items=len(self._items),
            n_queries=sum(len(i.queries) for i in self._items),
            subset="qa" if self.max_users is None else f"qa,max_users={self.max_users}",
            notes=(
                "Memory QA task only; official judge labels Correct/Hallucination/Omission "
                "(LLM, model not fixed by the benchmark); no turn-level retrieval gold"
            ),
        )

    def items(self) -> Iterator[EvalItem]:
        yield from self._items

    def _users(self) -> Iterator[dict[str, Any]]:
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)

    def _build(self) -> Iterator[EvalItem]:
        for n_user, user in enumerate(self._users()):
            if self.max_users is not None and n_user >= self.max_users:
                return
            uid = str(user.get("uuid") or f"user{n_user}")
            history: list[Turn] = []
            queries: list[Query] = []
            memory_points: list[dict[str, Any]] = []
            for s_index, session in enumerate(user.get("sessions") or []):
                sid = f"s{s_index}"
                last: str | None = None
                for position, turn in enumerate(session.get("dialogue") or []):
                    number = turn.get("dialogue_turn", position)
                    role = str(turn.get("role", ""))
                    tid = f"{uid}:{sid}:t{number}:{role}"
                    history.append(
                        Turn(
                            turn_id=tid,
                            session_id=sid,
                            speaker=role,
                            text=str(turn.get("content", "")),
                            timestamp=turn.get("timestamp") or session.get("start_time"),
                        )
                    )
                    last = tid
                memory_points.extend(session.get("memory_points") or [])
                for q_index, qa in enumerate(session.get("questions") or []):
                    evidence = [str(e.get("memory_content", "")) for e in qa.get("evidence") or []]
                    queries.append(
                        Query(
                            query_id=f"{uid}:{sid}:q{q_index}",
                            text=str(qa.get("question", "")),
                            gold=str(qa.get("answer", "")),
                            type_label=str(qa.get("question_type") or "") or None,
                            after_turn=last,
                            meta={
                                "benchmark": "halumem",
                                "difficulty": qa.get("difficulty"),
                                "key_memory_points": "\n".join(e for e in evidence if e),
                                "official_labels": list(HALUMEM_LABELS),
                            },
                        )
                    )
            if history and queries:
                yield EvalItem(
                    item_id=uid,
                    history=tuple(history),
                    queries=tuple(queries),
                    meta={"n_memory_points": len(memory_points), "memory_points": memory_points},
                )
