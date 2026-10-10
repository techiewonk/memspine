"""A04 + A06: a provenance-linked evidence table for list and count questions
(``--evidence-table``).

A 9B reader asked "how many ..." or "which ..." (many) estimates, drops items, adds items the
memories do not hold, or counts one event twice. This module is the shared piece that
``count_verify`` (I56) and ``slot_verify`` (E06) consume. It asks ONE bounded structured model
call for *rows*, never for the answer or a number, and does the rest in code:

1. **Extract (one call, I69 repair / retry).** The model reads the numbered context lines
   (``L1``..) and returns one row per candidate item: the item text, the source line id, the
   *exact span* of that line that supports it, the actor, whether the row satisfies the
   question's predicate, its status (``done`` | ``planned`` | ``mentioned``), an ``event_key``
   (one label per real-world item/event) and, optionally, ``same_as`` (the id of an earlier row
   it says it repeats).
2. **Validate (code).** A row is kept only if its line exists and its span is on that line.
   Everything else is dropped with a reason (``meta["dropped"]``). Date = the date of the line
   (an absolute date in the span wins).
3. **Filter (code).** Only rows that satisfy the actor (the question's subjects), the predicate
   flag, the status (``done``; ``planned`` only when the question speaks of plans; ``mentioned``
   never) and the time scope (an explicit year / month in the question) are counted. Each dropped
   row says why, so every count is inspectable.
4. **Dedupe (code, by identity).** Two rows are one item when they have the same actor and the
   same normalised item text (case, punctuation, articles, plural folded) AND the identity
   evidence agrees: for a thing ("how many pets") equal text is enough; for an event ("how many
   times / how often", or rows whose ``event_key`` marks an event) equal text on two different
   dates is two events, unless the later line itself says it is the same one (a coreference cue
   such as "the same", "as I mentioned", or the earlier row's date) and the model's ``same_as``
   points at it. Same-day token overlap never merges: different items on one day stay apart.
5. **Reconcile (code).** Explicit cumulative counts in the lines ("that's my third trip",
   "five so far", "a total of 4") that share a content word with the question are compared with
   the table: an exact total that equals the count agrees; a larger total or an ordinal lower
   bound turns the count into a range (the table is incomplete); a total smaller than the
   table, or two totals that disagree, is ``unresolved``.

The model never counts and never states a date. Fail closed: a failed call, no valid row or an
unresolved conflict leaves the reader's answer untouched. Generic: no dataset word, no
category, no gold (rule I37). Off (the default) no wrapper is built and rows are byte-identical.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .tokens import HeuristicTokenCounter

__all__ = [
    "EVIDENCE_TABLE_VERSION",
    "EvidenceTable",
    "EvidenceTableBuilder",
    "Row",
    "RowOut",
    "TableOut",
    "build_table",
    "cumulative_statements",
    "extract_rows",
    "needs_table",
    "numbered_lines",
    "render_table_answer",
]

EVIDENCE_TABLE_VERSION = "v1"
_MAX_LINES = 80
_MAX_LINE_CHARS = 400

# ---- context lines ---------------------------------------------------------------------------

_PREFIX = re.compile(r"^\s*(?:[*-]\s+|\[hit \d+\]\s*)?")
_DATE = re.compile(r"^\[(\d{4}-\d{2}-\d{2})(?: [A-Za-z]{3})?\]\s*")
_ANNOTATION = re.compile(r"\s*\[=[^\]]*\]")
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


@dataclass(frozen=True, slots=True)
class Line:
    n: int
    day: date | None
    text: str


def numbered_lines(context: str) -> list[Line]:
    """The context lines the prompt shows (and the validator indexes): non-empty memory lines,
    at most ``_MAX_LINES``; an optional leading ``[YYYY-MM-DD]`` becomes the line's date;
    resolver annotations are removed; text is clipped to ``_MAX_LINE_CHARS``."""
    out: list[Line] = []
    for raw in context.splitlines():
        body = _PREFIX.sub("", raw)
        if not body.strip() or body.lstrip().startswith("[Note:"):
            continue
        day: date | None = None
        m = _DATE.match(body)
        if m:
            day = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            body = body[m.end() :]
        body = _ANNOTATION.sub("", body).strip()[:_MAX_LINE_CHARS]
        if body:
            out.append(Line(len(out) + 1, day, body))
        if len(out) >= _MAX_LINES:
            break
    return out


def render_lines(lines: Sequence[Line]) -> str:
    return "\n".join(
        f"L{ln.n} [{ln.day.isoformat() if ln.day else 'undated'}] {ln.text}" for ln in lines
    )


# ---- the question gate -----------------------------------------------------------------------


def needs_table(question: str) -> bool:
    """A list or count question: the contract's cardinality is ``many`` or ``count`` (code only;
    any other question costs no call)."""
    from memspine.core.query_contract import build_contract
    from memspine.core.query_shape import is_count

    return build_contract(question).cardinality in ("many", "count") or is_count(question)


# ---- the model's output ----------------------------------------------------------------------


class RowOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    line: int
    item: str = ""
    actor: str = ""
    predicate_match: bool = True
    status: str = "unknown"
    span: str = ""
    event_key: str = ""
    same_as: str = ""

    @field_validator("line", mode="before")
    @classmethod
    def _line_no(cls, v: Any) -> Any:
        if isinstance(v, str):
            m = re.search(r"\d+", v)
            return int(m.group(0)) if m else v
        return v

    @field_validator("id", "same_as", mode="before")
    @classmethod
    def _id(cls, v: Any) -> str:
        return "" if v is None else str(v).strip()

    @field_validator("status", mode="before")
    @classmethod
    def _status(cls, v: Any) -> str:
        return str(v or "unknown").strip().lower()

    @field_validator("predicate_match", mode="before")
    @classmethod
    def _bool(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip().lower() in ("true", "yes", "1")
        return v


class TableOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rows: list[RowOut] = Field(default_factory=list)


_SYSTEM = (
    "You build an evidence table from numbered conversation lines. You never count, never "
    "compute a date and never answer the question. Reply with one JSON object only."
)

_BODY = """\
Question: {{ question }}

Lines (each starts with its id, then the date the line was written):
{{ context }}

List every candidate item the question asks about, one row per mention. Rules:
- "line": the id of the line the item comes from (for example "L3"). Use only ids shown above.
- "span": the exact words copied from that line that state the item. Copy characters exactly.
- "item": a short name for the item or event (who, what).
- "actor": who the item belongs to or who did it, as written in the line.
- "predicate_match": true only if the line states what the question asks about that actor
  (not a different person, not a related but different action).
- "status": "done" if it happened or is true (include states such as owning or living there),
  "planned" if it is intended, scheduled or hoped for and has not happened, "mentioned" if it is
  only discussed, hypothetical, hearsay or unclear.
- "event_key": one short lowercase label per real-world item or event, THE SAME label for every
  mention of it. Different occurrences of a repeated activity (two trips, two visits) get
  different labels.
- "same_as": the id of an earlier row if this line says it is the very same item or event as
  that row, else "".
- Include every mention as its own row, even repeats. Do not invent items the lines do not state.

JSON shape:
{"rows": [{"id": "r1", "line": "L3", "item": "...", "actor": "...", "predicate_match": true,
           "status": "done", "span": "...", "event_key": "...", "same_as": ""}]}
"""


def _prompt() -> Any:
    from memspine.prompts.base import Prompt, PromptFormat

    return Prompt(
        id="evidence_table",
        version=1,
        role="evidence_table",
        format=PromptFormat.JSON,
        system=_SYSTEM,
        body=_BODY,
    )


Chat = Callable[..., Awaitable[str]]


async def extract_rows(chat: Chat, question: str, lines: Sequence[Line]) -> TableOut:
    """ONE structured call (``structured_call``: format-aware parse, JSON repair, opt-in retry)."""
    from memspine.services.llm.structured import structured_call

    class _ChatLLM:
        provider_id = "harness:evidence_table"

        async def chat(self, messages: list[dict[str, str]], **_: Any) -> str:
            system = next((m["content"] for m in messages if m["role"] == "system"), None)
            user = "\n\n".join(m["content"] for m in messages if m["role"] != "system")
            return await chat(user, system=system)

    return await structured_call(
        _ChatLLM(),  # type: ignore[arg-type]
        _prompt(),
        {"question": question, "context": render_lines(lines)},
        TableOut,
    )


# ---- normalisation ---------------------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    ["the", "a", "an", "his", "her", "their", "my", "our", "its", "of", "to", "in", "on", "at", "and", "or", "for", "with", "by", "from", "as", "is", "was", "were", "that", "this"]
)
_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS.sub(" ", text).strip().lower()


def _content(text: str) -> frozenset[str]:
    out = set()
    for w in _WORD.findall(text.lower()):
        if w in _STOP:
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return frozenset(out)


def _actor_key(actor: str) -> str:
    return " ".join(sorted(_content(actor)))


def _in_line(span: str, line: str) -> bool:
    return bool(span.strip()) and _norm(span) in _norm(line)


# ---- rows ------------------------------------------------------------------------------------

_EVENT_Q = re.compile(r"\bhow (?:many times|often)\b", re.I)
_PLAN_WORDS = re.compile(
    r"\b(?:plan(?:s|ned|ning)?|schedul\w*|expect\w*|upcoming|will|going to|due|intend\w*|"
    r"book(?:ed|ing)|appointment)\b",
    re.I,
)
#: a later line that points back at an earlier one: only then does a different day not split.
_COREF = re.compile(
    r"\b(?:the same|same (?:one|event|trip|visit|thing)|as (?:i|we) (?:said|mentioned|told)|"
    r"mentioned (?:earlier|before)|earlier|previously|that (?:one|time))\b",
    re.I,
)
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_MONTH_YEAR = re.compile(
    rf"\b({'|'.join(_MONTHS)})\b(?:\s+\d{{1,2}}(?:st|nd|rd|th)?)?,?\s*((?:19|20)\d{{2}})?", re.I
)


@dataclass(slots=True)
class Row:
    id: str
    line: int
    item: str
    actor: str
    status: str
    span: str
    event_key: str
    day: date | None
    predicate_match: bool
    same_as: str = ""
    line_text: str = ""
    counted: bool = False
    reason: str = ""  # why it is not counted ("" = counted or merged, see merged_into)
    merged_into: str = ""

    def as_meta(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "line": f"L{self.line}",
            "item": self.item,
            "actor": self.actor,
            "status": self.status,
            "date": self.day.isoformat() if self.day else "",
            "predicate_match": self.predicate_match,
            "counted": self.counted,
            "reason": self.reason,
            "merged_into": self.merged_into,
        }


@dataclass(slots=True)
class EvidenceTable:
    #: ``resolved`` (low == high), ``range`` (low < high, the lines state a larger total),
    #: ``unresolved`` (conflicting totals), ``empty`` (no countable row)
    status: str
    low: int
    high: int
    items: list[Row] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)
    cumulative: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""

    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def usable(self) -> bool:
        return self.status in ("resolved", "range") and bool(self.items)

    def item_names(self) -> list[str]:
        return [r.item for r in self.items]

    def evidence_keys(self) -> list[str]:
        """One short string per distinct item for an answer-completeness check: the item's words
        minus the words most items share ("trip to Lisbon", "day trip to Sintra" -> "lisbon",
        "day sintra"), so an answer that names the place is not flagged for omitting "trip".
        Items with identical keys (two events with the same name) count once."""
        sets = [_content(r.item) for r in self.items]
        n = len(sets)
        if n < 2:
            return [" ".join(sorted(s)) for s in sets if s]
        df: dict[str, int] = {}
        for s in sets:
            for w in s:
                df[w] = df.get(w, 0) + 1
        keys: list[str] = []
        for s in sets:
            own = {w for w in s if df[w] * 2 <= n} or s
            key = " ".join(sorted(own))
            if key and key not in keys:
                keys.append(key)
        return keys

    def as_meta(self) -> dict[str, Any]:
        return {
            "version": EVIDENCE_TABLE_VERSION,
            "status": self.status,
            "low": self.low,
            "high": self.high,
            "n_rows": len(self.rows),
            "n_items": len(self.items),
            "note": self.note,
            "cumulative": self.cumulative,
            "rows": [r.as_meta() for r in self.rows],
        }


def _row_day(row: RowOut, line: Line) -> date | None:
    """The row's date: an absolute ISO date in its span, else the line's own date."""
    m = _ISO.search(row.span)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return line.day


def _subject_match(actor: str, subjects: Sequence[str]) -> bool:
    a = _content(actor)
    if not a:
        return False
    return any(a & _content(s) for s in subjects if _content(s))


def _time_scope(question: str) -> tuple[int | None, int | None]:
    """(year, month) the question names explicitly; None when it names none."""
    year = None
    ym = _YEAR.search(question)
    if ym:
        year = int(ym.group(0))
    month = None
    mm = _MONTH_YEAR.search(question)
    if mm and mm.group(1):
        month = _MONTHS.index(mm.group(1).lower()) + 1
        if mm.group(2):
            year = int(mm.group(2))
    return year, month


def _same_identity(a: Row, b: Row, events: bool) -> bool:
    """``b`` (later) repeats ``a`` (kept): same actor, same normalised text, identity evidence."""
    if _actor_key(a.actor) != _actor_key(b.actor):
        return False
    ca, cb = _content(a.item), _content(b.item)
    if not ca or ca != cb:
        return False
    if not events:
        return True  # a thing: equal text is one thing, whatever the day
    if a.day == b.day:
        return True
    # an event on two different days: two events, unless the later line says it is the same one
    ref_date = a.day.isoformat() if a.day else None
    says_same = bool(_COREF.search(b.line_text)) or bool(ref_date and ref_date in b.line_text)
    return says_same and b.same_as == a.id


def _is_events(question: str, rows: Sequence[Row]) -> bool:
    return bool(_EVENT_Q.search(question))


# ---- cumulative statements -------------------------------------------------------------------

_NUMWORDS = {
    w: i
    for i, w in enumerate(
        ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"]
    )
}
_ORD = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
    "eighth": 8, "ninth": 9, "tenth": 10,
}  # fmt: skip
_NUM = rf"(\d+|{'|'.join(_NUMWORDS)})"
_ORDRE = rf"(\d+(?:st|nd|rd|th)|{'|'.join(_ORD)})"
_TOTAL_AFTER = re.compile(
    rf"\b(?:so far|in total|altogether|a total of|total of|in all|overall)\b[^.\d]{{0,12}}{_NUM}\b",
    re.I,
)
_TOTAL_BEFORE = re.compile(
    rf"\b{_NUM}\b(?:\s+\w+){{0,3}}\s+(?:so far|in total|altogether|in all|overall)\b", re.I
)
_ORDINAL_TIME = re.compile(
    rf"\b(?:for|on|by) the {_ORDRE} time\b|\b(?:my|our|his|her|their) {_ORDRE} (?!of\b)\w+", re.I
)


def _to_int(tok: str) -> int | None:
    tok = tok.lower()
    if tok.isdigit():
        return int(tok)
    if tok in _NUMWORDS:
        return _NUMWORDS[tok]
    if tok in _ORD:
        return _ORD[tok]
    m = re.match(r"(\d+)(?:st|nd|rd|th)$", tok)
    return int(m.group(1)) if m else None


def cumulative_statements(question: str, lines: Sequence[Line]) -> list[dict[str, Any]]:
    """Explicit running totals in the lines that share a content word with the question.
    ``exact``: "five so far", "a total of 4"; ``lower``: "my third trip", "the 4th time"."""
    qwords = {w for w in _content(question) if len(w) > 3}
    out: list[dict[str, Any]] = []
    for ln in lines:
        if not (qwords & _content(ln.text)):
            continue
        for rx, kind in (
            (_TOTAL_AFTER, "exact"),
            (_TOTAL_BEFORE, "exact"),
            (_ORDINAL_TIME, "lower"),
        ):
            for m in rx.finditer(ln.text):
                tok = next((g for g in m.groups() if g), "")
                n = _to_int(tok)
                if n is not None and 0 < n <= 99:
                    out.append(
                        {
                            "line": f"L{ln.n}",
                            "date": ln.day.isoformat() if ln.day else "",
                            "n": n,
                            "kind": kind,
                            "text": m.group(0),
                        }
                    )
    return out


def _reconcile(count: int, stmts: Sequence[dict[str, Any]]) -> tuple[str, int, int, str]:
    """``(status, low, high, note)`` of the table count against the cumulative statements."""
    exact = [s for s in stmts if s["kind"] == "exact"]
    lower = [s for s in stmts if s["kind"] == "lower"]
    totals = sorted({s["n"] for s in exact})
    if len(totals) > 1:
        dated = sorted((s for s in exact if s["date"]), key=lambda s: s["date"])
        monotone = all(a["n"] <= b["n"] for a, b in zip(dated, dated[1:], strict=False))
        if len(dated) != len(exact) or not monotone:
            return "unresolved", count, count, f"stated totals disagree: {totals}"
        totals = [dated[-1]["n"]]  # a total that grew over time: the latest one stands
    floor = max([count] + [s["n"] for s in lower])
    if totals:
        total = totals[0]
        if total < count:
            return "unresolved", count, count, f"stated total {total} is below the table's {count}"
        if total > count:
            return "range", count, total, f"stated total {total} exceeds the table's {count}"
        return "resolved", count, count, ""
    if floor > count:
        return "range", floor, floor, f"an ordinal states at least {floor}"
    return "resolved", count, count, ""


# ---- build the table (code) ------------------------------------------------------------------


def build_table(
    question: str,
    lines: Sequence[Line],
    out: TableOut,
    *,
    subjects: Sequence[str] | None = None,
    plans_ok: bool | None = None,
) -> EvidenceTable:
    """Validate, filter, dedupe and reconcile the model's rows. Pure code."""
    if subjects is None:
        from memspine.core.query_contract import build_contract

        subjects = build_contract(question).subjects
    if plans_ok is None:
        plans_ok = bool(_PLAN_WORDS.search(question))
    year, month = _time_scope(question)
    rows: list[Row] = []
    seen_ids: set[str] = set()
    for ro in out.rows:
        r = Row(
            id=ro.id or f"r{len(rows) + 1}", line=ro.line, item=" ".join(ro.item.split()),
            actor=ro.actor.strip(), status=ro.status, span=ro.span, event_key=ro.event_key,
            day=None, predicate_match=ro.predicate_match, same_as=ro.same_as,
        )  # fmt: skip
        rows.append(r)
        if r.id in seen_ids:
            r.reason = "duplicate_row_id"
            continue
        seen_ids.add(r.id)
        if not 1 <= ro.line <= len(lines):
            r.reason = "line_not_in_context"
            continue
        ln = lines[ro.line - 1]
        r.line_text = ln.text
        if not _in_line(ro.span, ln.text):
            r.reason = "span_not_on_line"
            continue
        if not r.item:
            r.reason = "no_item"
            continue
        r.day = _row_day(ro, ln)
        if not r.predicate_match:
            r.reason = "predicate_mismatch"
        elif subjects and not _subject_match(r.actor, subjects):
            r.reason = "actor_mismatch"
        elif r.status == "planned" and not plans_ok:
            r.reason = "planned_not_done"
        elif r.status not in ("done", "planned"):
            r.reason = f"status_{r.status or 'unknown'}"
        elif (year or month) and r.day is None:
            r.reason = "time_scope_undated"
        elif (year and r.day and r.day.year != year) or (month and r.day and r.day.month != month):
            r.reason = "outside_time_scope"
        else:
            r.counted = True

    events = _is_events(question, rows)
    kept: list[Row] = []
    for r in rows:
        if not r.counted:
            continue
        hit = next((k for k in kept if _same_identity(k, r, events)), None)
        if hit is None:
            kept.append(r)
        else:
            r.counted = False
            r.merged_into = hit.id
            r.reason = "same_item"
    stmts = cumulative_statements(question, lines)
    if not kept:
        return EvidenceTable("empty", 0, 0, [], rows, stmts)
    status, low, high, note = _reconcile(len(kept), stmts)
    return EvidenceTable(status, low, high, kept, rows, stmts, note)


def render_table_answer(table: EvidenceTable) -> str:
    """``3: A (2023-05-01); B``; a range reads ``at least 3 (the memories state 5): A; B; C``."""
    parts = [f"{r.item} ({r.day.isoformat()})" if r.day else r.item for r in table.items]
    body = "; ".join(parts)
    if table.status == "range":
        if table.high != table.low:
            return f"at least {table.low}, up to {table.high} (the memories state a total of {table.high}): {body}"
        return f"at least {table.low} (the memories state at least that many): {body}"
    return f"{table.count}: {body}"


# ---- the shared builder (one call per question, memoised) ------------------------------------


class EvidenceTableBuilder:
    """Builds the table for a list/count question with one model call and memoises it by
    (question, context), so ``count_verify`` and ``slot_verify`` on the same row share one call.
    ``build`` returns ``(table | None, prompt_tokens, completion_tokens, calls)``; a memo hit
    costs 0."""

    def __init__(self, chat: Chat) -> None:
        self._chat = chat
        self._memo: dict[str, EvidenceTable | None] = {}
        self._counter = HeuristicTokenCounter()
        self.calls = 0
        self.failures = 0
        self.seen = 0
        # I69: repair + one retry of a malformed extraction (process-wide, opt-in switch).
        from memspine.services.llm import structured

        if not structured._OPTIONS.retry_on_error:
            structured.configure(
                structured.StructuredOptions(
                    retry_on_error=True, constrained_retry=structured._OPTIONS.constrained_retry
                )
            )

    def describe(self) -> Mapping[str, Any]:
        return {"evidence_table": EVIDENCE_TABLE_VERSION}

    async def build(
        self, question: str, context: str
    ) -> tuple[EvidenceTable | None, int, int, int]:
        key = hashlib.sha1(f"{question}\x00{context}".encode()).hexdigest()
        if key in self._memo:
            return self._memo[key], 0, 0, 0
        if not needs_table(question):
            return None, 0, 0, 0
        lines = numbered_lines(context)
        if not lines:
            self._memo[key] = None
            return None, 0, 0, 0
        self.seen += 1
        prompt_tokens = (
            self._counter.count(render_lines(lines)) + self._counter.count(question) + 600
        )
        self.calls += 1
        try:
            out = await extract_rows(self._chat, question, lines)
            table: EvidenceTable | None = build_table(question, lines, out)
        except Exception as exc:  # an enhancer: the reader's answer stands
            self.failures += 1
            table = EvidenceTable("empty", 0, 0, note=f"failed: {str(exc)[:160]}")
            self._memo[key] = table
            return table, prompt_tokens, 0, 1
        completion = self._counter.count(out.model_dump_json())
        self._memo[key] = table
        return table, prompt_tokens, completion, 1
