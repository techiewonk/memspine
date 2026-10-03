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
  Its gold is the refusal (``ABSTENTION_GOLD``), never the file's
  ``adversarial_answer``, which is the distractor a fooled system gives (R3-1).
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
from ..judge import ABSTENTION_GOLD

_SESSION = re.compile(r"^session_(\d+)$")
_DIA = re.compile(r"^D(\d+):(\d+)$")

#: Recorded in every manifest, so runs before and after R3-1 cannot be confused.
CAT5_PROTOCOL_NOTE = (
    "cat5-gold=abstention-v1: cat 5 gold is the refusal "
    f"{ABSTENTION_GOLD!r}, never adversarial_answer; cat 5 has no R@k gold"
)


def _evidence_text(conversation: dict[str, Any], evidence: list[str]) -> str:
    """Evidence ids -> one ``Speaker`` + full-width colon + ``text`` line each, as
    LoCoMo-Plus's ``_evidence_to_text`` builds the judge's evidence (raw text, no caption)."""
    lines = []
    for evid in (part.strip() for e in evidence for part in e.split(";")):
        match = _DIA.match(evid)
        turns = (conversation.get(f"session_{match.group(1)}") or []) if match else []
        index = int(match.group(2)) - 1 if match else -1
        if 0 <= index < len(turns):
            turn = turns[index]
            lines.append(f"{turn.get('speaker', 'Unknown')}\uff1a{turn.get('text', '')}")
        elif evid:
            lines.append(f"[{evid}] [Missing turn]")
    return "\n".join(lines)


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
            notes=CAT5_PROTOCOL_NOTE,
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
                evidence = [str(e) for e in qa.get("evidence") or []]
                adversarial = category == 5
                meta: dict[str, Any] = {
                    "benchmark": "locomo",
                    "category": category,
                    "adversarial": adversarial,
                    "abstention": adversarial,
                    "judge_evidence": _evidence_text(conversation, evidence),
                }
                if adversarial:
                    # R3-1: cat 5 is unanswerable by construction. Its
                    # ``adversarial_answer`` is the distractor a fooled system gives,
                    # so it is kept for analysis only and never used as gold; nor is
                    # its evidence (the turn the distractor came from) gold for R@k.
                    meta["adversarial_answer"] = qa.get("adversarial_answer")
                    meta["distractor_evidence"] = evidence
                    gold: str | None = ABSTENTION_GOLD
                    gold_turns: tuple[str, ...] = ()
                else:
                    answer = qa.get("answer")
                    gold = None if answer is None else str(answer)
                    gold_turns = tuple(evidence)
                queries.append(
                    Query(
                        query_id=f"{index}-{q_index}",
                        text=str(qa.get("question", "")),
                        gold=gold,
                        gold_turn_ids=gold_turns,
                        type_label=f"cat{category}" if category is not None else None,
                        meta=meta,
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
