"""LoCoMo-Plus adapter (arXiv 2602.10715; github xjtuleeyf/Locomo-Plus @ 059f4e3).

LoCoMo-Plus tests *implicit* recall. A short cue dialogue that carries a causal, state, goal
or value constraint is planted in a LoCoMo conversation. A later trigger message, filtered
for low lexical and embedding similarity to the cue, must be answered in a way that adapts to
that constraint. There is no gold answer string; the judge receives the cue as evidence.

The construction follows the repo's deterministic ``data/unified_input.py`` and
``build_conv.build_context``, **not** ``build_conv.py``'s ``__main__``, whose ``random.choice``
is unseeded:
- plus item *i* is paired with LoCoMo conversation ``i mod len(locomo)``;
- the query time is the last session time + 7 days;
- the cue is placed at ``query_time - time_gap`` (weeks = 7 days, months = 30, years = 365);
- A/B speakers are mapped to the conversation's ``speaker_a`` / ``speaker_b``.

Each item is one ingestion (401 items): the full LoCoMo history plus the cue turns, with the
trigger as the single query. ``gold_turn_ids`` are the cue turns, so R@k is computable here;
``gold`` carries the cue text for the judge. The repo has **no licence**: run only, never
redistribute.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

from ..contracts import DatasetInfo, EvalItem, Query, Turn
from .locomo import file_sha256

__all__ = ["LoCoMoPlusDataset"]

_NUM_WORD = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}
_GAP = re.compile(
    r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|a|an)\b\s*"
    r"(week|weeks|month|months|year|years)\b"
)
_SESSION = re.compile(r"^session_(\d+)$")
_STAMP_FMT = "%I:%M %p on %d %B, %Y"  # LoCoMo: "1:56 pm on 8 May, 2023"


def gap_days(time_gap: str) -> int:
    """Port of ``build_conv.parse_time_gap``: weeks 7, months 30, years 365; else 0."""
    m = _GAP.search(time_gap.lower().strip())
    if not m:
        return 0
    num, unit = m.groups()
    count = int(num) if num.isdigit() else 1 if num in ("a", "an") else _NUM_WORD.get(num, 0)
    if unit.startswith("week"):
        return count * 7
    if unit.startswith("month"):
        return count * 30
    return count * 365


def _ab_turns(text: str, speaker_a: str, speaker_b: str) -> list[tuple[str, str]]:
    out = []
    for line in (text or "").split("\n"):
        line = line.strip()
        if line.startswith("A:"):
            out.append((speaker_a, line[2:].strip()))
        elif line.startswith("B:"):
            out.append((speaker_b, line[2:].strip()))
    return out


class LoCoMoPlusDataset:
    def __init__(
        self,
        plus_path: str | Path,
        locomo_path: str | Path,
        revision_id: str,
        licence: str = "no licence in repo (run only; do not redistribute)",
    ) -> None:
        self.plus_path, self.locomo_path = Path(plus_path), Path(locomo_path)
        for path in (self.plus_path, self.locomo_path):
            if not path.exists():
                raise FileNotFoundError(f"{path} not found; fetch LoCoMo-Plus first")
        self._sha = file_sha256(self.plus_path)
        self.revision_id = f"sha256:{self._sha[:16]}" if revision_id == "auto" else revision_id
        self.licence = licence
        self._plus = json.loads(self.plus_path.read_text(encoding="utf-8"))
        self._locomo = json.loads(self.locomo_path.read_text(encoding="utf-8"))
        self._items = list(self._build())

    def info(self) -> DatasetInfo:
        return DatasetInfo(
            dataset_id="locomo_plus",
            revision_id=self.revision_id,
            licence=self.licence,
            source_path=str(self.plus_path),
            content_sha256=self._sha,
            n_items=len(self._items),
            n_queries=len(self._items),
            subset="full",
        )

    def items(self) -> Iterator[EvalItem]:
        yield from self._items

    def _build(self) -> Iterator[EvalItem]:
        for index, plus in enumerate(self._plus):
            sample = self._locomo[index % len(self._locomo)]
            conv = sample.get("conversation", {})
            speaker_a, speaker_b = str(conv.get("speaker_a")), str(conv.get("speaker_b"))
            sessions = sorted((int(m.group(1)), key) for key in conv if (m := _SESSION.match(key)))
            timed: list[tuple[datetime, list[Turn]]] = []
            last = None
            for number, key in sessions:
                raw = str(conv.get(f"{key}_date_time", ""))
                when = datetime.strptime(raw, _STAMP_FMT)
                last = when
                turns = []
                for position, turn in enumerate(conv.get(key) or []):
                    text = str(turn.get("text", ""))
                    if turn.get("blip_caption"):
                        text = f"{text} [image: {turn['blip_caption']}]".strip()
                    turns.append(
                        Turn(
                            turn_id=str(turn.get("dia_id") or f"{key}:{position}"),
                            session_id=f"session_{number}",
                            speaker=str(turn.get("speaker")),
                            text=text,
                            timestamp=raw,
                        )
                    )
                timed.append((when, turns))
            assert last is not None
            query_time = last + timedelta(days=7)
            cue_time = query_time - timedelta(days=gap_days(str(plus.get("time_gap", ""))))
            cue_stamp = cue_time.strftime("%I:%M %p on %d %B, %Y")
            cue = [
                Turn(
                    turn_id=f"CUE:{k}",
                    session_id="session_cue",
                    speaker=spk,
                    text=txt,
                    timestamp=cue_stamp,
                )
                for k, (spk, txt) in enumerate(
                    _ab_turns(str(plus.get("cue_dialogue", "")), speaker_a, speaker_b), 1
                )
            ]
            timed.append((cue_time, cue))
            timed.sort(key=lambda pair: pair[0])  # stable: the cue follows an equal-time session
            history = tuple(t for _, turns in timed for t in turns)
            trigger = _ab_turns(str(plus.get("trigger_query", "")), speaker_a, speaker_b)
            trigger_text = " ".join(f"{spk}: {txt}" for spk, txt in trigger)
            evidence = "\n".join(f"{t.speaker}: {t.text}" for t in cue)
            yield EvalItem(
                item_id=f"plus-{index}",
                history=history,
                queries=(
                    Query(
                        query_id=f"plus-{index}",
                        text=trigger_text,
                        gold=evidence,
                        gold_turn_ids=tuple(t.turn_id for t in cue),
                        type_label=str(plus.get("relation_type", "")) or None,
                        meta={
                            "benchmark": "locomo_plus",
                            "time_gap": plus.get("time_gap"),
                            "query_time": query_time.strftime("%Y-%m-%d %H:%M"),
                            "question_date": query_time.strftime("%Y-%m-%d %H:%M"),
                            "base_conversation": sample.get("sample_id"),
                            # the official judge's evidence: cue turns, speaker and text
                            # joined by a full-width colon
                            # (``unified_input._cue_dialogue_to_evidence``)
                            "judge_evidence": "\n".join(
                                f"{t.speaker}\uff1a{t.text.strip()}" for t in cue if t.text
                            ),
                        },
                    ),
                ),
                meta={"speakers": [speaker_a, speaker_b]},
            )
