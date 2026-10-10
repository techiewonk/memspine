"""I56: enumerate-then-count for count questions (``--count-verify``).

A 9B reader asked "how many X" estimates ("at least five") instead of listing the distinct
events and counting them; the same event under two dates is counted twice. Dev baseline: five
wrong LoCoMo counts of this shape (3-19, 3-49, 3-52, 3-80, 0-40). This module moves the
counting out of the model:

1. the reader lists the distinct items with the date of the memory line each comes from, as
   JSON (the model does the reading);
2. code merges mentions of one item (:func:`dedupe_items`) and counts (the model does not
   count);
3. the answer is that count with the items.

Two designs, one parser:

* ``two_call``: the normal reader answers first (kept as the fallback and in the row's
  ``meta["count_verify"]["inner_answer"]``), then a second call with :data:`ENUMERATE_PROMPT`
  returns the JSON list. Cost: +1 reader call on count questions only.
* ``single``: one call with :data:`SINGLE_PROMPT` (the JSON list, then a ``Count: N`` line)
  replaces the normal answer on count questions. Cost: +0 calls, a longer completion. The
  model's own ``Count`` is recorded next to the code's count and never used for the answer.

Detection is by question shape only (``memspine.core.query_shape.is_count``); no gold, no
category, no dataset name (rule I37). Non-count questions go to the inner reader untouched, so
every other row is byte-identical to a run without the flag. A reply that does not parse, or a
list that is empty, falls back to the inner answer (two_call) or to the model's text (single).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .contracts import ReaderAnswer

__all__ = [
    "COUNT_VERIFY_MODES",
    "ENUMERATE_PROMPT",
    "SINGLE_PROMPT",
    "CountVerifyReader",
    "ListedItem",
    "dedupe_items",
    "parse_listing",
    "render_count_answer",
]

COUNT_VERIFY_MODES = ("two_call", "single")
#: Bump when the parser, the merge rules or the answer format change.
COUNT_VERIFY_VERSION = "v1"

_LIST_HEAD = (
    "List the distinct items or events the question asks to count, using only the memories "
    "below. Each memory is one line and may start with [YYYY-MM-DD], the date it was said. Go "
    "through every line. Give one entry per distinct item or event: if the same item or event "
    "is mentioned on several lines, list it once, with the date of the earliest line. Give each "
    "item a short name that identifies it (who, what). Do not add items the memories do not "
    "state. Use an empty date (\"\") when the line has no date. "
)
#: Two-call mode, second call: JSON only.
ENUMERATE_PROMPT = (
    _LIST_HEAD
    + 'Reply with JSON only: {{"items": [{{"item": "<short name>", "date": "YYYY-MM-DD"}}]}}\n\n'
    "Memories:\n{context}\n\nQuestion date: {question_date}\nQuestion: {question}\nJSON:"
)
#: Single-call mode: the same list, then the count on its own line.
SINGLE_PROMPT = (
    _LIST_HEAD
    + 'Reply with the JSON list {{"items": [{{"item": "<short name>", "date": "YYYY-MM-DD"}}]}} '
    "and then, on a new line, Count: <the number of entries in your list>.\n\n"
    "Memories:\n{context}\n\nQuestion date: {question_date}\nQuestion: {question}\nJSON:"
)

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE = re.compile(r"```(?:json)?", re.I)
_MODEL_COUNT = re.compile(r"\bcount\s*[:=]\s*(\d+)", re.I)
_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    ["the", "a", "an", "his", "her", "their", "my", "our", "its", "of", "to", "in", "on", "at", "and", "or", "for", "with", "by", "from", "as", "is", "was", "were", "that", "this"]
)
_EVENT_COUNT = re.compile(r"\bhow (?:many times|often)\b", re.I)


@dataclass(frozen=True, slots=True)
class ListedItem:
    item: str
    date: str = ""


def _json_candidates(text: str) -> list[Any]:
    cleaned = _FENCE.sub("", _THINK.sub("", text))
    decoder = json.JSONDecoder()
    found: list[Any] = []
    pos = 0
    while pos < len(cleaned):
        start = min((i for i in (cleaned.find("{", pos), cleaned.find("[", pos)) if i >= 0), default=-1)
        if start < 0:
            break
        try:
            value, end = decoder.raw_decode(cleaned, start)
        except ValueError:
            pos = start + 1
            continue
        found.append(value)
        pos = end
    return found


def parse_listing(text: str, context: str = "") -> tuple[list[ListedItem], int | None] | None:
    """The items of a JSON listing and the model's own stated count (``Count: N``), or None
    when no JSON list can be read. A date that does not occur in ``context`` is dropped (a
    hallucinated date would block a same-date merge); an empty ``context`` keeps all dates."""
    for value in _json_candidates(text):
        raw = value.get("items") if isinstance(value, dict) else value
        if not isinstance(raw, list):
            continue
        items: list[ListedItem] = []
        for entry in raw:
            if isinstance(entry, str):
                name, date = entry, ""
            elif isinstance(entry, dict):
                name = str(
                    entry.get("item") or entry.get("name") or entry.get("event") or ""
                )
                date = str(entry.get("date") or "")
            else:
                continue
            name = " ".join(name.split())
            if not name:
                continue
            m = _DATE.search(date)
            date = m.group(0) if m else ""
            if date and context and date not in context:
                date = ""
            items.append(ListedItem(name, date))
        tail = _THINK.sub("", text)
        stated = _MODEL_COUNT.findall(tail)
        return items, (int(stated[-1]) if stated else None)
    return None


def _content(name: str) -> frozenset[str]:
    out = set()
    for w in _WORD.findall(name.lower()):
        if w in _STOP:
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]  # crude plural fold: "tournaments" ~ "tournament"
        out.add(w)
    return frozenset(out)


def _same_item(a: ListedItem, ca: frozenset[str], b: ListedItem, cb: frozenset[str], events: bool) -> bool:
    if not ca or not cb:
        return a.item.strip().lower() == b.item.strip().lower()
    if ca == cb:  # identical normalised strings
        return not (events and a.date and b.date and a.date != b.date)
    if a.date and a.date == b.date:  # same-date merging: one line, overlapping content
        small, big = (ca, cb) if len(ca) <= len(cb) else (cb, ca)
        overlap = len(ca & cb)
        return small <= big or overlap / len(ca | cb) >= 0.5
    return False


def dedupe_items(items: Sequence[ListedItem], *, events: bool = False) -> list[ListedItem]:
    """Merge mentions of the same item. Two entries are one item when their normalised strings
    are equal (lower case, no punctuation, no articles or possessives, plural folded), or when
    they carry the same date and one's content words contain the other's (or overlap by at
    least half). ``events`` ("how many times / how often"): equal strings on two different
    dates are two events and stay apart; for things ("how many pets") they merge. The first
    entry of a group is kept."""
    kept: list[ListedItem] = []
    content: list[frozenset[str]] = []
    for it in items:
        c = _content(it.item)
        hit = next(
            (
                i
                for i, (k, ck) in enumerate(zip(kept, content, strict=True))
                if _same_item(k, ck, it, c, events)
            ),
            None,
        )
        if hit is None:
            kept.append(it)
            content.append(c)
        elif not kept[hit].date and it.date:
            kept[hit] = ListedItem(kept[hit].item, it.date)
    return kept


def render_count_answer(items: Sequence[ListedItem]) -> str:
    """``3: A (2023-05-01); B; C (2023-06-02)``: the count first, then the items."""
    parts = [f"{i.item} ({i.date})" if i.date else i.item for i in items]
    return f"{len(items)}: " + "; ".join(parts)


class CountVerifyReader:
    """Wraps a reader (see the module docstring). ``enumerator`` is a second reader built with
    :data:`ENUMERATE_PROMPT` (``two_call``) or :data:`SINGLE_PROMPT` (``single``); it shares the
    model, sampler and context guard of ``inner``."""

    def __init__(
        self,
        inner: Any,
        enumerator: Any,
        mode: str = "two_call",
        *,
        is_count: Callable[[str], bool] | None = None,
        table: Any = None,
    ) -> None:
        if mode not in COUNT_VERIFY_MODES:
            raise ValueError(f"count-verify mode must be one of {COUNT_VERIFY_MODES}, got {mode!r}")
        if is_count is None:
            from memspine.core.query_shape import is_count as shape_is_count

            is_count = shape_is_count
        self.inner = inner
        self.enumerator = enumerator
        self.mode = mode
        #: A06: an ``evidence_table.EvidenceTableBuilder``; when set, the table (validated rows,
        #: identity dedupe, reconciled with explicit totals) replaces the enumerator's listing.
        self.table = table
        self._is_count = is_count
        self.guard = getattr(inner, "guard", None)
        self.reader_id = f"{inner.reader_id}+count-{'table' if table else mode.replace('_', '')}"
        self.model = inner.model
        self.makes_model_calls = True
        #: counters for the run log: count questions seen, answers replaced, replies that did
        #: not parse (fell back), extra reader calls spent
        self.seen = 0
        self.replaced = 0
        self.fallbacks = 0
        self.extra_calls = 0

    def describe(self) -> Mapping[str, Any]:
        return {
            **self.inner.describe(),
            "count_verify": self.mode,
            "count_verify_version": COUNT_VERIFY_VERSION,
            "count_prompt": "table"
            if self.table is not None
            else ("enumerate" if self.mode == "two_call" else "single"),
            **(dict(self.table.describe()) if self.table is not None else {}),
        }

    async def answer(
        self, question: str, context: str, question_date: str | None = None
    ) -> ReaderAnswer:
        if not self._is_count(question) or not context.strip():
            return await self.inner.answer(question, context, question_date)
        self.seen += 1
        if self.table is not None:
            return await self._answer_from_table(question, context, question_date)
        first: ReaderAnswer | None = None
        if self.mode == "two_call":
            first = await self.inner.answer(question, context, question_date)
            self.extra_calls += 1
        listing: ReaderAnswer = await self.enumerator.answer(question, context, question_date)
        parsed = parse_listing(listing.text, context)
        events = bool(_EVENT_COUNT.search(question))
        merged = dedupe_items(parsed[0], events=events) if parsed else []
        base = first if first is not None else listing
        meta: dict[str, Any] = {
            "mode": self.mode,
            "version": COUNT_VERIFY_VERSION,
            "extra_calls": 1 if first is not None else 0,
            "n_listed": len(parsed[0]) if parsed else None,
            "n_counted": len(merged),
            "model_count": parsed[1] if parsed else None,
            "listing": listing.text[:1500],
        }
        if first is not None:
            meta["inner_answer"] = first.text
        if not merged:
            self.fallbacks += 1
            meta["fallback"] = "unparsed" if parsed is None else "empty_list"
            text = first.text if first is not None else listing.text
        else:
            self.replaced += 1
            text = render_count_answer(merged)
        calls = (first.model_calls if first else 0) + listing.model_calls
        return ReaderAnswer(
            text=text,
            prompt_tokens=(first.prompt_tokens if first else 0) + listing.prompt_tokens,
            completion_tokens=(first.completion_tokens if first else 0) + listing.completion_tokens,
            latency_ms=(first.latency_ms if first else 0.0) + listing.latency_ms,
            model_calls=calls,
            truncated=listing.truncated and not merged,
            cached_prompt_tokens=(first.cached_prompt_tokens if first else 0)
            + listing.cached_prompt_tokens,
            finish_reason=listing.finish_reason,
            raw_text=base.raw_text,
            prompt_variant=base.prompt_variant,
            extra_meta={**dict(base.extra_meta), "count_verify": meta},
        )

    async def _answer_from_table(
        self, question: str, context: str, question_date: str | None
    ) -> ReaderAnswer:
        """A06: the normal reader answers (the fallback); the evidence table is built (one
        structured call, shared with ``slot_verify`` through the builder's memo) and, when it holds
        a usable count (``resolved`` or ``range``), the answer becomes the table's items with
        their count. ``empty`` / ``unresolved`` / a failed call leave the reader's answer."""
        first: ReaderAnswer = await self.inner.answer(question, context, question_date)
        self.extra_calls += 1
        table, p_tok, c_tok, calls = await self.table.build(question, context)
        meta: dict[str, Any] = {
            "mode": "table",
            "version": COUNT_VERIFY_VERSION,
            "inner_answer": first.text,
        }
        text = first.text
        if table is None:
            meta["fallback"] = "no_table"
        else:
            meta["table"] = table.as_meta()
            if table.usable:
                from .evidence_table import render_table_answer

                self.replaced += 1
                text = render_table_answer(table)
            else:
                self.fallbacks += 1
                meta["fallback"] = table.status
        return ReaderAnswer(
            text=text,
            prompt_tokens=first.prompt_tokens + p_tok,
            completion_tokens=first.completion_tokens + c_tok,
            latency_ms=first.latency_ms,
            model_calls=first.model_calls + calls,
            truncated=first.truncated,
            cached_prompt_tokens=first.cached_prompt_tokens,
            finish_reason=first.finish_reason,
            raw_text=first.raw_text,
            prompt_variant=first.prompt_variant,
            extra_meta={**dict(first.extra_meta), "count_verify": meta},
        )
