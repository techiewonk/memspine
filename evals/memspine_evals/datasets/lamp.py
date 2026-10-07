"""LaMP-2 (movie tagging) adapter, retrieval-only (Salemi et al., ACL 2024; LaMP dev split).

**Licence:** no licence file in the release (derived from MovieLens / source corpora);
research use only. Data lives at ``evals/data/lamp/`` as the dev pair
``LaMP_2_new_dev_dev_questions.json`` (list of ``{id, input, profile: [{id, tag,
description}]}``) and ``LaMP_2_new_dev_dev_outputs.json`` (``{task, golds: [{id,
output}]}``). Never downloaded by the harness.

Mapping to the harness contract:

- one ``EvalItem`` per question (each question carries its own user profile); every
  profile item is a ``Turn`` (``turn_id`` = ``"<qid>:p<profile id>"``, ``session_id`` =
  ``"profile"``, ``speaker`` = ``"user"``, ``text`` = the movie description only; the tag
  is kept out of the text and stored in ``Turn.meta["tag"]``);
- one ``Query``: ``text`` = the movie description after ``description:`` in ``input`` (the
  instruction preamble and tag list are dropped, they are reader material), ``gold`` = the
  gold tag, ``type_label`` = the gold tag;
- ``Query.meta["labels"]`` maps every profile ``turn_id`` to its tag, so a result row's
  ``retrieved_ids`` can be scored with :func:`knn.knn_vote` (free kNN tag-vote accuracy).

**Gold evidence is a proxy, stated as such.** LaMP has no retrieval gold. ``gold_turn_ids``
= the profile items that carry the gold tag ("label-consistent neighbours"); R@k over them
reads "does retrieval surface at least one of the user's own items with the right tag".
Questions whose profile has no item with the gold tag keep empty gold (R@k = None).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..contracts import DatasetInfo, EvalItem, Query, Turn
from .locomo import file_sha256

__all__ = ["LAMP2_FILES", "LaMP2Dataset", "lamp2_query_text"]

LAMP2_FILES = ("LaMP_2_new_dev_dev_questions.json", "LaMP_2_new_dev_dev_outputs.json")


def lamp2_query_text(raw_input: str) -> str:
    """The movie description: the text after the last ``description:``, else the input."""
    marker = "description:"
    lower = raw_input.lower()
    cut = lower.rfind(marker)
    return raw_input[cut + len(marker) :].strip() if cut >= 0 else raw_input.strip()


class LaMP2Dataset:
    """LaMP-2 dev questions + outputs from a local directory. Never downloads."""

    def __init__(
        self,
        root: str | Path,
        revision_id: str,
        max_questions: int | None = None,
        licence: str = "no licence file (LaMP); research use only",
    ) -> None:
        self.root = Path(root)
        self._q_path, self._o_path = (self.root / name for name in LAMP2_FILES)
        for path in (self._q_path, self._o_path):
            if not path.exists():
                raise FileNotFoundError(f"{path} not found; fetch LaMP-2 dev first")
        self._sha = file_sha256(self._q_path)[:32] + file_sha256(self._o_path)[:32]
        self.revision_id = f"sha256:{self._sha[:16]}" if revision_id == "auto" else revision_id
        self.licence = licence
        self.max_questions = max_questions
        self._items = list(self._build())

    def info(self) -> DatasetInfo:
        return DatasetInfo(
            dataset_id="lamp2",
            revision_id=self.revision_id,
            licence=self.licence,
            source_path=str(self.root),
            content_sha256=self._sha,
            n_items=len(self._items),
            n_queries=len(self._items),
            subset="dev" if self.max_questions is None else f"dev,max={self.max_questions}",
            notes=(
                "retrieval-only: kNN tag vote over retrieved profile ids (knn.knn_vote); "
                "gold_turn_ids = profile items with the gold tag (label-consistent proxy)"
            ),
        )

    def items(self) -> Iterator[EvalItem]:
        yield from self._items

    def _build(self) -> Iterator[EvalItem]:
        questions: list[dict[str, Any]] = json.loads(self._q_path.read_text(encoding="utf-8"))
        outputs = json.loads(self._o_path.read_text(encoding="utf-8"))
        golds = {str(g["id"]): str(g["output"]) for g in outputs.get("golds") or []}
        for n, question in enumerate(questions):
            if self.max_questions is not None and n >= self.max_questions:
                return
            qid = str(question.get("id", n))
            gold = golds.get(qid)
            history: list[Turn] = []
            labels: dict[str, str] = {}
            for p_index, item in enumerate(question.get("profile") or []):
                tid = f"{qid}:p{item.get('id', p_index)}"
                tag = str(item.get("tag", ""))
                history.append(
                    Turn(
                        turn_id=tid,
                        session_id="profile",
                        speaker="user",
                        text=str(item.get("description", "")),
                        meta={"tag": tag},
                    )
                )
                labels[tid] = tag
            gold_ids = tuple(t for t, tag in labels.items() if gold is not None and tag == gold)
            if not history or gold is None:
                continue
            yield EvalItem(
                item_id=qid,
                history=tuple(history),
                queries=(
                    Query(
                        query_id=qid,
                        text=lamp2_query_text(str(question.get("input", ""))),
                        gold=gold,
                        gold_turn_ids=gold_ids,
                        type_label=gold,
                        meta={"benchmark": "lamp2", "labels": labels, "gold_is_proxy": True},
                    ),
                ),
            )
