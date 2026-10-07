"""PersonaBench adapter, retrieval-only (Salesforce, ACL 2025 Findings).

Sources, checked 2026-10-07:

- data + code: github ``SalesforceAIResearch/personabench`` @ ``151e8c9``, directory
  ``eval_data/eval_data_v1/synthetic_data/community_<n>/``; scoring in
  ``scripts/evaluation/retrieval_and_generation.py`` and ``scripts/evaluation/eval.py``.
- **Licence: CC BY-NC-SA 4.0** (GPT-4o-generated; the README says not for training
  competing models). Research use; derived copies carry the same licence.

Format, per community and noise level ``n`` in {0.0, 0.3, 0.5, 0.7}:

- ``private_data/noise_<n>/conversation_data_all.json``: ``[{"Name", "Data": [{"Target_name",
  "Conversations": [{"session", "time", "target_name", "conversation": [{"role",
  "content"}], "segment_id"}]}]}]``;
- ``.../user_ai_interaction_data_all.json`` and ``.../purchase_history_data_all.json``:
  ``[{"Name", "Data": [{"session", "time", "user_ai_interaction" | "purchase_history",
  "segment_id"}]}]``;
- ``eval_info/eval_info_all.json``: per person ``{"Name", "Eval_Info": {"qa": [{"q_id",
  "question", "answer", "type", "difficulty", "outdated_value"}]}}``;
- ``eval_info/qa_gt_context_all_noise_<n>.json``: ``[{"q_id", "question", "answer",
  "segment_id": {answer_part: [alternative segment ids]}}]``.

Higher noise adds distractor sessions; the gold ids change with the noise level.

Mapping to the harness contract:

- one ``EvalItem`` per (community, person, noise) (``item_id`` =
  ``"<community>/<person>/noise_<n>"``); every segment (one session of any of the three
  document types, as in the official ``create_vector_base``) is one ``Turn`` with
  ``turn_id`` = the ``segment_id``, ``session_id`` = the session name, timestamp = ``time``;
  the text is the session rendered as plain lines (``segment_id`` and ``session`` excluded,
  as the official chunker does);
- one ``Query`` per QA with a gold entry (``query_id`` = ``"<community>/<n>/<q_id>"``);
  ``gold_turn_ids`` = every candidate segment id (any hit counts for ``R@k``);
  ``meta["gold_groups"]`` keeps the per-answer-part alternatives, and
  :func:`official_recall` reproduces the benchmark's recall (one id per part, best
  combination); ``type_label`` = ``type`` (``Preference`` also carries the difficulty, as the
  official breakdown does); ``meta["outdated_value"]`` is kept (107 QA in the release).

Only the people with private data are in the local release (3 per community), so 263 QA
per noise level are measurable, not all ``eval_info_all`` entries.

**Gaps:** the official QA score uses an LLM reader; ``Subjective`` questions are skipped by
the official retrieval score (``meta["official_excluded"]`` is true for them).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from itertools import product
from pathlib import Path
from typing import Any

from ..contracts import DatasetInfo, EvalItem, Query, Turn

__all__ = ["NOISE_LEVELS", "PERSONABENCH_SOURCE", "PersonaBenchDataset", "official_recall"]

PERSONABENCH_SOURCE = {
    "data": "https://github.com/SalesforceAIResearch/personabench",
    "data_revision": "151e8c942b73dec072b633abbb3356185b59279c",
    "paper": "PersonaBench (ACL 2025 Findings)",
    "licence": "CC BY-NC-SA 4.0",
}

NOISE_LEVELS = ("0.0", "0.3", "0.5", "0.7")
_DOCS = ("conversation_data", "user_ai_interaction_data", "purchase_history_data")


def _render(value: Any, indent: str = "") -> list[str]:
    if isinstance(value, dict):
        if set(value) >= {"role", "content"}:
            return [f"{indent}{value['role']}: {value['content']}"]
        lines: list[str] = []
        for key, inner in value.items():
            if key in ("segment_id", "session"):
                continue
            if isinstance(inner, dict | list):
                lines.append(f"{indent}{key}:")
                lines.extend(_render(inner, indent + "  "))
            else:
                lines.append(f"{indent}{key}: {inner}")
        return lines
    if isinstance(value, list):
        if all(not isinstance(v, dict | list) for v in value):
            return [f"{indent}{', '.join(map(str, value))}"]
        return [line for v in value for line in _render(v, indent)]
    return [f"{indent}{value}"]


def official_recall(retrieved_ids: Sequence[str], groups: Mapping[str, Sequence[str]]) -> float:
    """``eval.py``'s recall: every answer part needs one of its alternative segments; the
    best combination over the alternatives counts. ``retrieved_ids`` is the top-k list."""
    if not groups:
        return 0.0
    got = set(retrieved_ids)
    best = 0.0
    for combo in product(*groups.values()):
        wanted = set(combo)
        best = max(best, len(got & wanted) / len(wanted))
    return best


class PersonaBenchDataset:
    """PersonaBench v1 from a local checkout of the repo. Never downloads."""

    def __init__(
        self,
        root: str | Path,
        revision_id: str,
        noises: tuple[str, ...] = NOISE_LEVELS,
        communities: tuple[str, ...] | None = None,
        licence: str = "CC BY-NC-SA 4.0 (SalesforceAIResearch/personabench); research only",
    ) -> None:
        root = Path(root)
        candidates = (root, root / "synthetic_data", root / "eval_data" / "eval_data_v1")
        base = next((c for c in candidates if (c / "synthetic_data").is_dir()), None)
        if base is None:
            raise FileNotFoundError(f"{root}: no synthetic_data/; fetch PersonaBench first")
        self.root = base / "synthetic_data"
        bad = [n for n in noises if n not in NOISE_LEVELS]
        if bad:
            raise ValueError(f"unknown noise levels {bad}; expected {NOISE_LEVELS}")
        self.noises, self.licence = noises, licence
        self.communities = communities or tuple(
            sorted(p.name for p in self.root.iterdir() if p.is_dir())
        )
        self._digest = hashlib.sha256()
        self._items = list(self._build())
        self._sha = self._digest.hexdigest()
        self.revision_id = f"sha256:{self._sha[:16]}" if revision_id == "auto" else revision_id

    def info(self) -> DatasetInfo:
        return DatasetInfo(
            dataset_id="personabench",
            revision_id=self.revision_id,
            licence=self.licence,
            source_path=str(self.root),
            content_sha256=self._sha,
            n_items=len(self._items),
            n_queries=sum(len(i.queries) for i in self._items),
            subset=f"noise={'/'.join(self.noises)},communities={'/'.join(self.communities)}",
            notes="retrieval-only: segment_id R@k per noise level; official recall in meta",
        )

    def items(self) -> Iterator[EvalItem]:
        yield from self._items

    def _load(self, path: Path) -> Any:
        raw = path.read_bytes()
        self._digest.update(path.relative_to(self.root).as_posix().encode() + b"\0" + raw)
        return json.loads(raw.decode("utf-8"))

    def _segments(self, document: str, data: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
        if document == "conversation_data":
            for partner in data:
                yield from partner.get("Conversations") or []
        else:
            yield from data

    def _build(self) -> Iterator[EvalItem]:
        for community in self.communities:
            cdir = self.root / community
            eval_info = self._load(cdir / "eval_info" / "eval_info_all.json")
            qa_meta = {q["q_id"]: q for p in eval_info for q in p["Eval_Info"]["qa"]}
            owner = {q["q_id"]: p["Name"] for p in eval_info for q in p["Eval_Info"]["qa"]}
            for noise in self.noises:
                gt = self._load(cdir / "eval_info" / f"qa_gt_context_all_noise_{noise}.json")
                docs = {
                    d: {
                        e["Name"]: e["Data"]
                        for e in self._load(
                            cdir / "private_data" / f"noise_{noise}" / f"{d}_all.json"
                        )
                    }
                    for d in _DOCS
                }
                people = list(docs["conversation_data"])
                for person in people:
                    history: list[Turn] = []
                    for document in _DOCS:
                        for seg in self._segments(document, docs[document].get(person) or []):
                            history.append(
                                Turn(
                                    turn_id=str(seg["segment_id"]),
                                    session_id=str(seg.get("session") or document),
                                    speaker=document,
                                    text="\n".join(_render(seg)),
                                    timestamp=seg.get("time"),
                                    meta={"document": document},
                                )
                            )
                    known = {t.turn_id for t in history}
                    queries: list[Query] = []
                    for entry in gt:
                        if owner.get(entry["q_id"]) != person:
                            continue
                        meta_qa = qa_meta.get(entry["q_id"], {})
                        groups = {
                            str(k): [str(i) for i in v] for k, v in entry["segment_id"].items()
                        }
                        gold_ids = tuple(
                            dict.fromkeys(i for v in groups.values() for i in v if i in known)
                        )
                        qtype = str(meta_qa.get("type") or "")
                        difficulty = meta_qa.get("difficulty")
                        label = (
                            f"{qtype}/{difficulty}"
                            if qtype == "Preference" and difficulty
                            else qtype
                        )
                        answer = entry.get("answer")
                        queries.append(
                            Query(
                                query_id=f"{community}/{noise}/{entry['q_id']}",
                                text=str(entry.get("question", "")),
                                gold=answer if isinstance(answer, str) else json.dumps(answer),
                                gold_turn_ids=gold_ids,
                                type_label=label or None,
                                meta={
                                    "benchmark": "personabench",
                                    "noise": noise,
                                    "gold_groups": groups,
                                    "outdated_value": meta_qa.get("outdated_value"),
                                    "official_excluded": qtype == "Subjective",
                                },
                            )
                        )
                    if history and queries:
                        yield EvalItem(
                            item_id=f"{community}/{person}/noise_{noise}",
                            history=tuple(history),
                            queries=tuple(queries),
                            meta={"community": community, "person": person, "noise": noise},
                        )
