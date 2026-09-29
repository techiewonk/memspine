"""LoCoMo adapter (tier 1, A4-5).

Shape of the public release (``locomo10.json``): a list of samples, each with a
``conversation`` object holding ``session_N`` lists of turns and
``session_N_date_time`` stamps, plus a ``qa`` list whose entries carry
``question``, ``answer``, ``evidence`` (turn ids such as ``"D1:2"``) and
``category``.

Two properties of this benchmark are load-bearing for the survey and are
therefore surfaced rather than smoothed over:

* ``category`` 5 is the *adversarial* class, where the correct answer is a
  refusal. Pooling it with the factual categories changes a headline; the
  harness keeps it as ``type_label`` so any run can be re-read per category.
* ``evidence`` gives real retrieval ground truth, so R@k is computable here —
  which is what makes LoCoMo the place to test whether MemPalace's verbatim
  advantage transfers (their own LoCoMo figure is 60.3% R@10 against 96.6% on
  LongMemEval).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..contracts import DatasetInfo, EvalItem, Query, Turn

_SESSION = re.compile(r"^session_(\d+)$")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class LoCoMoDataset:
    """Adapter over a local ``locomo10.json``.

    ``revision_id`` is required. Pass the release name if you know it; pass
    ``"auto"`` to identify the file by its own content hash, which is a weaker
    label but a stronger identifier than the name most papers omit entirely.
    """

    def __init__(
        self,
        path: str | Path,
        revision_id: str,
        licence: str = "see LoCoMo release (check before publishing a number)",
        subset: str = "full",
        categories: tuple[int, ...] | None = None,
    ) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(
                f"{self.path} not found — fetch LoCoMo first (see evals/README.md); "
                "the harness never downloads data on your behalf"
            )
        self._sha = file_sha256(self.path)
        self.revision_id = f"sha256:{self._sha[:16]}" if revision_id == "auto" else revision_id
        self.licence = licence
        self.subset = subset
        self.categories = categories
        self._raw: list[dict[str, Any]] = json.loads(self.path.read_text(encoding="utf-8"))
        if isinstance(self._raw, dict):  # some mirrors wrap the list
            self._raw = list(self._raw.values())
        self._items = list(self._build())

    def info(self) -> DatasetInfo:
        return DatasetInfo(
            dataset_id="locomo",
            revision_id=self.revision_id,
            licence=self.licence,
            source_path=str(self.path),
            content_sha256=self._sha,
            n_items=len(self._items),
            n_queries=sum(len(item.queries) for item in self._items),
            subset=self.subset
            if self.categories is None
            else f"{self.subset}+cat{'/'.join(map(str, self.categories))}",
        )

    def items(self) -> Iterator[EvalItem]:
        yield from self._items

    def _build(self) -> Iterator[EvalItem]:
        for index, sample in enumerate(self._raw):
            conversation = sample.get("conversation", {})
            sessions = sorted(
                (
                    (int(match.group(1)), key)
                    for key in conversation
                    if (match := _SESSION.match(key))
                ),
                key=lambda pair: pair[0],
            )
            history: list[Turn] = []
            for number, key in sessions:
                stamp = conversation.get(f"{key}_date_time")
                for position, turn in enumerate(conversation.get(key) or []):
                    turn_id = str(turn.get("dia_id") or f"{key}:{position}")
                    text = str(turn.get("text", ""))
                    caption = turn.get("blip_caption")
                    if caption:
                        # Image turns carry their caption as the only textual
                        # evidence; dropping it silently loses gold turns.
                        text = f"{text} [image: {caption}]".strip()
                    history.append(
                        Turn(
                            turn_id=turn_id,
                            session_id=f"session_{number}",
                            speaker=str(turn.get("speaker", "unknown")),
                            text=text,
                            timestamp=str(stamp) if stamp else None,
                        )
                    )
            queries: list[Query] = []
            for q_index, qa in enumerate(sample.get("qa") or []):
                category = qa.get("category")
                if self.categories is not None and category not in self.categories:
                    continue
                answer = qa.get("answer", qa.get("adversarial_answer"))
                evidence = qa.get("evidence") or []
                queries.append(
                    Query(
                        query_id=f"{index}-{q_index}",
                        text=str(qa.get("question", "")),
                        gold=None if answer is None else str(answer),
                        gold_turn_ids=tuple(str(e) for e in evidence),
                        type_label=f"cat{category}" if category is not None else None,
                        meta={"adversarial": category == 5},
                    )
                )
            if queries:
                yield EvalItem(
                    item_id=str(sample.get("sample_id", index)),
                    history=tuple(history),
                    queries=tuple(queries),
                    meta={
                        "speakers": [
                            conversation.get("speaker_a"),
                            conversation.get("speaker_b"),
                        ]
                    },
                )
