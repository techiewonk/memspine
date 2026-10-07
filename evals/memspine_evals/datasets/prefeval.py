"""PrefEval adapter: preference-turn R@k under filler (ICLR 2025, arXiv 2502.09597).

Sources, checked 2026-10-07:

- data: github ``amazon-science/PrefEval`` (``main``), ``benchmark_dataset/``; mirrored on
  HF ``siyanzhao/prefeval_{explicit,implicit_choice,implicit_persona}``.
- **Licence: CC BY-NC 4.0** (repo ``LICENSE``). Research use only.

Format (``README_github.md``), one JSON list per topic (20 topics):

- ``explicit_preference/<topic>.json``: ``{"preference", "question", "explanation"}``;
- ``implicit_preference/choice-based/<topic>.json``: the same plus ``implicit_query``,
  ``options``, ``aligned_op`` and ``conversation = {"query", "assistant_options",
  "user_selection", "assistant_acknowledgment"}``;
- ``implicit_preference/persona-driven/<topic>.json``: the same plus ``persona`` and
  ``conversation = {"0": {"user", "assistant"}, ...}`` (the preference surfaces in passing);
- ``filtered_inter_turns.json``: 24 LMSYS conversations used as inter-turn filler.

Mapping to the harness contract:

- one ``EvalItem`` per (form, topic, row): the preference turns, then ``filler`` filler
  conversations (taken deterministically, seeded), then the ``Query`` (``question``);
- explicit: one user turn holding the stated preference, which is the gold;
- choice-based: the four conversation messages; gold = the ``user_selection`` turn (the
  choice is what reveals the preference), the options turn id is kept in meta;
- persona-driven: every user / assistant message; gold = the user turn that states the
  preference: the one containing it verbatim (normalised), else the user turn with the
  highest content-word recall of the preference, kept only if that recall is at least
  ``min_overlap`` (``meta["gold_method"]`` = ``verbatim`` / ``overlap`` / ``unmapped``,
  ``meta["gold_overlap"]`` = the recall). Only 92 / 1,000 rows state it verbatim, so this is a
  heuristic and is reported per method;
- ``gold`` = the preference text (the generation judge checks adherence to it);
  ``type_label`` = ``"<form>/<topic>"``.

**Gaps:** the paper's filler is up to 300 LMSYS turns (~100K tokens); only the 24 conversations
in ``filtered_inter_turns.json`` are local, so ``filler`` caps at 24 conversations (~240 turns).
Generation and MCQ scores need a reader and judge; only preference-turn R@k is free.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from ..contracts import DatasetInfo, EvalItem, Query, Turn

__all__ = ["PREFEVAL_FORMS", "PrefEvalDataset", "preference_turn"]

PREFEVAL_FORMS = ("explicit", "choice", "persona")
_DIRS = {
    "explicit": "explicit_preference",
    "choice": "implicit_preference/choice-based",
    "persona": "implicit_preference/persona-driven",
}
_STOP_WORDS = (
    "i a an the and or to of in on for with my me is am are be it that this as at by from so do "
    "not no but you your we our they very really absolutely"
)
_STOP = frozenset(_STOP_WORDS.split())


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


def _content(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP and len(w) > 2}


def preference_turn(
    preference: str, user_turns: Mapping[str, str], min_overlap: float = 0.3
) -> tuple[str | None, str, float]:
    """``(turn id, method, recall)`` of the user turn that states ``preference``."""
    target = _norm(preference)
    for tid, text in user_turns.items():
        if target and target in _norm(text):
            return tid, "verbatim", 1.0
    words = _content(preference)
    best, score = None, 0.0
    for tid, text in user_turns.items():
        recall = len(words & _content(text)) / len(words) if words else 0.0
        if recall > score:
            best, score = tid, recall
    if best is None or score < min_overlap:
        return None, "unmapped", score
    return best, "overlap", score


class PrefEvalDataset:
    """PrefEval, read from a local ``benchmark_dataset/`` folder. Never downloads."""

    def __init__(
        self,
        root: str | Path,
        revision_id: str,
        forms: tuple[str, ...] = PREFEVAL_FORMS,
        topics: tuple[str, ...] | None = None,
        filler: int = 0,
        seed: int = 0,
        per_topic: int | None = None,
        min_overlap: float = 0.3,
        licence: str = "CC BY-NC 4.0 (amazon-science/PrefEval); research use only",
    ) -> None:
        root = Path(root)
        self.root = root / "benchmark_dataset" if (root / "benchmark_dataset").is_dir() else root
        if not (self.root / "explicit_preference").is_dir():
            raise FileNotFoundError(f"{self.root}: no explicit_preference/; fetch PrefEval")
        unknown = set(forms) - set(PREFEVAL_FORMS)
        if unknown:
            raise ValueError(f"unknown PrefEval forms {sorted(unknown)}")
        self.forms, self.topics, self.per_topic = forms, topics, per_topic
        self.filler, self.seed, self.min_overlap = filler, seed, min_overlap
        self.licence = licence
        self._digest = hashlib.sha256()
        self._items = list(self._build())
        self._sha = self._digest.hexdigest()
        self.revision_id = f"sha256:{self._sha[:16]}" if revision_id == "auto" else revision_id

    def info(self) -> DatasetInfo:
        return DatasetInfo(
            dataset_id="prefeval",
            revision_id=self.revision_id,
            licence=self.licence,
            source_path=str(self.root),
            content_sha256=self._sha,
            n_items=len(self._items),
            n_queries=len(self._items),
            subset=(
                f"forms={','.join(self.forms)},filler={self.filler},seed={self.seed}"
                + ("" if self.per_topic is None else f",per_topic={self.per_topic}")
                + ("" if self.topics is None else f",topics={','.join(self.topics)}")
            ),
            notes="free measure: preference-turn R@k; filler capped at the 24 local LMSYS convs",
        )

    def items(self) -> Iterator[EvalItem]:
        yield from self._items

    def _read(self, path: Path) -> Any:
        raw = path.read_bytes()
        self._digest.update(path.relative_to(self.root).as_posix().encode() + b"\0" + raw)
        return json.loads(raw.decode("utf-8"))

    def _filler_pool(self) -> list[dict[str, Any]]:
        path = self.root / "filtered_inter_turns.json"
        if not self.filler:
            return []
        if not path.exists():
            raise FileNotFoundError(f"{path} not found; filler needs filtered_inter_turns.json")
        return [c for c in self._read(path) if isinstance(c, dict)]

    def _build(self) -> Iterator[EvalItem]:
        pool = self._filler_pool()
        rng = random.Random(self.seed)
        for form in self.forms:
            for path in sorted((self.root / _DIRS[form]).glob("*.json")):
                topic = path.stem
                if self.topics is not None and topic not in self.topics:
                    continue
                rows = self._read(path)
                for n, row in enumerate(rows[: self.per_topic]):
                    chosen = rng.sample(pool, min(self.filler, len(pool))) if pool else []
                    yield self._item(form, topic, n, row, chosen)

    def _item(
        self, form: str, topic: str, n: int, row: Mapping[str, Any], filler: list[dict[str, Any]]
    ) -> EvalItem:
        qid = f"{form}/{topic}/{n}"
        pref_session = f"{qid}:pref"
        turns: list[Turn] = []
        meta: dict[str, Any] = {"benchmark": "prefeval", "form": form, "topic": topic}

        def add(tag: str, speaker: str, text: str, session: str = pref_session) -> str:
            tid = f"{qid}:{tag}"
            turns.append(Turn(tid, session, speaker, str(text)))
            return tid

        gold: list[str] = []
        if form == "explicit":
            gold.append(add("p0", "user", row.get("preference", "")))
            meta["gold_method"] = "explicit"
        elif form == "choice":
            conv = row.get("conversation") or {}
            add("c0", "user", conv.get("query", ""))
            meta["options_turn_id"] = add("c1", "assistant", conv.get("assistant_options", ""))
            gold.append(add("c2", "user", conv.get("user_selection", "")))
            add("c3", "assistant", conv.get("assistant_acknowledgment", ""))
            meta["gold_method"] = "user_selection"
        else:
            users: dict[str, str] = {}
            convo = row.get("conversation") or {}
            for key in sorted(convo, key=lambda k: int(k) if str(k).isdigit() else 0):
                msg = convo[key] or {}
                users[add(f"t{key}u", "user", msg.get("user", ""))] = str(msg.get("user", ""))
                add(f"t{key}a", "assistant", msg.get("assistant", ""))
            tid, method, overlap = preference_turn(
                str(row.get("preference", "")), users, self.min_overlap
            )
            if tid is not None:
                gold.append(tid)
            meta.update(gold_method=method, gold_overlap=round(overlap, 4))
            meta["persona"] = row.get("persona")
        for conv in filler:
            cid = str(conv.get("conversation_id", "filler"))
            for m, msg in enumerate(conv.get("conversation") or []):
                role = str(msg.get("role", ""))
                add(f"f:{cid}:{m}", role, msg.get("content", ""), f"filler:{cid}")
        return EvalItem(
            item_id=qid,
            history=tuple(turns),
            queries=(
                Query(
                    query_id=qid,
                    text=str(row.get("question", "")),
                    gold=str(row.get("preference", "")),
                    gold_turn_ids=tuple(gold),
                    type_label=f"{form}/{topic}",
                    meta=meta,
                ),
            ),
            meta={"filler": len(filler), "explanation": row.get("explanation")},
        )
