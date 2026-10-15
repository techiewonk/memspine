"""Parse only conversation inputs; keep benchmark labels outside ingestion."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

CATEGORIES = {1: "multi_hop", 2: "temporal", 3: "open_domain", 4: "single_hop", 5: "adversarial"}


@dataclass(frozen=True)
class Turn:
    session: int
    dia_id: str
    speaker: str
    content: str
    timestamp: datetime
    role: str
    exchange: int


@dataclass(frozen=True)
class Question:
    index: int
    question: str
    reference: str
    category: int
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class Sample:
    sample_id: str
    turns: tuple[Turn, ...]
    questions: tuple[Question, ...]


def parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = datetime.strptime(value, "%I:%M %p on %d %B, %Y")
    # LoCoMo does not provide a timezone. Use UTC consistently; record this assumption.
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def load_dataset(path: Path) -> list[Sample]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("Dataset must be a nonempty JSON array")
    samples = []
    seen_samples = set()
    for index, item in enumerate(raw):
        sample_id = str(item.get("sample_id", index))
        if sample_id in seen_samples:
            raise ValueError(f"Duplicate sample_id: {sample_id}")
        seen_samples.add(sample_id)
        conversation = item["conversation"]
        speakers = (conversation["speaker_a"], conversation["speaker_b"])
        session_keys = sorted(
            (key for key in conversation if re.fullmatch(r"session_\d+", key)),
            key=lambda key: int(key.split("_")[1]),
        )
        turns = []
        seen_turns = set()
        for key in session_keys:
            timestamp = parse_timestamp(conversation[f"{key}_date_time"])
            exchange = 0
            for turn in conversation[key]:
                dia_id = str(turn["dia_id"])
                if dia_id in seen_turns:
                    raise ValueError(f"Duplicate dia_id {dia_id} in sample {sample_id}")
                seen_turns.add(dia_id)
                if turn["speaker"] not in speakers:
                    raise ValueError(f"Unknown speaker: {turn['speaker']}")
                role = "user" if turn["speaker"] == speakers[0] else "assistant"
                if role == "user":
                    exchange += 1
                text = str(turn.get("text", ""))
                if turn.get("blip_caption"):
                    text = f"{text}\n[Image caption: {turn['blip_caption']}]".strip()
                turns.append(Turn(int(key.split("_")[1]), dia_id, turn["speaker"], text,
                                  timestamp, role, exchange))
        if not turns:
            raise ValueError(f"Sample {sample_id} has no conversation turns")
        questions = []
        for qi, qa in enumerate(item["qa"]):
            category = int(qa["category"])
            if category not in CATEGORIES:
                raise ValueError(f"Unsupported category {category}")
            reference = "Not mentioned in the conversation" if category == 5 else qa.get("answer")
            if reference is None or not str(reference).strip():
                raise ValueError(f"Missing reference for {sample_id}:{qi}")
            questions.append(Question(qi, str(qa["question"]), str(reference), category,
                                      tuple(qa.get("evidence", []))))
        if not questions:
            raise ValueError(f"Sample {sample_id} has no questions")
        samples.append(Sample(sample_id, tuple(turns), tuple(questions)))
    return samples


def dataset_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
