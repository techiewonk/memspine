"""ConvoMem adapter (Salesforce, arXiv 2511.10523; HF Salesforce/ConvoMem @ e3e9b39).

**Licence: CC BY-NC 4.0 (data).** Run for research only; never redistribute samples.

ConvoMem asks at what history size retrieval beats full context. Files are laid out as
``core_benchmark/evidence_questions/<evidence_type>/<k>_evidence/<person>.json``. Each file holds
~100 evidence items: a question, its answer, the evidence messages, and the conversation(s) that
contain them. The haystack axis is built by adding filler conversations. Here that is
``filler`` conversations drawn deterministically (seeded) from other items in the same sample,
so a run can sweep ``filler`` in {0, 10, 30, 100, ...}.

Mapping:
- one ``EvalItem`` per evidence item; each message is a ``Turn`` (``turn_id`` =
  ``"<conv id>:<n>"``, ``session_id`` = the conversation id, ``timestamp`` = None: ConvoMem has
  no times);
- ``gold`` = the answer; ``gold_turn_ids`` = messages whose text equals an evidence message
  (the README says evidence appears exactly once);
- ``type_label`` = ``"<evidence_type>/<k>"``.

Scoring (official README, github SalesforceAIResearch/ConvoMem, Apache-2.0 code): *exact
match* for factual questions (user, assistant and changing facts) and *semantic match* for
preference and implicit-connection questions. Each query carries the declared metric in
``meta["official_metric"]``; abstention is not named in the README and is marked
``"unspecified"``. Gap: the judge implementation (Scala ``Evaluate*Evidence*``) was not
inspected, so the semantic-match judge model and prompt are not reproduced here.

Sampling is stratified: the first ``per_stratum`` items (by item order within files sorted by
name) of each (evidence type, k) stratum, so a run's question set is reproducible and its ids
can be published.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..contracts import DatasetInfo, EvalItem, Query, Turn

__all__ = ["CONVOMEM_OFFICIAL_METRIC", "ConvoMemDataset", "official_metric"]

#: Evidence-type directory -> the metric the official README declares for it.
CONVOMEM_OFFICIAL_METRIC = {
    "user_evidence": "exact_match",
    "assistant_facts_evidence": "exact_match",
    "changing_evidence": "exact_match",
    "preference_evidence": "semantic_match",
    "implicit_connection_evidence": "semantic_match",
}


def official_metric(evidence_type: str) -> str:
    """The README's metric for an evidence type; ``"unspecified"`` when it names none."""
    return CONVOMEM_OFFICIAL_METRIC.get(evidence_type, "unspecified")


class ConvoMemDataset:
    def __init__(
        self,
        root: str | Path,
        revision_id: str,
        per_stratum: int = 20,
        filler: int = 0,
        seed: int = 0,
        evidence_types: tuple[str, ...] | None = None,
        licence: str = "CC BY-NC 4.0 (data); research use only",
    ) -> None:
        self.root = Path(root) / "core_benchmark" / "evidence_questions"
        if not self.root.exists():
            raise FileNotFoundError(f"{self.root} not found; fetch ConvoMem files first")
        self.revision_id = revision_id
        self.per_stratum, self.filler, self.seed = per_stratum, filler, seed
        self.evidence_types = evidence_types
        self.licence = licence
        # R3-11: identify the exact files read (relative path + bytes, in read order).
        self._digest = hashlib.sha256()
        self._items = list(self._build())
        self._sha = self._digest.hexdigest()

    def info(self) -> DatasetInfo:
        return DatasetInfo(
            dataset_id="convomem",
            revision_id=self.revision_id,
            licence=self.licence,
            source_path=str(self.root),
            content_sha256=self._sha,
            n_items=len(self._items),
            n_queries=len(self._items),
            subset=f"per_stratum={self.per_stratum},filler={self.filler},seed={self.seed}",
        )

    def items(self) -> Iterator[EvalItem]:
        yield from self._items

    def _raw(self) -> list[tuple[str, str, dict[str, Any]]]:
        rows: list[tuple[str, str, dict[str, Any]]] = []
        for type_dir in sorted(p for p in self.root.iterdir() if p.is_dir()):
            if self.evidence_types and type_dir.name not in self.evidence_types:
                continue
            for k_dir in sorted(p for p in type_dir.iterdir() if p.is_dir()):
                taken = 0
                for f in sorted(k_dir.glob("*.json")):
                    raw = f.read_bytes()
                    self._digest.update(f.relative_to(self.root).as_posix().encode() + b"\0")
                    self._digest.update(raw)
                    data = json.loads(raw.decode("utf-8"))
                    for item in data.get("evidence_items") or []:
                        if taken >= self.per_stratum:
                            break
                        rows.append((type_dir.name, k_dir.name.split("_")[0], item))
                        taken += 1
                    if taken >= self.per_stratum:
                        break
        return rows

    def _build(self) -> Iterator[EvalItem]:
        rows = self._raw()
        pool = [c for _, _, item in rows for c in item.get("conversations") or []]
        rng = random.Random(self.seed)
        for index, (etype, k, item) in enumerate(rows):
            own = list(item.get("conversations") or [])
            own_ids = {c.get("id") for c in own}
            fillers = [c for c in pool if c.get("id") not in own_ids]
            chosen = rng.sample(fillers, min(self.filler, len(fillers))) if self.filler else []
            convs = chosen + own
            if chosen:
                rng.shuffle(convs)
            evidence = {str(e.get("text", "")).strip() for e in item.get("message_evidences") or []}
            history: list[Turn] = []
            gold_ids: list[str] = []
            for conv in convs:
                cid = str(conv.get("id"))
                for n, msg in enumerate(conv.get("messages") or []):
                    tid = f"{cid}:{n}"
                    text = str(msg.get("text", ""))
                    history.append(
                        Turn(
                            turn_id=tid,
                            session_id=cid,
                            speaker=str(msg.get("speaker", "")),
                            text=text,
                            timestamp=None,
                        )
                    )
                    if text.strip() in evidence and cid in {str(i) for i in own_ids}:
                        gold_ids.append(tid)
            qid = f"{etype}/{k}/{index}"
            yield EvalItem(
                item_id=qid,
                history=tuple(history),
                queries=(
                    Query(
                        query_id=qid,
                        text=str(item.get("question", "")),
                        gold=str(item.get("answer", "")),
                        gold_turn_ids=tuple(gold_ids),
                        type_label=f"{etype}/{k}",
                        meta={"benchmark": "convomem", "official_metric": official_metric(etype)},
                    ),
                ),
                meta={"filler": len(chosen), "person": item.get("personId")},
            )
