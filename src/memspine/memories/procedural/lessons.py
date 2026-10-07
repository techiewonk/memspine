"""W17a / N28 / N24 (plan v3.2, ADR-060): lessons from structured outcome receipts.

A lesson is an *advisory* procedural record built from a receipt the caller already
has (task, action, outcome, reason). The body is a fixed-field template, never a
transcript and never model-written, so its form cannot degrade into a symptom-only
note. Lessons are retrievable through their own verbs and shown after the evidence;
they never execute and never become plans.

N28 counters ride existing events: ``scoring.likes`` counts helpful outcomes that
followed an injection, ``scoring.dislikes`` harmful ones, ``scoring.access_count``
uses (RETRIEVE events) and ``scoring.notes`` repeats of the same lesson key. Pure
functions only; the engine does the I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from memspine.config import constants
from memspine.core.events import fingerprint_payload
from memspine.core.query_shape import content_words
from memspine.core.records import MemoryRecord
from memspine.exceptions import ConflictError

__all__ = [
    "Outcome",
    "OutcomeReceipt",
    "action_signature",
    "doc_type",
    "error_class",
    "failures_dominate",
    "lesson_key",
    "lesson_text",
    "outcome_rank",
    "quarantine_lesson_key",
    "quarantine_lesson_text",
    "render_lesson",
    "should_retire",
    "source_signature",
]

Outcome = Literal["success", "failure"]

#: Literal arguments that make two runs of the same action look different.
_LITERALS = re.compile(r"\"[^\"]*\"|'[^']*'|https?://\S+|\b0x[0-9a-f]+\b|\d+(?:\.\d+)?", re.I)
_ERROR_HEAD = re.compile(r"^\s*([A-Za-z_][\w.]*(?:Error|Exception|Timeout|Failure))\b")


@dataclass(frozen=True)
class OutcomeReceipt:
    """What happened when a task ran: the fixed fields a lesson is built from.

    ``action`` is the action that ran (for a failure, the one that failed);
    ``next_action`` the one that then worked, when known."""

    task: str
    outcome: Outcome
    action: str | None = None
    error: str | None = None
    next_action: str | None = None
    reward: float | None = None
    tool: str | None = None

    def __post_init__(self) -> None:
        if not self.task.strip():
            raise ConflictError("an outcome receipt needs a non-empty task")
        if self.outcome not in ("success", "failure"):
            raise ConflictError(f"outcome must be success or failure, got {self.outcome!r}")


def lesson_text(receipt: OutcomeReceipt) -> str:
    """The fixed-field lesson body: When <task> · tried <action> · failed because
    <error> · worked <next action> (a success reads When <task> · worked <action>)."""
    parts = [f"When {receipt.task.strip()}"]
    if receipt.outcome == "failure":
        if receipt.action:
            parts.append(f"tried {receipt.action.strip()}")
        parts.append(f"failed because {(receipt.error or 'unknown').strip()}")
        if receipt.next_action:
            parts.append(f"worked {receipt.next_action.strip()}")
    else:
        worked = receipt.action or receipt.next_action
        parts.append(f"worked {worked.strip()}" if worked else "succeeded")
    if receipt.tool:
        parts.append(f"tool {receipt.tool.strip()}")
    return " · ".join(parts)


def action_signature(action: str | None) -> str:
    """The abstract shape of an action: literals (quoted strings, URLs, numbers)
    removed, then its first six distinct words in order (AWM ``induce_rule``
    style), so ``click "Buy" at 3`` and ``click "Sell" at 9`` match."""
    if not action:
        return ""
    words = re.findall(r"[a-z][a-z_]+", _LITERALS.sub(" ", action.lower()))
    seen: list[str] = []
    for word in words:
        if word not in seen:
            seen.append(word)
    return " ".join(seen[:6])


def error_class(error: str | None) -> str:
    """The class of an error: its exception name when it has one
    (``TimeoutError: after 30s`` -> ``timeouterror``), else its first three
    content words (sorted) with literals removed."""
    if not error:
        return ""
    head = _ERROR_HEAD.match(error)
    if head:
        return head.group(1).lower()
    return " ".join(sorted(content_words(_LITERALS.sub(" ", error)))[:3])


def lesson_key(receipt: OutcomeReceipt) -> str:
    """The dedup key: (trigger words, outcome, action signatures, error class)."""
    return fingerprint_payload(
        {
            "trigger": sorted(content_words(receipt.task)),
            "outcome": receipt.outcome,
            "action": action_signature(receipt.action),
            "next": action_signature(receipt.next_action),
            "error": error_class(receipt.error),
        }
    )[:16]


def outcome_rank(similarity: float, helpful: int, harmful: int) -> float:
    """N28 ranking: ``cos x (1 + helpful) / (1 + harmful)``; equal to the cosine
    for a record with no outcomes yet."""
    return similarity * (1 + helpful) / (1 + harmful)


def failures_dominate(
    helpful: int, harmful: int, min_harmful: int = constants.OUTCOME_DEMOTE_MIN_HARMFUL
) -> bool:
    """N28 demotion: at least ``min_harmful`` harmful outcomes, and more harmful
    than helpful ones."""
    return harmful >= min_harmful and harmful > helpful


def should_retire(
    helpful: int,
    used: int,
    min_used: int = constants.OUTCOME_RETIRE_MIN_USED,
    min_ratio: float = constants.OUTCOME_RETIRE_MIN_RATIO,
) -> bool:
    """N28 retirement: used at least ``min_used`` times with a helpful share below
    ``min_ratio`` (ReMe ``utility / freq``)."""
    return used >= min_used and helpful / used < min_ratio


def render_lesson(record: MemoryRecord) -> MemoryRecord:
    """A lesson as shown in a read: marked as advisory data, placed after the evidence."""
    return record.model_copy(update={"content": f"{constants.LESSON_MARKER} {record.content}"})


def doc_type(record: MemoryRecord) -> str | None:
    """N26: the document type a record declares (``doctype:<t>`` tag), if any."""
    for tag in record.tags:
        if tag.startswith(constants.DOCTYPE_TAG_PREFIX):
            return tag[len(constants.DOCTYPE_TAG_PREFIX) :]
    return None


def source_signature(record: MemoryRecord) -> str:
    """N24: where held content came from: role / channel / document type."""
    return f"{record.source.role}/{record.source.channel}/{doc_type(record) or '-'}"


def _reason_kinds(reasons: list[str]) -> list[str]:
    return sorted({reason.split("(", 1)[0] for reason in reasons if reason != "quarantined"})


def quarantine_lesson_key(record: MemoryRecord, reasons: list[str]) -> str:
    """N24: one lesson per (source signature, reason kinds); repeats count as notes."""
    return fingerprint_payload(
        {"source": source_signature(record), "reasons": _reason_kinds(reasons)}
    )[:16]


def quarantine_lesson_text(record: MemoryRecord, reasons: list[str]) -> str:
    """N24: the advisory lesson for a quarantine verdict. It names the source, the
    reason kinds and the content hash, and never copies the held text, so the
    lesson cannot re-inject the payload."""
    kinds = _reason_kinds(reasons)
    return (
        f"When content arrives from {source_signature(record)} · it was held because "
        f"{', '.join(kinds) or 'quarantined'} · content hash {record.content_fingerprint}"
    )
