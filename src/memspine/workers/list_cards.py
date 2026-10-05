"""#30 (SimpleMem synthesis): person-level list cards over mined event facts.

A list question ("What activities does Melanie partake in?") needs every item, and
the items sit in event facts mined from different sessions. This step, run after
``mine_facts`` when ``consolidation.list_cards`` is on, groups the live EVENT facts
of each namespace by (person, class) and keeps one derived card per group::

    Melanie — activities: pottery class (2023-05), camping with her kids (2023-07)

* the person is the fact's ``person:`` tags (#28), else its entity;
* the class is its ``topic:`` tag (#28), else its miner attribute ("Melanie hobby:
  ..." -> "hobby") unless that is generic ("event"); a fact with neither gets a
  class from one ``extract@classes`` call per person batch (cached in the log as a
  ``list_classes`` MARKER, so it is asked once);
* a card's parents are its facts, so erasing a fact cascades to the card; its trust
  is capped at the least trusted fact by the engine's write door;
* a card is re-derived only when its fingerprint (members and text) changes: the
  old card is archived and the new one written. An unchanged group writes nothing.

Grouping and rendering are deterministic. Only the class labeller calls a model.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from memspine.config import constants
from memspine.core.event_date import happened_of
from memspine.core.events import EventKind, MemoryEvent, fingerprint_payload
from memspine.core.fact_views import (
    PERSON_PREFIX,
    TOPIC_PREFIX,
    normalise_view,
    tag_values,
    view_tags,
)
from memspine.core.records import MemoryRecord, RecordStatus, chrono_key
from memspine.observability.logging import get_logger

if TYPE_CHECKING:
    from memspine.workers.pipelines import PipelineContext

__all__ = [
    "LIST_CARD_KEY_PREFIX",
    "LIST_CLASSES_MARKER",
    "DepositListCard",
    "LabelClasses",
    "ListCard",
    "class_key",
    "derive_list_cards",
    "fact_class",
    "fact_persons",
    "fact_statement",
    "render_list_card",
]

_log = get_logger(__name__)

#: MARKER event caching the LLM class labels: ``{"labels": {record_id: label}}``.
LIST_CLASSES_MARKER = "list_classes"
LIST_CARD_KEY_PREFIX = "listkey:"
_FP_PREFIX = "listfp:"


@dataclass(frozen=True)
class ListCard:
    """One derived list card, ready for the engine's write door."""

    person: str
    label: str
    text: str
    parents: list[str]
    valid_from: datetime
    tags: list[str]


#: (namespace, card or None, ids of the cards it replaces) -> the stored card, or
#: None. A None card only archives the replaced ones (the group fell apart).
DepositListCard = Callable[[str, ListCard | None, list[str]], Awaitable[object]]
#: (person, numbered fact statements) -> {1-based index: class label}.
LabelClasses = Callable[[str, list[str]], Awaitable[dict[int, str]]]


def class_key(label: str) -> str:
    """The grouping key of a class label: normalised, last word singular-ish
    ("Activities" and "activity" group together)."""
    words = normalise_view(label).split()
    if not words:
        return ""
    last = words[-1]
    if last.endswith("ies") and len(last) > 4:
        last = last[:-3] + "y"
    elif last.endswith("s") and not last.endswith("ss") and len(last) > 3:
        last = last[:-1]
    return " ".join([*words[:-1], last])


def _split(record: MemoryRecord) -> tuple[str | None, str]:
    """A mined fact ``"<entity> <attribute>: <statement>"`` -> (attribute, statement);
    other content -> (None, the whole text)."""
    text = " ".join(record.content.split())
    entity = record.entity or ""
    if entity and text[: len(entity)].casefold() == entity.casefold():
        rest = text[len(entity) :].lstrip()
        if ": " in rest:
            attribute, statement = rest.split(": ", 1)
            return attribute.strip() or None, statement.strip()
    return None, text


def fact_statement(record: MemoryRecord) -> str:
    """The statement of a mined fact, without its ``entity attribute:`` key."""
    return _split(record)[1]


def fact_class(record: MemoryRecord) -> str | None:
    """The fact's list class: its ``topic:`` tag, else a non-generic miner attribute."""
    topics = tag_values(record, TOPIC_PREFIX)
    if topics:
        return topics[0]
    attribute = _split(record)[0]
    if attribute is None:
        return None
    norm = normalise_view(attribute)
    return None if norm in constants.LIST_CARD_GENERIC_CLASSES else norm


def fact_persons(record: MemoryRecord) -> list[str]:
    """The normalised people a fact is about: its ``person:`` tags, else its entity."""
    persons = tag_values(record, PERSON_PREFIX)
    if persons:
        return persons
    return [normalise_view(record.entity)] if record.entity else []


def _month(record: MemoryRecord) -> str:
    """The item's date: the happened date's month when tagged, else ``valid_from``'s."""
    happened = happened_of(record)
    if happened:
        return happened[:7]
    return f"{record.valid_from:%Y-%m}"


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit // 2 else cut).rstrip(" ,;:") + "…"


def render_list_card(person: str, label: str, members: list[MemoryRecord]) -> str:
    """``"<Person> — <class>: item (YYYY-MM), item (YYYY-MM)"``, oldest first.

    Identical items (same statement and month) are listed once. Past
    :data:`constants.LIST_CARD_MAX_ITEMS` the newest are kept and the rest counted.
    """
    items: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for record in sorted(members, key=lambda r: (_month(r), chrono_key(r))):
        statement = _cut(fact_statement(record), constants.LIST_CARD_ITEM_MAX_CHARS)
        month = _month(record)
        if (statement.casefold(), month) in seen:
            continue
        seen.add((statement.casefold(), month))
        items.append((statement, month))
    dropped = max(0, len(items) - constants.LIST_CARD_MAX_ITEMS)
    shown = ", ".join(f"{s} ({m})" for s, m in items[dropped:])
    more = f" (+{dropped} earlier)" if dropped else ""
    return f"{person} — {label}: {shown}{more}"


def _live_event_fact(record: MemoryRecord) -> bool:
    return (
        record.status is RecordStatus.ACTIVATED
        and not record.quarantined
        and not record.instruction_flag
        and "atomic_fact" in record.tags
        and "kind:event" in record.tags
        and constants.LIST_CARD_TAG not in record.tags
    )


def _display_person(key: str, members: list[MemoryRecord]) -> str:
    """The person as written: a member entity naming them, else the key title-cased."""
    names = sorted(
        {m.entity for m in members if m.entity and normalise_view(m.entity) == key},
    )
    return names[0] if names else key.title()


def _display_label(labels: list[str]) -> str:
    """The group's most common label (ties: alphabetical)."""
    counts: dict[str, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    return sorted(counts, key=lambda label: (-counts[label], label))[0]


async def _label_unclassed(
    ctx: PipelineContext, namespace: str, unclassed: dict[str, list[MemoryRecord]]
) -> dict[str, str]:
    """#30: LLM class labels of the facts with no deterministic class, one call per
    person batch, cached in the log. Returns ``{record_id: label}`` ("" = no list)."""
    cache = ctx.session_index.list_classes
    labels = {
        r.record_id: cache[(namespace, r.record_id)]
        for batch in unclassed.values()
        for r in batch
        if (namespace, r.record_id) in cache
    }
    if ctx.label_classes is None or ctx.append_event is None:
        return labels
    for person, batch in sorted(unclassed.items()):
        todo = [r for r in batch if r.record_id not in labels]
        if not todo:
            continue
        try:
            found = await ctx.label_classes(person, [fact_statement(r) for r in todo])
        except Exception as exc:  # an enhancer, never a gate: asked again next cycle
            _log.warning("list_cards.label_failed", namespace=namespace, error=str(exc))
            continue
        fresh = {}
        for position, record in enumerate(todo, 1):
            label = normalise_view(found.get(position) or "")
            fresh[record.record_id] = label
        await ctx.append_event(
            MemoryEvent(
                kind=EventKind.MARKER,
                namespace=namespace,
                actor="system",
                payload={"marker": LIST_CLASSES_MARKER, "labels": fresh},
            )
        )
        for record_id, label in fresh.items():
            cache[(namespace, record_id)] = label
        labels.update(fresh)
    return labels


def _groups(
    facts: list[MemoryRecord], llm_labels: dict[str, str]
) -> dict[tuple[str, str], tuple[list[MemoryRecord], list[str]]]:
    """(person key, class key) -> (members, their labels)."""
    groups: dict[tuple[str, str], tuple[list[MemoryRecord], list[str]]] = {}
    for record in facts:
        label = fact_class(record) or llm_labels.get(record.record_id) or ""
        key = class_key(label)
        if not key:
            continue
        for person in fact_persons(record):
            members, labels = groups.setdefault((person, key), ([], []))
            members.append(record)
            labels.append(label)
    return groups


async def derive_list_cards(ctx: PipelineContext) -> dict[str, object]:
    """#30: keep one list card per (person, class) of live event facts, per namespace."""
    if ctx.deposit_list_card is None or ctx.append_event is None:
        return {"status": "skipped", "reason": "no list-card deposit in this context"}
    await ctx.session_index.refresh(ctx.storage)
    written = kept = archived = 0
    errors: list[str] = []
    for namespace in await ctx.storage.list_namespaces():
        records = await ctx.storage.list_records(namespace, "semantic")
        facts = [r for r in records if _live_event_fact(r)]
        existing: dict[str, list[MemoryRecord]] = {}
        for record in records:
            if constants.LIST_CARD_TAG in record.tags and record.status is RecordStatus.ACTIVATED:
                for key in tag_values(record, LIST_CARD_KEY_PREFIX):
                    existing.setdefault(key, []).append(record)
        unclassed: dict[str, list[MemoryRecord]] = {}
        for record in facts:
            if fact_class(record) is None:
                for person in fact_persons(record):
                    unclassed.setdefault(person, []).append(record)
        llm_labels = await _label_unclassed(ctx, namespace, unclassed) if unclassed else {}
        for (person_key, key), (members, labels) in sorted(_groups(facts, llm_labels).items()):
            if len(members) < constants.LIST_CARD_MIN_ITEMS:
                continue
            group = fingerprint_payload({"person": person_key, "class": key})
            person = _display_person(person_key, members)
            label = _display_label(labels)
            text = render_list_card(person, label, members)
            ids = sorted(m.record_id for m in members)
            fp = fingerprint_payload({"members": ids, "text": text})
            old = existing.pop(group, [])
            if len(old) == 1 and f"{_FP_PREFIX}{fp}" in old[0].tags:
                kept += 1
                continue
            card = ListCard(
                person=person,
                label=label,
                text=text,
                parents=ids,
                valid_from=max(m.valid_from for m in members),
                tags=[
                    constants.LIST_CARD_TAG,
                    "atomic_fact",
                    f"{LIST_CARD_KEY_PREFIX}{group}",
                    f"{_FP_PREFIX}{fp}",
                    *view_tags([person_key], None, label),
                ],
            )
            try:
                stored = await ctx.deposit_list_card(namespace, card, [o.record_id for o in old])
            except Exception as exc:  # one bad card must not lose the rest
                errors.append(f"{namespace}:{person_key}/{key}: list card failed: {exc}")
                continue
            archived += len(old)
            written += stored is not None
        for old in existing.values():  # the group fell below the floor or emptied
            try:
                await ctx.deposit_list_card(namespace, None, [o.record_id for o in old])
            except Exception as exc:
                errors.append(f"{namespace}: list card archive failed: {exc}")
                continue
            archived += len(old)
    return {
        "status": "ok" if not errors else "partial",
        "cards": written,
        "kept": kept,
        "archived": archived,
        "errors": errors,
    }
