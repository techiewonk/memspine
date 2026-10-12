"""Typed loaders for the X-05 labelled sets, plus a content hash for manifests.

Each ``*.v1.jsonl`` file holds one header line and then one JSON record per line.
``load_*`` functions return a :class:`LabelledSet` of frozen dataclasses. No model, network
or engine import is needed, so the loaders run in a bare environment.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, TypeVar

HERE = Path(__file__).resolve().parent

GROUNDING_LABELS = ("supported", "unsupported", "partly")
MODALITIES = ("asserted", "negated", "hypothetical", "planned", "reported", "question")
BINDING_BASES = ("none", "turn", "source_time")
GROUNDING_FAILURES = ("number", "unit", "polarity", "name", "qualifier", "modality", "binding")
PAIR_LABELS = ("same", "different")
PAIR_CATEGORIES = ("alias", "homonym", "numeric", "cross_language")
OUTCOMES = ("supersede", "coexist", "contest", "tentative")
OUTCOME_REASONS = ("cardinality", "modality", "trust", "binding", "time")
BINDINGS = ("bound", "partly", "unbound")
TRUST_TIERS = ("default", "low")
CARDINALITIES = ("one", "many")
STAGES = ("same_key", "cross_key")
QUESTION_TYPES = ("list", "set", "count", "aggregate")

T = TypeVar("T")


@dataclass(frozen=True)
class Header:
    set: str
    version: int
    schema: str
    description: str


@dataclass(frozen=True)
class Turn:
    turn_id: str
    speaker: str
    text: str


@dataclass(frozen=True)
class Span:
    turn_id: str
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class GroundingItem:
    id: str
    lang: str
    category: str
    source_time: str | None
    turns: tuple[Turn, ...]
    assertion: str
    claimed_modality: str
    label: str
    failure: str | None
    support_turns: tuple[str, ...]
    binding_turns: tuple[str, ...]
    binding_basis: str
    span: Span | None
    unsupported_part: str | None


@dataclass(frozen=True)
class Mention:
    mention: str
    type: str
    context: str


@dataclass(frozen=True)
class EntityPair:
    id: str
    category: str
    subcategory: str
    label: str
    a: Mention
    b: Mention
    languages: tuple[str, str]
    note: str


@dataclass(frozen=True)
class Assertion:
    id: str
    text: str
    subject: str
    predicate: str
    value: str
    modality: str
    binding: str
    source_trust: str
    source_family: str
    source_time: str


@dataclass(frozen=True)
class ContradictionItem:
    id: str
    lang: str
    attribute: str
    cardinality: str
    stage: str
    existing: tuple[Assertion, ...]
    incoming: Assertion
    expected: str
    reason: str
    note: str


@dataclass(frozen=True)
class Conversation:
    id: str
    lang: str
    speaker: str
    date: str
    turns: tuple[Turn, ...]


@dataclass(frozen=True)
class GlobalQuestion:
    id: str
    question: str
    question_type: str
    expected_conversations: tuple[str, ...]
    answer_elements: tuple[str, ...]
    expected_count: int | None
    note: str


@dataclass(frozen=True)
class GlobalSet:
    conversations: tuple[Conversation, ...]
    questions: tuple[GlobalQuestion, ...]


@dataclass(frozen=True)
class LabelledSet(Generic[T]):
    header: Header
    items: Any  # tuple[T, ...] for flat sets, GlobalSet for global_questions
    records: tuple[dict[str, Any], ...] = field(repr=False, default=())
    path: Path | None = None

    def __len__(self) -> int:
        return len(self.records)


def read_jsonl(path: Path) -> tuple[Header, list[dict[str, Any]]]:
    lines = [ln.rstrip("\r") for ln in path.read_text(encoding="utf-8").split("\n") if ln.strip()]
    head = json.loads(lines[0])
    header = Header(head["set"], head["version"], head["schema"], head["description"])
    return header, [json.loads(ln) for ln in lines[1:]]


def _turns(rows: list[dict[str, Any]]) -> tuple[Turn, ...]:
    return tuple(Turn(r["turn_id"], r["speaker"], r["text"]) for r in rows)


def _assertion(r: dict[str, Any]) -> Assertion:
    return Assertion(
        r["id"], r["text"], r["subject"], r["predicate"], r["value"], r["modality"],
        r["binding"], r["source_trust"], r["source_family"], r["source_time"],
    )  # fmt: skip


def _grounding(r: dict[str, Any]) -> GroundingItem:
    sp = r["span"]
    return GroundingItem(
        r["id"], r["lang"], r["category"], r["source_time"], _turns(r["turns"]), r["assertion"],
        r["claimed_modality"], r["label"], r["failure"], tuple(r["support_turns"]),
        tuple(r["binding_turns"]), r["binding_basis"],
        Span(sp["turn_id"], sp["start"], sp["end"], sp["text"]) if sp else None,
        r["unsupported_part"],
    )  # fmt: skip


def _pair(r: dict[str, Any]) -> EntityPair:
    return EntityPair(
        r["id"], r["category"], r["subcategory"], r["label"], Mention(**r["a"]), Mention(**r["b"]),
        (r["languages"][0], r["languages"][1]), r["note"],
    )  # fmt: skip


def _contradiction(r: dict[str, Any]) -> ContradictionItem:
    return ContradictionItem(
        r["id"], r["lang"], r["attribute"], r["cardinality"], r["stage"],
        tuple(_assertion(e) for e in r["existing"]), _assertion(r["incoming"]),
        r["expected"], r["reason"], r["note"],
    )  # fmt: skip


def _path(name: str, path: Path | None) -> Path:
    return path or HERE / f"{name}.v1.jsonl"


def load_grounding(path: Path | None = None) -> LabelledSet[GroundingItem]:
    p = _path("grounding", path)
    header, recs = read_jsonl(p)
    return LabelledSet(header, tuple(_grounding(r) for r in recs), tuple(recs), p)


def load_entity_pairs(path: Path | None = None) -> LabelledSet[EntityPair]:
    p = _path("entity_pairs", path)
    header, recs = read_jsonl(p)
    return LabelledSet(header, tuple(_pair(r) for r in recs), tuple(recs), p)


def load_contradictions(path: Path | None = None) -> LabelledSet[ContradictionItem]:
    p = _path("contradictions", path)
    header, recs = read_jsonl(p)
    return LabelledSet(header, tuple(_contradiction(r) for r in recs), tuple(recs), p)


def load_global_questions(path: Path | None = None) -> LabelledSet[Any]:
    p = _path("global_questions", path)
    header, recs = read_jsonl(p)
    convs = tuple(
        Conversation(r["id"], r["lang"], r["speaker"], r["date"], _turns(r["turns"]))
        for r in recs
        if r["kind"] == "conversation"
    )
    qs = tuple(
        GlobalQuestion(
            r["id"], r["question"], r["question_type"], tuple(r["expected_conversations"]),
            tuple(r["answer_elements"]), r["expected_count"], r["note"],
        )  # fmt: skip
        for r in recs
        if r["kind"] == "question"
    )
    return LabelledSet(header, GlobalSet(convs, qs), tuple(recs), p)


LOADERS = {
    "grounding": load_grounding,
    "entity_pairs": load_entity_pairs,
    "contradictions": load_contradictions,
    "global_questions": load_global_questions,
}


def content_hash(labelled: LabelledSet[Any]) -> str:
    """SHA-256 over the canonical JSON of the header and every record (line-ending proof)."""
    h = hashlib.sha256()
    head = {
        "set": labelled.header.set, "version": labelled.header.version,
        "schema": labelled.header.schema, "description": labelled.header.description,
    }  # fmt: skip
    for obj in (head, *labelled.records):
        blob = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        h.update(blob.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()
