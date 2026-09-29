"""A small deterministic dataset, generated in memory.

Its purpose is to prove the harness end-to-end without a download, a model or a
licence question: planted facts, known gold turns, exact-match answers. Every
number it produces is a property of the *harness*, never of a memory system,
and its ``dataset_id`` says so — ``synthetic-smoke`` must never appear in a
score matrix row that is quoted anywhere.
"""

from __future__ import annotations

import random
from collections.abc import Iterator

from ..contracts import DatasetInfo, EvalItem, Query, Turn, sha256_text

FACTS = [
    ("favourite city", "Lisbon"),
    ("allergy", "walnuts"),
    ("sibling name", "Priya"),
    ("first car", "a blue Fiat"),
    ("preferred contact time", "after 6pm"),
    ("gym day", "Thursday"),
    ("coffee order", "flat white"),
    ("home town", "Nagpur"),
]

FILLER = [
    "The weather has been unpredictable this week.",
    "I finally finished that book you recommended.",
    "Work is busy but nothing unusual.",
    "We watched a documentary about deep-sea vents.",
    "The neighbours are renovating again.",
    "I tried a new recipe and it went badly.",
    "Traffic was terrible this morning.",
    "I might start running in the evenings.",
]


class SyntheticDataset:
    """Planted facts in a haystack of filler turns."""

    def __init__(
        self,
        n_items: int = 3,
        turns_per_item: int = 24,
        facts_per_item: int = 3,
        seed: int = 7,
    ) -> None:
        self.n_items = n_items
        self.turns_per_item = turns_per_item
        self.facts_per_item = min(facts_per_item, len(FACTS))
        self.seed = seed
        self._items = list(self._generate())

    def info(self) -> DatasetInfo:
        payload = "".join(turn.text for item in self._items for turn in item.history) + "".join(
            q.text for item in self._items for q in item.queries
        )
        return DatasetInfo(
            dataset_id="synthetic-smoke",
            revision_id=f"gen-v1-seed{self.seed}-n{self.n_items}x{self.turns_per_item}",
            licence="none (generated in memory)",
            source_path="memspine_evals.datasets.synthetic",
            content_sha256=sha256_text(payload),
            n_items=len(self._items),
            n_queries=sum(len(item.queries) for item in self._items),
            subset="smoke",
            notes="harness self-test only — never quote as a system result",
        )

    def items(self) -> Iterator[EvalItem]:
        yield from self._items

    def _generate(self) -> Iterator[EvalItem]:
        rng = random.Random(self.seed)
        for i in range(self.n_items):
            facts = rng.sample(FACTS, self.facts_per_item)
            positions = sorted(rng.sample(range(self.turns_per_item), self.facts_per_item))
            placed: dict[int, tuple[str, str]] = dict(zip(positions, facts, strict=True))
            history: list[Turn] = []
            gold: dict[str, str] = {}
            for t in range(self.turns_per_item):
                session = f"s{t // 8 + 1}"
                turn_id = f"item{i}-t{t:03d}"
                if t in placed:
                    key, value = placed[t]
                    text = f"By the way, my {key} is {value}."
                    gold[key] = turn_id
                    speaker = "user"
                else:
                    text = FILLER[(t + i) % len(FILLER)]
                    speaker = "user" if t % 2 == 0 else "assistant"
                history.append(
                    Turn(
                        turn_id=turn_id,
                        session_id=session,
                        speaker=speaker,
                        text=text,
                        timestamp=f"2026-01-{t % 28 + 1:02d}",
                    )
                )
            queries = tuple(
                Query(
                    query_id=f"item{i}-q{j}",
                    text=f"What is my {key}?",
                    gold=value,
                    gold_turn_ids=(gold[key],),
                    type_label="single-session-user",
                )
                for j, (key, value) in enumerate(facts)
            )
            yield EvalItem(item_id=f"item{i}", history=tuple(history), queries=queries)
