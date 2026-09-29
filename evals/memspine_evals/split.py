"""Deterministic development / held-out splits, saved as ids.

`EVALUATION_PLAN_2026-09.md` §4: *"deterministically reserve two whole LoCoMo
conversations and forty LongMemEval question histories before tuning; save IDs
and category balance. A later complete official-set run must disclose
development overlap."*

Two properties matter and both are enforced here:

1. **Whole clusters, never questions.** Splitting by question puts paraphrases
   of the same history on both sides. The unit is the item — a conversation, a
   haystack — exactly as the cluster bootstrap treats it.
2. **The split is a file, not a seed you remember.** MemPalace publishes
   ``benchmarks/lme_split_50_450.json`` and reports the held-out figure beside
   the tuned one; that is the discipline being copied. A split that exists only
   as an argument cannot be audited later.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import DatasetAdapter, DatasetInfo, EvalItem


@dataclass(frozen=True, slots=True)
class Split:
    dataset_id: str
    revision_id: str
    content_sha256: str
    dev_items: tuple[str, ...]
    heldout_items: tuple[str, ...]
    seed: int
    created_at: str = ""
    category_balance: dict[str, dict[str, int]] = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["dev_items"] = list(self.dev_items)
        payload["heldout_items"] = list(self.heldout_items)
        return payload

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path

    @staticmethod
    def load(path: str | Path) -> Split:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return Split(
            dataset_id=payload["dataset_id"],
            revision_id=payload["revision_id"],
            content_sha256=payload["content_sha256"],
            dev_items=tuple(payload["dev_items"]),
            heldout_items=tuple(payload["heldout_items"]),
            seed=payload["seed"],
            created_at=payload.get("created_at", ""),
            category_balance=payload.get("category_balance", {}),
            note=payload.get("note", ""),
        )

    def check(self, info: DatasetInfo) -> None:
        """Refuse to apply a split to different bytes than it was made from."""
        if info.content_sha256 != self.content_sha256:
            raise ValueError(
                f"split was made from {self.dataset_id}@{self.revision_id} "
                f"(sha {self.content_sha256[:12]}), but this file hashes "
                f"{info.content_sha256[:12]} — a split does not transfer across data revisions"
            )


def _rank(item_id: str, seed: int) -> str:
    """Stable pseudo-random order that depends only on the id and the seed, so
    the same split falls out on any machine, in any Python build."""
    return hashlib.sha256(f"{seed}:{item_id}".encode()).hexdigest()


def make_split(
    dataset: DatasetAdapter,
    n_dev_items: int,
    seed: int = 0,
    note: str = "",
) -> Split:
    """Reserve ``n_dev_items`` whole items for development; the rest is held out."""
    items = list(dataset.items())
    info = dataset.info()
    if n_dev_items >= len(items):
        raise ValueError(
            f"{n_dev_items} development items requested but the dataset has {len(items)} — "
            "a split that keeps nothing back is not a split"
        )
    ordered = sorted(items, key=lambda item: _rank(item.item_id, seed))
    dev = ordered[:n_dev_items]
    heldout = ordered[n_dev_items:]
    return Split(
        dataset_id=info.dataset_id,
        revision_id=info.revision_id,
        content_sha256=info.content_sha256,
        dev_items=tuple(sorted(item.item_id for item in dev)),
        heldout_items=tuple(sorted(item.item_id for item in heldout)),
        seed=seed,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        category_balance={
            "dev": category_balance(dev),
            "heldout": category_balance(heldout),
        },
        note=note,
    )


def category_balance(items: Iterable[EvalItem]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        for query in item.queries:
            label = query.type_label or "unlabelled"
            counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items()))


class SplitView:
    """A ``DatasetAdapter`` restricted to one side of a split.

    ``info().subset`` records which side, so the manifest — and therefore the
    score-matrix row — always says whether a number came from the development
    set or the held-out set. That disclosure is the point of the split.
    """

    def __init__(self, dataset: DatasetAdapter, split: Split, side: str = "heldout") -> None:
        if side not in ("dev", "heldout"):
            raise ValueError("side must be 'dev' or 'heldout'")
        info = dataset.info()
        split.check(info)
        self._dataset = dataset
        self._split = split
        self.side = side
        self._ids = set(split.dev_items if side == "dev" else split.heldout_items)

    def info(self) -> DatasetInfo:
        base = self._dataset.info()
        kept = list(self._items())
        return DatasetInfo(
            dataset_id=base.dataset_id,
            revision_id=base.revision_id,
            licence=base.licence,
            source_path=base.source_path,
            content_sha256=base.content_sha256,
            n_items=len(kept),
            n_queries=sum(len(item.queries) for item in kept),
            subset=f"{base.subset}+split:{self.side}",
            notes=(base.notes + f" | split seed {self._split.seed}").strip(" |"),
        )

    def _items(self) -> Sequence[EvalItem]:
        return [item for item in self._dataset.items() if item.item_id in self._ids]

    def items(self) -> Iterator[EvalItem]:
        yield from self._items()
