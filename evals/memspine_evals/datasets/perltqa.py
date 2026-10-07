"""PerLTQA adapter: Reference Memory R@k per memory type (SIGHAN-10 @ ACL 2024).

Sources, checked 2026-10-07:

- data + code: github ``Elvin-Yiming-Du/PerLTQA`` @ ``8d9e198``,
  ``Dataset/en_v2/{perltmem_en_v2.json, perltqa_en_v2.json}`` (also ``en`` and ``zh``).
- **Licence: CC BY-NC 4.0** (``LICENSE.txt``). Research use only.

Format (``Dataset/README.md`` plus the files themselves):

- ``perltmem``: ``{character: {"profile": {field: value}, "profile_description": str,
  "social_relationship": {rid: {"Supporting Characters", "Description", "Relationship"}},
  "events": {eid: {"content", "summary", "Characters", "Creation Time", ...}},
  "dialogues": {did: {"events": eid, "contents": {timestamp: [line, ...]}}}}}``;
- ``perltqa``: a list of ``{character: {"profile": [qa], "social_relationship": [{rid: [qa]}],
  "events": [{eid: [qa]}], "dialogues": [{did: [qa]}]}}`` with
  ``qa = {"Question", "Answer", "Reference Memory", "Memory Anchors"}``; ``Reference Memory``
  is a profile field name, or the repr of a list of memory ids (``"['4_0_0']"``). In en_v2, 25
  ``social_relationship`` sections are stored as a Python dict's repr; they are parsed.

Mapping to the harness contract (the memory is a typed record store, not a chat log, so each
record becomes one ``Turn``; ``speaker`` = the memory type):

- one ``EvalItem`` per QA character; its history is that character's memory only:
  profile fields (``"<char>:profile:<field>"``), relationships (``"<char>:social:<rid>"``),
  events (``"<char>:event:<eid>"``, timestamp = ``Creation Time`` verbatim) and dialogues
  (``"<char>:dialogue:<did>:<n>"``, one turn per timestamp block, lines joined);
- one ``Query`` per QA; ``gold`` = ``Answer``; ``gold_turn_ids`` = every turn of the referenced
  memory records, so turn R@k is the paper's memory-retrieval recall;
- ``type_label`` = the memory type (``profile``, ``social_relationship``, ``events``,
  ``dialogues``).

**Gaps:** 11 en_v2 questions reference ids absent from the memory file; they keep empty gold
and ``meta["gold_unmapped"]``. One QA character (``"dragon beautiful"`` in en_v2) has no memory
entry and is skipped, counted in ``info().notes``. ``Memory Anchors`` span offsets are kept in
meta but not used. Answers are long paraphrases: QA needs a judge; only R@k is free.
"""

from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from ..contracts import DatasetInfo, EvalItem, Query, Turn

__all__ = ["PERLTQA_TYPES", "PerLTQADataset"]

PERLTQA_TYPES = ("profile", "social_relationship", "events", "dialogues")
_FILES = {
    "en_v2": ("perltmem_en_v2.json", "perltqa_en_v2.json"),
    "en": ("perltmem_en.json", "perltqa_en.json"),
    "zh": ("perltmem.json", "perltqa.json"),
}
_PREFIX = {"social_relationship": "social", "events": "event", "dialogues": "dialogue"}


def _refs(raw: Any) -> list[str]:
    text = str(raw).strip()
    if text.startswith("["):
        try:
            return [str(x) for x in ast.literal_eval(text)]
        except (ValueError, SyntaxError):
            return []
    return [text] if text else []


def _section(raw: Any) -> dict[str, Any]:
    """A memory section; 25 en_v2 ``social_relationship`` sections are a dict's repr."""
    if isinstance(raw, str):
        try:
            raw = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            return {}
    return raw if isinstance(raw, dict) else {}


class PerLTQADataset:
    """PerLTQA, read from a local ``Dataset/<version>/`` folder. Never downloads."""

    def __init__(
        self,
        root: str | Path,
        revision_id: str,
        version: str = "en_v2",
        memory_types: tuple[str, ...] = PERLTQA_TYPES,
        max_characters: int | None = None,
        licence: str = "CC BY-NC 4.0 (Elvin-Yiming-Du/PerLTQA); research use only",
    ) -> None:
        if version not in _FILES:
            raise ValueError(f"unknown PerLTQA version {version!r}; one of {sorted(_FILES)}")
        root = Path(root)
        folder = root / "Dataset" / version if (root / "Dataset").is_dir() else root
        self.mem_path, self.qa_path = (folder / name for name in _FILES[version])
        for path in (self.mem_path, self.qa_path):
            if not path.exists():
                raise FileNotFoundError(f"{path} not found; fetch PerLTQA first (evals/README.md)")
        self.version, self.memory_types, self.max_characters = version, memory_types, max_characters
        self.licence = licence
        digest = hashlib.sha256()
        for path in (self.mem_path, self.qa_path):
            digest.update(path.name.encode() + b"\0" + path.read_bytes())
        self._sha = digest.hexdigest()
        self.revision_id = f"sha256:{self._sha[:16]}" if revision_id == "auto" else revision_id
        self.skipped: list[str] = []
        self._items = list(self._build())

    def info(self) -> DatasetInfo:
        return DatasetInfo(
            dataset_id=f"perltqa_{self.version}",
            revision_id=self.revision_id,
            licence=self.licence,
            source_path=str(self.qa_path.parent),
            content_sha256=self._sha,
            n_items=len(self._items),
            n_queries=sum(len(i.queries) for i in self._items),
            subset=f"types={','.join(self.memory_types)}"
            + ("" if self.max_characters is None else f",max_characters={self.max_characters}"),
            notes="free measure: Reference Memory R@k per memory type"
            + (f"; skipped characters without memory: {self.skipped}" if self.skipped else ""),
        )

    def items(self) -> Iterator[EvalItem]:
        yield from self._items

    @staticmethod
    def _history(name: str, mem: Mapping[str, Any]) -> tuple[list[Turn], dict[str, list[str]]]:
        turns: list[Turn] = []
        ids: dict[str, list[str]] = {}  # "<type>:<ref>" -> turn ids

        def add(kind: str, ref: str, text: str, stamp: str | None, suffix: str = "") -> None:
            tid = f"{name}:{_PREFIX.get(kind, kind)}:{ref}{suffix}"
            turns.append(Turn(tid, kind, kind, text, stamp or None, {"memory_id": ref}))
            ids.setdefault(f"{kind}:{ref}", []).append(tid)

        for field, value in _section(mem.get("profile")).items():
            add("profile", str(field), f"{field}: {value}", None)
        for rid, rel in _section(mem.get("social_relationship")).items():
            if not isinstance(rel, dict):
                continue
            text = (
                f"{rel.get('Supporting Characters', '')} ({rel.get('Relationship', '')}): "
                f"{rel.get('Description', '')}"
            )
            add("social_relationship", str(rid), text, None)
        for eid, event in _section(mem.get("events")).items():
            add("events", str(eid), str(event.get("content", "")), event.get("Creation Time"))
        for did, dialogue in _section(mem.get("dialogues")).items():
            for n, (stamp, lines) in enumerate((dialogue.get("contents") or {}).items()):
                text = "\n".join(str(line) for line in lines or [])
                add("dialogues", str(did), text, str(stamp), f":{n}")
        return turns, ids

    def _build(self) -> Iterator[EvalItem]:
        memory: dict[str, Any] = json.loads(self.mem_path.read_text(encoding="utf-8"))
        qa_raw: list[dict[str, Any]] = json.loads(self.qa_path.read_text(encoding="utf-8"))
        characters = [(n, qs) for block in qa_raw for n, qs in block.items()]
        taken = 0
        for name, qs in characters:
            if self.max_characters is not None and taken >= self.max_characters:
                return
            mem = memory.get(name)
            if mem is None:
                self.skipped.append(name)
                continue
            history, ids = self._history(name, mem)
            queries: list[Query] = []
            for kind in self.memory_types:
                for n, qa in enumerate(self._qas(qs.get(kind))):
                    refs = _refs(qa.get("Reference Memory"))
                    gold = [tid for ref in refs for tid in ids.get(f"{kind}:{ref}", [])]
                    unmapped = [ref for ref in refs if f"{kind}:{ref}" not in ids]
                    queries.append(
                        Query(
                            query_id=f"{name}:{kind}:{n}",
                            text=str(qa.get("Question", "")),
                            gold=str(qa.get("Answer", "")),
                            gold_turn_ids=tuple(gold),
                            type_label=kind,
                            meta={
                                "benchmark": "perltqa",
                                "reference_memory": refs,
                                "memory_anchors": qa.get("Memory Anchors") or [],
                                **({"gold_unmapped": unmapped} if unmapped else {}),
                            },
                        )
                    )
            if history and queries:
                taken += 1
                yield EvalItem(name, tuple(history), tuple(queries), {"character": name})

    @staticmethod
    def _qas(block: Any) -> Iterator[dict[str, Any]]:
        """Profile QAs are a flat list; the other types are a list of ``{id: [qa]}``."""
        for entry in block or []:
            if not isinstance(entry, dict):
                continue
            if "Question" in entry:
                yield entry
                continue
            for qa_list in entry.values():
                yield from (qa for qa in qa_list or [] if isinstance(qa, dict))
