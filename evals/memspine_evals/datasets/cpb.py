"""CPB Live replay, retrieval-only (Correlated Promotion Benchmark, arXiv 2609.30813;
github ``lxy1134/iclr_2027``, MIT).

Read from ``evals/data/cpb/data/cpb_live/``: ``scenarios/stage<N>/<id>.json`` (feeds with
``source_id``, ``text``, ``source_type`` (``web_text`` / ``register_document``),
``round``, ``agent``; ``task_queries`` per consumer agent), ``gold/stage<N>/<id>.gold.json``
(``true_texts``, ``false_texts``, family-specific flags) and ``lineage/stage<N>.json``
(``roots``: source id -> lineage root; stages 2, 4, 5, 6).

Deterministic replay, mapped to the harness contract:

- one ``EvalItem`` per scenario; every feed is a ``Turn`` in round order (``turn_id`` =
  the feed's ``source_id``, ``session_id`` = ``"round<r>"``, ``speaker`` = the agent,
  ``timestamp`` = a synthetic time increasing with the round). ``Turn.meta`` carries
  ``source_type`` and the lineage ``root`` (the source id itself when no lineage file);
- one ``Query`` per consumer in ``task_queries`` (the consumer's prompt), after the last
  feed; ``gold_turn_ids`` = feeds whose text is in ``true_texts``;
  ``meta["false_turn_ids"]`` = feeds whose text is in ``false_texts``.

**Free measure** (:func:`cpb_retrieval_rates`): over the top-k retrieved ids, true-evidence
R@k (the harness's own R@k on ``gold_turn_ids``), **false-retrieval rate** (share of
queries with any false feed in the top k: retrieval-level false adoption), and
**true-above-false order** (on queries with both kinds retrieved, share where the best true
feed outranks the best false one). Lineage roots let a run count independent sources.

**Gaps:** stage-1 ``script`` entries (agent echoes of earlier text, generated at run time)
are not replayed; only ``feeds`` are. Consumer adoption (the paper's metric) needs an LLM
consumer and is not computed. The Static split needs hydration from external corpora and is
not adapted.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..contracts import DatasetInfo, EvalItem, Query, Turn

__all__ = ["CPBLiveDataset", "cpb_retrieval_rates"]

_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def cpb_retrieval_rates(
    rows: Sequence[tuple[Sequence[str], Sequence[str], Sequence[str]]], k: int
) -> dict[str, float | int | None]:
    """Score ``(retrieved_ids, gold_true_ids, false_ids)`` triples at top-``k``.

    Returns ``true_recall`` (any true feed in top k, over queries with true gold),
    ``false_retrieval_rate`` (any false feed in top k, over queries that have a false
    feed), ``true_above_false`` (over queries where both appear in top k) and the three
    denominators."""
    n_true = n_false = n_both = hit_true = hit_false = ordered = 0
    for retrieved, true_ids, false_ids in rows:
        top = list(dict.fromkeys(retrieved))[:k]
        t_ranks = [top.index(t) for t in true_ids if t in top]
        f_ranks = [top.index(f) for f in false_ids if f in top]
        if true_ids:
            n_true += 1
            hit_true += bool(t_ranks)
        if false_ids:
            n_false += 1
            hit_false += bool(f_ranks)
        if t_ranks and f_ranks:
            n_both += 1
            ordered += min(t_ranks) < min(f_ranks)
    return {
        "true_recall": hit_true / n_true if n_true else None,
        "false_retrieval_rate": hit_false / n_false if n_false else None,
        "true_above_false": ordered / n_both if n_both else None,
        "n_true": n_true,
        "n_false": n_false,
        "n_both": n_both,
    }


class CPBLiveDataset:
    """CPB Live scenarios from a local ``cpb_live`` dir (160 local files; card says 180)."""

    def __init__(
        self,
        root: str | Path,
        revision_id: str,
        stages: tuple[int, ...] | None = None,
        licence: str = "MIT (lxy1134/iclr_2027)",
    ) -> None:
        self.root = Path(root)
        if (self.root / "data" / "cpb_live").exists():
            self.root = self.root / "data" / "cpb_live"
        if not (self.root / "scenarios").exists():
            raise FileNotFoundError(f"{self.root}/scenarios not found; fetch CPB first")
        self.revision_id = revision_id
        self.stages, self.licence = stages, licence
        self._digest = hashlib.sha256()
        self._items = list(self._build())
        self._sha = self._digest.hexdigest()

    def info(self) -> DatasetInfo:
        return DatasetInfo(
            dataset_id="cpb_live",
            revision_id=self.revision_id,
            licence=self.licence,
            source_path=str(self.root),
            content_sha256=self._sha,
            n_items=len(self._items),
            n_queries=sum(len(i.queries) for i in self._items),
            subset="all" if self.stages is None else "stages=" + "/".join(map(str, self.stages)),
            notes=(
                "deterministic feed replay; free measure = true R@k, false-retrieval rate, "
                "true-above-false order (cpb_retrieval_rates); stage-1 echo scripts not replayed"
            ),
        )

    def items(self) -> Iterator[EvalItem]:
        yield from self._items

    def _read(self, path: Path) -> Any:
        raw = path.read_bytes()
        self._digest.update(path.relative_to(self.root).as_posix().encode() + b"\0" + raw)
        return json.loads(raw.decode("utf-8"))

    def _build(self) -> Iterator[EvalItem]:
        stage_dirs = sorted(
            (int(p.name.removeprefix("stage")), p)
            for p in (self.root / "scenarios").iterdir()
            if p.is_dir() and p.name.startswith("stage")
        )
        for stage, sdir in stage_dirs:
            if self.stages and stage not in self.stages:
                continue
            lineage_path = self.root / "lineage" / f"stage{stage}.json"
            roots: Mapping[str, str] = (
                self._read(lineage_path).get("roots", {}) if lineage_path.exists() else {}
            )
            for spath in sorted(sdir.glob("*.json")):
                scenario = self._read(spath)
                sid = str(scenario.get("scenario_id", spath.stem))
                gpath = self.root / "gold" / f"stage{stage}" / f"{sid}.gold.json"
                gold = self._read(gpath) if gpath.exists() else {}
                yield from self._item(stage, scenario, gold, roots)

    def _item(
        self,
        stage: int,
        scenario: dict[str, Any],
        gold: dict[str, Any],
        roots: Mapping[str, str],
    ) -> Iterator[EvalItem]:
        sid = str(scenario.get("scenario_id"))
        feeds = sorted(
            enumerate(scenario.get("feeds") or []),
            key=lambda pair: (int(pair[1].get("round", 0)), pair[0]),
        )
        true_texts = {str(t).strip() for t in gold.get("true_texts") or []}
        false_texts = {str(t).strip() for t in gold.get("false_texts") or []}
        history: list[Turn] = []
        true_ids: list[str] = []
        false_ids: list[str] = []
        for order, (_, feed) in enumerate(feeds):
            fid = str(feed.get("source_id", f"{sid}-f{order}"))
            rnd = int(feed.get("round", 0))
            text = str(feed.get("text", ""))
            history.append(
                Turn(
                    turn_id=fid,
                    session_id=f"round{rnd}",
                    speaker=str(feed.get("agent", "")),
                    text=text,
                    timestamp=(_BASE + timedelta(hours=rnd, seconds=order)).isoformat(),
                    meta={
                        "source_type": feed.get("source_type"),
                        "round": rnd,
                        "root": roots.get(fid, fid),
                    },
                )
            )
            if text.strip() in true_texts:
                true_ids.append(fid)
            if text.strip() in false_texts:
                false_ids.append(fid)
        if not history:
            return
        family = str(scenario.get("family", ""))
        queries = tuple(
            Query(
                query_id=f"{sid}:{agent}",
                text=str(prompt),
                gold=" || ".join(sorted(true_texts)) or None,
                gold_turn_ids=tuple(true_ids),
                type_label=f"stage{stage}/{family}",
                after_turn=history[-1].turn_id,
                meta={
                    "benchmark": "cpb_live",
                    "false_turn_ids": list(false_ids),
                    "dv_variant": gold.get("dv_variant"),
                    "genuine_corroboration": gold.get("genuine_corroboration"),
                    "n_lineage_roots": len({t.meta["root"] for t in history}),
                },
            )
            for agent, prompt in sorted((scenario.get("task_queries") or {}).items())
        )
        if queries:
            yield EvalItem(
                item_id=sid,
                history=tuple(history),
                queries=queries,
                meta={"stage": stage, "family": family, "domain": scenario.get("domain")},
            )
