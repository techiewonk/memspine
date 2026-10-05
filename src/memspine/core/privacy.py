"""Data-subject and audit primitives (#46-#50): pure logic, no I/O.

- the request-scoped **principal** and read **purpose** (context variables the REST
  auth middleware and the read verbs set, the read gates and the audit read),
- the **purpose gate** over a record's ``consent_tags`` (``consent.enforce``),
- the **audit hash chain** over ``memory.read_audit`` / ``memory.audit`` events,
- **retention class** matching (``retention.classes``),
- the **export line** encoders (subject-access export, JSONL).
"""

from __future__ import annotations

import fnmatch
import hashlib
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import orjson

from memspine.core.events import EventKind, MemoryEvent, canonical_payload
from memspine.core.records import MemoryRecord, PiiTier

if TYPE_CHECKING:
    from memspine.config.schema import ConsentConfig, RetentionClassConfig

__all__ = [
    "AUDIT_GENESIS",
    "AUDIT_KINDS",
    "PURPOSE_ANY",
    "PURPOSE_NONE",
    "AuditChainReport",
    "chain_digest",
    "current_principal",
    "current_purpose",
    "export_line",
    "export_record",
    "inherited_consent",
    "inherited_pii",
    "matching_class",
    "payload_mentions",
    "pii_rank",
    "principal_scope",
    "purpose_allows",
    "read_scope",
    "verify_audit_chain",
]

#: The chain anchor of the first audit event in a full log.
AUDIT_GENESIS = "memspine-audit-v1"
#: Event kinds the audit hash chain covers.
AUDIT_KINDS = frozenset({EventKind.READ_AUDIT, EventKind.AUDIT})
#: A record purpose that allows every read purpose.
PURPOSE_ANY = "*"
#: The purpose set of a record derived from parents whose purposes do not
#: overlap: no read purpose matches it, so the record serves no tagged read.
PURPOSE_NONE = "!none"

_PII_ORDER = {PiiTier.NONE: 0, PiiTier.LOW: 1, PiiTier.HIGH: 2, PiiTier.REGULATED: 3}

_PRINCIPAL: ContextVar[str | None] = ContextVar("memspine_principal", default=None)
_PURPOSE: ContextVar[str | None] = ContextVar("memspine_purpose", default=None)
_READ_DEPTH: ContextVar[int] = ContextVar("memspine_read_depth", default=0)


def pii_rank(tier: PiiTier | str) -> int:
    """Order of PII tiers: none < low < high < regulated."""
    return _PII_ORDER[PiiTier(tier)]


# ── principal + purpose (request scope) ──────────────────────────────────────


def current_principal() -> str | None:
    """The authenticated principal of the current request, if any."""
    return _PRINCIPAL.get()


def current_purpose() -> str | None:
    """The purpose of the read in progress, if one was given."""
    return _PURPOSE.get()


@contextmanager
def principal_scope(principal: str | None) -> Iterator[None]:
    """Bind ``principal`` for the duration of the block (REST auth middleware)."""
    token = _PRINCIPAL.set(principal)
    try:
        yield
    finally:
        _PRINCIPAL.reset(token)


@contextmanager
def read_scope(purpose: str | None) -> Iterator[bool]:
    """Enter a read verb. Yields True for the OUTERMOST read only, so a verb that
    calls another public read verb (``shared_search`` -> ``search``) audits once.
    A nested verb without a purpose keeps the outer one."""
    depth = _READ_DEPTH.get()
    depth_token = _READ_DEPTH.set(depth + 1)
    purpose_token = _PURPOSE.set(purpose) if purpose is not None or depth == 0 else None
    try:
        yield depth == 0
    finally:
        if purpose_token is not None:
            _PURPOSE.reset(purpose_token)
        _READ_DEPTH.reset(depth_token)


def purpose_allows(record: MemoryRecord, purpose: str | None, consent: ConsentConfig) -> bool:
    """#50 purpose gate. Off (``consent.enforce`` false) every record passes.

    On: a record with no purposes passes when ``consent.untagged`` is ``allow``; a
    record carrying ``*`` passes every read; otherwise the read's purpose must be one
    of the record's purposes (a read with no purpose sees no tagged record)."""
    if not consent.enforce:
        return True
    purposes = record.consent_tags
    if not purposes:
        return consent.untagged == "allow"
    if PURPOSE_ANY in purposes:
        return True
    return purpose is not None and purpose != PURPOSE_NONE and purpose in purposes


def inherited_consent(tag_sets: Iterable[Sequence[str]], untagged: str = "allow") -> list[str]:
    """The purposes a record derived from parts carrying ``tag_sets`` may serve:
    the intersection of the parts' purposes, so the derived record is never
    visible to a read that could not see every part.

    ``*`` is the universal set. An untagged part is universal under
    ``consent.untagged: allow`` (it passes every read); under ``deny`` it passes
    none, so a mix of tagged and untagged parts gets :data:`PURPOSE_NONE`. All
    parts untagged: untagged (``[]``). Disjoint purposes: :data:`PURPOSE_NONE`."""
    sets = [list(tags) for tags in tag_sets]
    if not any(sets):
        return []
    if untagged == "deny" and any(not tags for tags in sets):
        return [PURPOSE_NONE]
    specific = [set(tags) for tags in sets if tags and PURPOSE_ANY not in tags]
    if not specific:
        return [PURPOSE_ANY]
    common = set.intersection(*specific) - {PURPOSE_NONE}
    return sorted(common) if common else [PURPOSE_NONE]


def inherited_pii(tiers: Iterable[PiiTier | str]) -> PiiTier:
    """The highest of ``tiers`` (``none`` when empty): derived text carries the
    most sensitive tier of what it was derived from."""
    best = PiiTier.NONE
    for tier in tiers:
        if pii_rank(tier) > pii_rank(best):
            best = PiiTier(tier)
    return best


# ── audit hash chain ─────────────────────────────────────────────────────────


def chain_digest(prev: str, kind: str, namespace: str, actor: str, body: dict[str, Any]) -> str:
    """SHA-256 over the previous hash and the event's kind, namespace, actor and
    canonical payload (without its ``chain`` field)."""
    content = {key: value for key, value in body.items() if key != "chain"}
    message = b"|".join(
        [
            prev.encode(),
            kind.encode(),
            namespace.encode(),
            actor.encode(),
            canonical_payload(content),
        ]
    )
    return hashlib.sha256(message).hexdigest()


@dataclass
class AuditChainReport:
    """Result of :func:`verify_audit_chain`."""

    events: int = 0
    ok: bool = True
    #: seq of the first event whose link or hash does not verify (None = intact).
    broken_at: int | None = None
    head: str = AUDIT_GENESIS
    problems: list[str] = field(default_factory=list)


def verify_audit_chain(events: Iterable[MemoryEvent], *, anchored: bool = True) -> AuditChainReport:
    """Check the hash chain over the audit events of ``events`` (seq order).

    ``anchored``: the first audit event must link to :data:`AUDIT_GENESIS` (a full
    log). A rolling log prunes old events, so the first surviving event's ``prev``
    is accepted as the anchor there (``anchored=False``)."""
    report = AuditChainReport()
    prev: str | None = AUDIT_GENESIS if anchored else None
    for event in events:
        if event.kind not in AUDIT_KINDS:
            continue
        report.events += 1
        chain = event.payload.get("chain")
        seq = int(event.seq or -1)
        if not isinstance(chain, dict):
            report.problems.append(f"seq {seq}: no chain field")
        else:
            link = str(chain.get("prev", ""))
            expected = chain_digest(
                link, event.kind.value, event.namespace, event.actor, event.payload
            )
            if prev is not None and link != prev:
                report.problems.append(f"seq {seq}: prev link does not match")
            elif chain.get("hash") != expected:
                report.problems.append(f"seq {seq}: hash does not match its content")
            prev = str(chain.get("hash", ""))
        if report.problems and report.broken_at is None:
            report.broken_at = seq
    report.ok = not report.problems
    report.head = prev or AUDIT_GENESIS
    return report


# ── retention classes ────────────────────────────────────────────────────────


def matching_class(
    record: MemoryRecord, classes: Sequence[RetentionClassConfig]
) -> RetentionClassConfig | None:
    """The first retention class whose namespace glob and memory type match."""
    for cls in classes:
        if cls.memory_type is not None and cls.memory_type != record.memory_type:
            continue
        if fnmatch.fnmatchcase(record.namespace, cls.namespace):
            return cls
    return None


# ── subject-access export ────────────────────────────────────────────────────

#: Record fields left out of an export line: dedup sketches and the compressed
#: duplicate of the content (the inflated ``content`` is exported instead).
_EXPORT_DROP = frozenset({"simhash", "minhash_sig", "content_zstd"})


def export_line(kind: str, body: dict[str, Any]) -> str:
    """One JSONL line, keys sorted, so equal exports are byte-identical."""
    return orjson.dumps({"type": kind, **body}, option=orjson.OPT_SORT_KEYS).decode()


def export_record(record: MemoryRecord, *, include_history: bool) -> dict[str, Any]:
    """The exported form of one record: everything but the dedup sketches."""
    data = record.model_dump(mode="json", exclude=set(_EXPORT_DROP))
    if not include_history:
        data.pop("history", None)
    return data


def payload_mentions(node: Any, ids: set[str]) -> bool:
    """True if any string value anywhere in ``node`` is one of ``ids``."""
    if isinstance(node, str):
        return node in ids
    if isinstance(node, dict):
        return any(payload_mentions(value, ids) for value in node.values())
    if isinstance(node, list):
        return any(payload_mentions(item, ids) for item in node)
    return False
