"""MemoryCD adapter, retrieval-only rating proxy (Zhang et al. 2026; HF ``WZDavid/MemoryCD``
@ 14b934c, code github ``AgentMemoryWorld/MemoryCD`` @ 4cee5eb, MIT code).

**Licence:** the data derives from Amazon Reviews 2023 and carries no card licence;
research use only. Read from ``evals/data/memorycd/hf/users/cross_domain_users_sampled
.jsonl.gz``: one JSON object per user, ``{user_id, interactions: {<domain>: [{asin,
timestamp (ms), rating, title, text, ...}]}}`` (323 users x 4 domains released).

Mapping (a free proxy of the paper's rating-prediction task, **not** its protocol):

- one ``EvalItem`` per (user, target domain); interactions sorted by timestamp; the last
  ``n_test`` interactions of the target domain are held out as queries, everything earlier
  is history. ``setting="cross"`` keeps every domain's earlier interactions in history
  (memory from other domains); ``"single"`` keeps the target domain only. Only
  interactions strictly older than the first held-out one enter history (no future leak);
  ``history_cap`` keeps the most recent N so a run stays bounded;
- each history interaction is a ``Turn`` (``turn_id`` = ``"<user>:<domain>:<asin>:<ts>"``,
  ``session_id`` = the domain, ``text`` = ``"<title>. <review text>"`` truncated to
  ``max_chars``, ``Turn.meta`` = rating + domain);
- each held-out interaction is a ``Query``: ``text`` = its review **title** (the item
  metadata, 283 MB, was not fetched, so the title is the item surrogate; titles carry
  sentiment, which this proxy does not hide), ``gold`` = the rating as a string;
- ``Query.meta["ratings"]`` maps history ``turn_id`` -> rating for :func:`knn.knn_mean`
  (free kNN rating MAE), ``meta["user_mean"]`` is the history mean (the fallback baseline).

**Gold evidence is a proxy:** ``gold_turn_ids`` = same-domain history items with the same
rating as the held-out one.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from pathlib import Path
from statistics import fmean
from typing import Any

from ..contracts import DatasetInfo, EvalItem, Query, Turn
from .locomo import file_sha256

__all__ = ["MEMORYCD_FILE", "MemoryCDDataset"]

MEMORYCD_FILE = "cross_domain_users_sampled.jsonl.gz"


class MemoryCDDataset:
    """MemoryCD users file (``.jsonl`` or ``.jsonl.gz``). Never downloads."""

    def __init__(
        self,
        path: str | Path,
        revision_id: str,
        setting: str = "cross",
        n_test: int = 1,
        max_users: int | None = None,
        domains: tuple[str, ...] | None = None,
        history_cap: int = 200,
        max_chars: int = 1000,
        licence: str = "Amazon Reviews 2023 derived, no card licence; research use only",
    ) -> None:
        if setting not in {"cross", "single"}:
            raise ValueError("setting must be 'cross' or 'single'")
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"{self.path} not found; fetch MemoryCD users first")
        self._sha = file_sha256(self.path)
        self.revision_id = f"sha256:{self._sha[:16]}" if revision_id == "auto" else revision_id
        self.setting, self.n_test, self.max_users = setting, n_test, max_users
        self.domains, self.history_cap, self.max_chars = domains, history_cap, max_chars
        self.licence = licence
        self._items = list(self._build())

    def info(self) -> DatasetInfo:
        return DatasetInfo(
            dataset_id="memorycd",
            revision_id=self.revision_id,
            licence=self.licence,
            source_path=str(self.path),
            content_sha256=self._sha,
            n_items=len(self._items),
            n_queries=sum(len(i.queries) for i in self._items),
            subset=(
                f"{self.setting},n_test={self.n_test},cap={self.history_cap}"
                + ("" if self.max_users is None else f",max_users={self.max_users}")
            ),
            notes=(
                "retrieval-only rating proxy: kNN mean of retrieved history ratings "
                "(knn.knn_mean) -> MAE; query = review title (item metadata not fetched)"
            ),
        )

    def items(self) -> Iterator[EvalItem]:
        yield from self._items

    def _users(self) -> Iterator[dict[str, Any]]:
        opener = gzip.open if self.path.suffix == ".gz" else open
        with opener(self.path, "rt", encoding="utf-8") as handle:  # type: ignore[operator]
            for line in handle:
                if line.strip():
                    yield json.loads(line)

    def _turn(self, uid: str, domain: str, row: dict[str, Any]) -> Turn:
        text = f"{row.get('title', '')}. {row.get('text', '')}".strip()
        return Turn(
            turn_id=f"{uid}:{domain}:{row.get('asin', '')}:{row.get('timestamp', '')}",
            session_id=domain,
            speaker="user",
            text=text[: self.max_chars],
            timestamp=str(row.get("timestamp")) if row.get("timestamp") is not None else None,
            meta={"rating": float(row.get("rating", 0.0)), "domain": domain},
        )

    def _build(self) -> Iterator[EvalItem]:
        for n_user, user in enumerate(self._users()):
            if self.max_users is not None and n_user >= self.max_users:
                return
            uid = str(user.get("user_id", f"user{n_user}"))
            by_domain = {
                d: sorted(rows or [], key=lambda r: r.get("timestamp", 0))
                for d, rows in (user.get("interactions") or {}).items()
            }
            for domain in sorted(by_domain):
                if self.domains and domain not in self.domains:
                    continue
                rows = by_domain[domain]
                if len(rows) <= self.n_test:
                    continue
                held = rows[-self.n_test :]
                cutoff = held[0].get("timestamp", 0)
                pool = [
                    (d, r)
                    for d, rs in by_domain.items()
                    if self.setting == "cross" or d == domain
                    for r in rs
                    if r.get("timestamp", 0) < cutoff
                ]
                pool.sort(key=lambda dr: (dr[1].get("timestamp", 0), dr[0]))
                pool = pool[-self.history_cap :] if self.history_cap else pool
                history = tuple(self._turn(uid, d, r) for d, r in pool)
                if not history:
                    continue
                ratings = {t.turn_id: float(t.meta["rating"]) for t in history}
                user_mean = fmean(ratings.values())
                queries = []
                for q_index, row in enumerate(held):
                    rating = float(row.get("rating", 0.0))
                    gold_ids = tuple(
                        t.turn_id
                        for t in history
                        if t.session_id == domain and t.meta["rating"] == rating
                    )
                    queries.append(
                        Query(
                            query_id=f"{uid}:{domain}:q{q_index}",
                            text=str(row.get("title", "")),
                            gold=f"{rating:g}",
                            gold_turn_ids=gold_ids,
                            type_label=f"{domain}/{self.setting}",
                            meta={
                                "benchmark": "memorycd",
                                "ratings": ratings,
                                "user_mean": round(user_mean, 4),
                                "gold_is_proxy": True,
                            },
                        )
                    )
                yield EvalItem(
                    item_id=f"{uid}:{domain}",
                    history=history,
                    queries=tuple(queries),
                    meta={"user_id": uid, "target_domain": domain, "setting": self.setting},
                )
