"""``derive.ItemValidator`` and ``derive.RepairAsk``: per-item validation and the one repair re-ask (W2-06a).

W-14 (C) and P-05 (D): validate every item the model returned on its own, keep the valid ones at once, ask
ONCE for the invalid ones (the invalid items and their validator errors only; no new items allowed), and send
what is still invalid to the :class:`~memspine.derive.failure_ledger.FailureLedger`. A bad item never costs
its siblings. At most one repair call per extraction call; it goes through the LLM router the caller hands in,
so the cost ledger counts it as a call like any other.

The validator is :func:`memspine.derive.compile.compile_items` (LIB-05) seen item by item: it checks that the
item parses as ``ItemV1``, that at least one cited line exists in the window and that a non-CONTEXT line is
cited, that no reserved system name is present, and that the kind is accepted by the registry (the cap).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from memspine.derive.compile import (
    CompileContext,
    ParseFailure,
    Rejection,
    RejectionCode,
    parse_output,
)
from memspine.derive.envelope_compiler import EnvelopeCompiler
from memspine.derive.failure_ledger import FailureLedger
from memspine.exceptions import LLMError
from memspine.observability import tap
from memspine.prompts.structured import RepairItem, repair_messages
from memspine.schemas.durable import AssertionEnvelope
from memspine.services.llm.base import LLMService, lenient_json

__all__ = [
    "InvalidItem",
    "ItemValidator",
    "ItemsOutcome",
    "RepairAsk",
    "parse_reply",
    "process_items",
]

#: A duplicate is not a failure of the model's output: it is dropped, never repaired or dead-lettered.
_NOT_REPAIRABLE = frozenset({RejectionCode.DUPLICATE})

_HINTS: dict[RejectionCode, str] = {
    RejectionCode.UNCITED: "cite at least one numbered line in `lines`",
    RejectionCode.CITED_TURN_NOT_IN_WINDOW: "cite only line numbers that exist in the lines above",
    RejectionCode.CONTEXT_ONLY: "cite at least one NEW line, not only context lines",
    RejectionCode.RESERVED_FIELD: "remove the system field; the model never sets ids, trust, scope or status",
    RejectionCode.INVALID_FIELD: "fix the field type or value",
    RejectionCode.EMPTY_STATEMENT: "`text` must be a non-empty statement",
    RejectionCode.KIND_CAPPED: "use a more common kind",
    RejectionCode.INVALID_ENVELOPE: "the item cannot be stored as written",
}


@dataclass(frozen=True)
class InvalidItem:
    index: int
    item: Any
    rejection: Rejection

    @property
    def repairable(self) -> bool:
        return self.rejection.code not in _NOT_REPAIRABLE

    def errors(self) -> tuple[str, ...]:
        """Typed errors for the re-ask: code, hint, field path. Never the item text."""
        rej = self.rejection
        parts = [str(rej.code.value), _HINTS.get(rej.code, "")]
        if rej.paths:
            parts.append("at " + ", ".join(rej.paths[:4]))
        if rej.code is RejectionCode.INVALID_FIELD and rej.detail:
            parts.append(rej.detail[:80])
        if rej.code is RejectionCode.CITED_TURN_NOT_IN_WINDOW and rej.detail:
            parts.append(rej.detail[:80])
        return (": ".join(p for p in (parts[0], "; ".join(x for x in parts[1:] if x)) if p),)


@dataclass(frozen=True)
class Validated:
    envelopes: tuple[AssertionEnvelope, ...] = ()
    invalid: tuple[InvalidItem, ...] = ()
    duplicates: int = 0
    parse_error: ParseFailure | None = None
    item_count: int = 0
    notes: tuple[str, ...] = ()


class ItemValidator:
    """Validate and compile a list of raw items in one pass; each item stands or falls alone."""

    def __init__(self, compiler: EnvelopeCompiler) -> None:
        self.compiler = compiler

    def validate(self, raw: str | Sequence[Any], ctx: CompileContext) -> Validated:
        items, failure = parse_output(raw)
        if failure is not None or items is None:
            return Validated(parse_error=failure)
        result = self.compiler.compile(items, ctx)
        invalid: list[InvalidItem] = []
        dup = 0
        for rej in result.rejections:
            if rej.code is RejectionCode.DUPLICATE:
                dup += 1
                continue
            invalid.append(InvalidItem(rej.index, items[rej.index], rej))
        return Validated(
            envelopes=result.envelopes,
            invalid=tuple(invalid),
            duplicates=dup,
            item_count=len(items),
            notes=tuple(f"{n.code}" for n in result.notes),
        )


class RepairAsk:
    """The one targeted re-ask. Returns one corrected item per invalid item, by position, or None."""

    def __init__(self, llm: LLMService, **chat_options: Any) -> None:
        self._llm = llm
        self._options = {"temperature": 0.0, **chat_options}
        self.calls = 0
        self.failed = 0
        self.extra_dropped = 0

    async def ask(
        self, base: Sequence[dict[str, str]], invalid: Sequence[InvalidItem]
    ) -> list[Any] | None:
        messages = repair_messages(
            base, [RepairItem(i, entry.item, entry.errors()) for i, entry in enumerate(invalid)]
        )
        self.calls += 1
        try:
            reply = await self._llm.chat(messages, **self._options)
        except LLMError:
            self.failed += 1
            return None
        parsed = parse_reply(reply)
        if parsed is None:
            self.failed += 1
            return None
        if len(parsed) > len(invalid):  # no new items in a repair
            self.extra_dropped += len(parsed) - len(invalid)
            parsed = parsed[: len(invalid)]
        return parsed


def parse_reply(reply: str) -> list[Any] | None:
    text = reply.strip()
    fence = re.match(r"^```[a-zA-Z0-9_-]*\s*\n(.*?)\n?```\s*$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        data: Any = json.loads(text)
    except ValueError:
        # A truncated or slightly broken list is rescued; garbage is not read as "nothing to report".
        try:
            data = lenient_json(text)
        except Exception:
            return None
        if not data:
            return None
    if isinstance(data, dict):
        data = data.get("items", [data] if "text" in data else None)
    return list(data) if isinstance(data, list) else None


@dataclass
class ItemsOutcome:
    envelopes: list[AssertionEnvelope] = field(default_factory=list)
    returned: int = 0
    valid: int = 0  # valid on the first pass
    repaired: int = 0  # valid only after the re-ask
    dead: int = 0
    duplicates: int = 0
    repair_calls: int = 0
    dead_by_code: Counter[str] = field(default_factory=Counter)
    notes: Counter[str] = field(default_factory=Counter)
    parse_error: ParseFailure | None = None


async def process_items(
    raw: str | Sequence[Any],
    ctx: CompileContext,
    *,
    validator: ItemValidator,
    repair: RepairAsk | None,
    ledger: FailureLedger | None,
    base_messages: Sequence[dict[str, str]],
    ns: str,
    window_id: str,
) -> ItemsOutcome:
    """Validate, repair once, dead-letter the rest. Siblings of a bad item are always kept."""
    first = validator.validate(raw, ctx)
    out = ItemsOutcome(parse_error=first.parse_error)
    if first.parse_error is not None:
        return out
    out.returned = first.item_count
    out.envelopes.extend(first.envelopes)
    out.valid = len(first.envelopes)
    out.duplicates = first.duplicates
    out.notes.update(first.notes)
    for env in first.envelopes:
        tap.event("write.item.valid", id=env.occurrence_id, window=window_id, kind=env.kind)
    seen = {(e.write_id, e.occurrence_id) for e in first.envelopes}

    todo = [x for x in first.invalid if x.repairable]
    dead: list[tuple[InvalidItem, str, int]] = [
        (x, x.rejection.code.value, 1) for x in first.invalid if not x.repairable
    ]
    if todo and repair is not None:
        out.repair_calls = 1
        fixed = await repair.ask(base_messages, todo)
        if fixed is None:
            dead.extend((x, "repair_call_failed", 2) for x in todo)
        else:
            _settle(todo, fixed, validator, ctx, seen, out, dead, window_id)
    else:
        dead.extend((x, x.rejection.code.value, 1) for x in todo)

    for item, code, attempts in dead:
        out.dead += 1
        out.dead_by_code[code] += 1
        tap.event(
            "write.item.dead",
            reason=code,
            index=item.index,
            window=window_id,
            attempts=attempts,
            first_reason=item.rejection.code.value,
        )
        if ledger is not None:
            ledger.record(
                ns,
                window_id,
                item.index,
                code,
                detail=item.rejection.code.value,
                attempts=attempts,
                retry_allowed=False,
                item=item.item,
            )
    return out


def _settle(
    todo: list[InvalidItem],
    fixed: list[Any],
    validator: ItemValidator,
    ctx: CompileContext,
    seen: set[tuple[str, str]],
    out: ItemsOutcome,
    dead: list[tuple[InvalidItem, str, int]],
    window_id: str,
) -> None:
    """Validate the repair reply item by item and map each result back by position."""
    for pos, original in enumerate(todo):
        if pos >= len(fixed):  # the model returned fewer items than asked
            dead.append((original, "repair_missing", 2))
            continue
        again = validator.validate([fixed[pos]], ctx)
        if again.invalid:
            dead.append((original, again.invalid[0].rejection.code.value, 2))
            continue
        if not again.envelopes:  # parse failure of a single element cannot happen; stay safe
            dead.append((original, "repair_missing", 2))
            continue
        env = again.envelopes[0]
        key = (env.write_id, env.occurrence_id)
        if key in seen:
            out.duplicates += 1
            continue
        seen.add(key)
        out.envelopes.append(env)
        out.repaired += 1
        tap.event(
            "write.item.repaired",
            id=env.occurrence_id,
            index=original.index,
            window=window_id,
            was=original.rejection.code.value,
        )
