"""LongMemEval adapter (tier 1, A4-5) — with the revision rule enforced.

The finding from ``research/2026-09-15/LONGMEMEVAL_VERSIONS_AUDIT.md``:
LongMemEval v1 was **re-released in September 2025 with cleaned histories** by
its own first author, the release "replaces the original", and nothing in the
literature marks which revision a published score used. Pre- and post-revision
scores therefore cannot be pooled, and a row without this field is dropped from
the score matrix under D16.

So this adapter refuses to be constructed without ``revision_id``, and the
recognised values are named: ``"2024-original"``, ``"2025-09-cleaned"``, or
``"auto"`` to fall back to the file's own content hash. Anything else is
accepted but recorded verbatim — the point is that *something* explicit is
always on the row.

(LongMemEval-V2 is a different benchmark — web-agent trajectories over
WebArena/WorkArena — and is out of census scope under D15. It gets its own
adapter in tier 3, not this one.)
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..contracts import DatasetInfo, DataShape, EvalItem, Query, Turn
from .locomo import file_sha256

KNOWN_REVISIONS = ("2024-original", "2025-09-cleaned", "auto")


class LongMemEvalDataset:
    """Adapter over ``longmemeval_s.json`` / ``_m`` / ``_oracle``.

    Each question owns its own haystack, so one question becomes one
    ``EvalItem``: history is inserted per item and never shared, which is what
    the benchmark intends and what makes the insert cost honest.
    """

    def __init__(
        self,
        path: str | Path,
        revision_id: str,
        variant: str = "s",
        licence: str = "MIT (cleaned release) — verify for the file you hold",
        subset: str = "full",
        question_types: tuple[str, ...] | None = None,
    ) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(
                f"{self.path} not found — fetch LongMemEval first (see evals/README.md)"
            )
        if not revision_id:
            raise ValueError(
                "LongMemEval needs an explicit revision_id — the September 2025 cleaned "
                f"release replaced the original. One of {KNOWN_REVISIONS}, or your own label."
            )
        self._sha = file_sha256(self.path)
        self.revision_id = f"sha256:{self._sha[:16]}" if revision_id == "auto" else revision_id
        self.variant = variant
        self.licence = licence
        self.subset = subset
        self.question_types = question_types
        raw: Any = json.loads(self.path.read_text(encoding="utf-8"))
        self._raw: list[dict[str, Any]] = raw if isinstance(raw, list) else list(raw.values())
        self._items = list(self._build())

    def info(self) -> DatasetInfo:
        return DatasetInfo(
            dataset_id=f"longmemeval-{self.variant}",
            revision_id=self.revision_id,
            licence=self.licence,
            source_path=str(self.path),
            content_sha256=self._sha,
            n_items=len(self._items),
            n_queries=sum(len(item.queries) for item in self._items),
            subset=self.subset
            if self.question_types is None
            else f"{self.subset}+types:{','.join(self.question_types)}",
            notes="one question per item; haystack inserted per item",
            shape=DataShape(
                has_timestamps=True,
                has_question_date=True,
                speaker_kind="user_assistant",
                language="en",
                abstention_possible=True,
            ),
        )

    def items(self) -> Iterator[EvalItem]:
        yield from self._items

    def _build(self) -> Iterator[EvalItem]:
        for index, sample in enumerate(self._raw):
            q_type = str(sample.get("question_type", ""))
            if self.question_types is not None and q_type not in self.question_types:
                continue
            question_id = str(sample.get("question_id", index))
            sessions = sample.get("haystack_sessions") or []
            session_ids = sample.get("haystack_session_ids") or []
            dates = sample.get("haystack_dates") or []
            answer_sessions = {str(s) for s in (sample.get("answer_session_ids") or [])}

            history: list[Turn] = []
            gold_turn_ids: list[str] = []
            for s_index, session in enumerate(sessions):
                session_id = str(
                    session_ids[s_index] if s_index < len(session_ids) else f"session_{s_index}"
                )
                stamp = str(dates[s_index]) if s_index < len(dates) else None
                for t_index, turn in enumerate(session or []):
                    turn_id = f"{session_id}:{t_index}"
                    history.append(
                        Turn(
                            turn_id=turn_id,
                            session_id=session_id,
                            speaker=str(turn.get("role", "user")),
                            text=str(turn.get("content", "")),
                            timestamp=stamp,
                        )
                    )
                    # ``has_answer`` is the per-turn label; the answer-session
                    # list is the coarser fallback for files that omit it.
                    if turn.get("has_answer") or (
                        "has_answer" not in turn and session_id in answer_sessions
                    ):
                        gold_turn_ids.append(turn_id)

            query = Query(
                query_id=question_id,
                text=str(sample.get("question", "")),
                gold=str(sample.get("answer")) if sample.get("answer") is not None else None,
                gold_turn_ids=tuple(gold_turn_ids),
                type_label=q_type or None,
                meta={
                    "benchmark": "longmemeval",
                    "question_date": sample.get("question_date"),
                    # LongMemEval marks abstention questions by a ``_abs`` suffix on the
                    # question id; their question_type is an ordinary type.
                    "abstention": question_id.endswith("_abs"),
                },
            )
            yield EvalItem(
                item_id=question_id,
                history=tuple(history),
                queries=(query,),
                meta={"n_sessions": len(sessions)},
            )
