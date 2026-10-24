"""Compile the model's extraction output (``ExtractionItem``) into stored ``AssertionEnvelope`` objects (LIB-05).

Pure and unwired (wired later by W2-06a). Imports only the stdlib, pydantic and ``memspine.schemas``.

What the rules come from:

* S2-1: the model authors content only, with at least one cited turn. Every system field (ids, scope, trust,
  provenance class, epoch, erasure generation, recorded time, status, verified) comes from
  :class:`CompileContext`, never from the item.
* S2-2: an invalid item is dropped with a typed :class:`Rejection`; its siblings are kept. Only output that
  cannot be parsed at all sets :attr:`CompileResult.parse_error` (the caller then spends the ER-15 recovery
  budget).
* S2-3 and its conditions: any reserved name anywhere in an item makes the item invalid; other unknown fields
  go to ``extras``; the kind set is open and is normalised (and possibly capped) by a registry callback.
* S3-2 / S3-3: ``occurrence_id`` and ``write_id`` come from the schema helpers.
* S4-1 / ER-11: lines cited from the NEW part of the window are ``support`` evidence; cited CONTEXT lines are
  ``binding`` evidence and never count as support.
* S5-1: ``source_trust`` is system-set from the cited line's role; the model's confidence is a hint only.
* I-3: ``chg`` marks a change; it sets ``AssertionEnvelope.change_hint``.

Interpretations the owner may veto are listed in the docstrings of the functions that make them (modality
mapping, binding, assertion key, support, source label).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ValidationError

from memspine.schemas.durable import (
    AssertionEnvelope,
    AssertionKey,
    Assessment,
    Evidence,
    Interval,
    Mention,
    OwnerScope,
    Provenance,
    Span,
    Support,
    TemporalRef,
    TimeAxes,
    TurnRef,
    occurrence_id,
    write_id,
)
from memspine.schemas.durable.assessment import Binding, Modality
from memspine.schemas.durable.common import SrcClass
from memspine.schemas.reserved import reserved_hits
from memspine.schemas.steps.shared import ExtractionItem

__all__ = [
    "CHANGE_KEY",
    "DEFAULT_ROLE_POLICY",
    "FALLBACK_KIND",
    "CompileContext",
    "CompileNote",
    "CompileResult",
    "ParseFailure",
    "Rejection",
    "RejectionCode",
    "RolePolicy",
    "TimeAnchor",
    "WindowLine",
    "compile_items",
    "gliner_annotation_hits",
    "keep_text_only",
    "map_modality",
    "normalise_kind",
    "parse_output",
]

CHANGE_KEY = "chg"
"""Wire key of the I-3 change marker; it maps to ``AssertionEnvelope.change_hint``."""

SOURCE_HINT_EXTRA_KEY = "source_hint"
SPAN_TEXT_EXTRA_KEY = "span_text"
FALLBACK_KIND = "other"

_CONFIDENCE_KEYS = ("conf", "confidence")


# ---------------------------------------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------------------------------------
class RejectionCode(StrEnum):
    RESERVED_FIELD = "reserved_field"
    UNCITED = "uncited"
    CITED_TURN_NOT_IN_WINDOW = "cited_turn_not_in_window"
    CONTEXT_ONLY = "context_only"
    INVALID_FIELD = "invalid_field"
    EMPTY_STATEMENT = "empty_statement"
    KIND_CAPPED = "kind_capped"
    DUPLICATE = "duplicate"
    INVALID_ENVELOPE = "invalid_envelope"


@dataclass(frozen=True)
class Rejection:
    """One dropped item: its index in the output list, a typed reason and a short detail (never item text)."""

    index: int
    code: RejectionCode
    detail: str = ""
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompileNote:
    """A non-fatal observation about a kept item (e.g. the kind fell back to ``other``)."""

    index: int
    code: str
    detail: str = ""


@dataclass(frozen=True)
class ParseFailure:
    """The whole output was unparsable (S2-2). ``code`` is ``empty``, ``not_json`` or ``bad_structure``."""

    code: str
    detail: str = ""


@dataclass(frozen=True)
class CompileResult:
    envelopes: tuple[AssertionEnvelope, ...] = ()
    rejections: tuple[Rejection, ...] = ()
    notes: tuple[CompileNote, ...] = ()
    parse_error: ParseFailure | None = None
    item_count: int = 0

    @property
    def ok(self) -> bool:
        """True when the output parsed (individual items may still have been rejected)."""
        return self.parse_error is None


# ---------------------------------------------------------------------------------------------------------
# Context (system-authored; nothing here comes from the model)
# ---------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class WindowLine:
    """One numbered line of the rendered window, keyed by its alias number in :attr:`CompileContext.window`."""

    turn_id: str
    source_id: str
    source_version: int
    seg_index: int
    role: str
    speaker: str | None = None
    source_at: datetime | None = None
    tz: str | None = None
    content: str | None = None
    is_context: bool = False  # a CONTEXT line (ER-11): citable as binding evidence only


@dataclass(frozen=True)
class RolePolicy:
    src_class: SrcClass
    trust: float


DEFAULT_ROLE_POLICY: Mapping[str, RolePolicy] = {
    # "default" tier (S5-2 clarification): user, operator, system. "low" tier: tool, document, assistant.
    "user": RolePolicy("user_stated", 0.8),
    "operator": RolePolicy("user_stated", 0.8),
    "system": RolePolicy("external", 0.8),
    "assistant": RolePolicy("assistant_proposed", 0.4),
    "tool": RolePolicy("tool", 0.4),
    "document": RolePolicy("document", 0.4),
}
_UNKNOWN_ROLE_POLICY = RolePolicy("external", 0.4)

TimeAnchor = Callable[[str, "datetime | None", "str | None"], TemporalRef]
"""``(phrase, source_at, tz) -> TemporalRef``. Injected (LIB-01 provides the real one)."""

KindRegistry = Callable[[str], "str | None"]
"""Takes a normalised kind; returns the registered kind (``"other"`` when over the cap) or ``None`` to refuse."""


def keep_text_only(text: str, source_at: datetime | None, tz: str | None) -> TemporalRef:
    """Default anchor: keep the phrase, resolve nothing (S6-2: never invent a date)."""
    return TemporalRef(text=text, basis="none")


def normalise_kind(raw: str) -> str:
    """Lower-case, snake_case, naive singular (S2-3 condition 3). ``"Events"`` -> ``"event"``."""
    name = re.sub(r"[^0-9a-z]+", "_", raw.strip().lower()).strip("_")
    if len(name) > 3:
        if name.endswith("ies"):
            name = name[:-3] + "y"
        elif name.endswith("s") and not name.endswith(("ss", "us", "is")):
            name = name[:-1]
    return name


def _identity_registry(kind: str) -> str | None:
    return kind


@dataclass(frozen=True)
class CompileContext:
    """Everything the compiler needs that the model must not author."""

    scope: OwnerScope
    source_id: str
    window: Mapping[int, WindowLine]
    source_version: int = 1
    source_event_id: str | None = None  # S3-2 "source event id"; defaults to ``source_id``
    session_id: str | None = None
    agent_id: str | None = None
    principal: str | None = None
    source_time: datetime | None = None  # fallback when the cited line has no time of its own
    recorded_at: datetime | None = None
    role_policy: Mapping[str, RolePolicy] = field(default_factory=lambda: dict(DEFAULT_ROLE_POLICY))
    erasure_generation: int = 0
    epoch: int | None = None
    prompt_hash: str | None = None
    kind_registry: KindRegistry = _identity_registry
    time_anchor: TimeAnchor = keep_text_only


# ---------------------------------------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------------------------------------
_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n(.*?)\n\s*```\s*$", re.DOTALL)


def parse_output(raw: str | Sequence[Any]) -> tuple[list[Any] | None, ParseFailure | None]:
    """Parse the raw reply into a list of (unvalidated) items.

    Tolerated: a JSON array, a JSON object with an ``items`` array, a single item object, and a fenced
    block around any of these. Not tolerated (parse failure): anything else. YAML is not supported because
    the stdlib has no YAML parser and this module may import only the stdlib and pydantic.
    """
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None, ParseFailure("empty", "no output")
        match = _FENCE.match(text)
        if match:
            text = match.group(1).strip()
        try:
            data: Any = json.loads(text)
        except ValueError as exc:
            return None, ParseFailure("not_json", f"{type(exc).__name__}: {exc}"[:200])
    else:
        data = raw
    if isinstance(data, Mapping):
        if "items" in data and isinstance(data["items"], list):
            if set(data) - {"items"}:
                return None, ParseFailure("bad_structure", "object mixes 'items' with other keys")
            return list(data["items"]), None
        if "text" in data:
            return [data], None
        return None, ParseFailure("bad_structure", "object is neither an item nor {'items': [...]}")
    if isinstance(data, (list, tuple)):
        return list(data), None
    return None, ParseFailure("bad_structure", f"top level is {type(data).__name__}, not a list")


# ---------------------------------------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------------------------------------
def map_modality(mode: str, neg: bool, reported: bool = False) -> tuple[Modality, bool]:
    """Map the wire ``mode``, ``neg`` and reported speaker onto ``(modality, negated)``.

    A reported speaker makes the modality ``reported`` (unless the item is a question); otherwise
    ``planned`` stays ``planned``, ``hoped`` and ``hedged`` become ``hypothetical``, ``asked`` becomes
    ``question``. An asserted negation is modality ``negated`` with ``negated=False`` (the integration rules
    are unchanged); for every other modality ``neg`` sets the separate ``negated`` flag, so "won't move" is
    ``planned`` + ``negated=True``.
    """
    modality: Modality
    if mode == "asked":
        modality = "question"
    elif reported:
        modality = "reported"
    elif mode == "asserted":
        return ("negated", False) if neg else ("asserted", False)
    else:
        modality = "planned" if mode == "planned" else "hypothetical"
    return modality, neg


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _fold(value: str) -> str:
    return " ".join(value.casefold().split())


def _hash(domain: str, parts: list[Any]) -> str:
    blob = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"{domain}_{hashlib.sha256(blob.encode('utf-8')).hexdigest()[:32]}"


def _locate(quote: str, content: str | None) -> tuple[int, int] | None:
    if not quote or not content:
        return None
    pos = content.find(quote)
    if pos >= 0:
        return pos, pos + len(quote)
    low_c, low_q = content.lower(), quote.lower()
    if len(low_c) == len(content) and len(low_q) == len(quote):
        pos = low_c.find(low_q)
        if pos >= 0:
            return pos, pos + len(quote)
    return None


def _confidence_hint(extra: Mapping[str, Any]) -> tuple[float | None, str | None]:
    for key in _CONFIDENCE_KEYS:
        value = extra.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0.0 <= value <= 1.0:
            return float(value), key
    return None, None


def _time_ref(
    ctx: CompileContext,
    phrase: str | None,
    line: WindowLine,
    notes: list[CompileNote],
    index: int,
) -> TemporalRef | None:
    phrase = _clean(phrase)
    if phrase is None:
        return None
    source_at = line.source_at or ctx.source_time
    try:
        return TemporalRef.model_validate(ctx.time_anchor(phrase, source_at, line.tz))
    except Exception as exc:  # injected callable: fail soft, never invent a date (S6-2)
        notes.append(CompileNote(index, "time_anchor_failed", type(exc).__name__))
        return keep_text_only(phrase, source_at, line.tz)


# ---------------------------------------------------------------------------------------------------------
# Compile
# ---------------------------------------------------------------------------------------------------------
def compile_items(raw: str | Sequence[Any], ctx: CompileContext) -> CompileResult:
    """Compile a raw extraction reply (or an already-parsed list) into envelopes plus typed rejections.

    Pure and deterministic: the same ``raw`` and ``ctx`` give identical envelopes, ids and ordering
    (input order; a later item with the same write and occurrence id is rejected as a duplicate). Never
    raises for bad model output.
    """
    items, failure = parse_output(raw)
    if failure is not None or items is None:
        return CompileResult(parse_error=failure)

    envelopes: list[AssertionEnvelope] = []
    rejections: list[Rejection] = []
    notes: list[CompileNote] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(items):
        outcome = _compile_one(index, item, ctx, notes)
        if isinstance(outcome, Rejection):
            rejections.append(outcome)
            continue
        ident = (outcome.write_id, outcome.occurrence_id)
        if ident in seen:
            rejections.append(
                Rejection(index, RejectionCode.DUPLICATE, "same write and occurrence id")
            )
            continue
        seen.add(ident)
        envelopes.append(outcome)
    return CompileResult(
        envelopes=tuple(envelopes),
        rejections=tuple(rejections),
        notes=tuple(notes),
        item_count=len(items),
    )


def _compile_one(
    index: int, item: Any, ctx: CompileContext, notes: list[CompileNote]
) -> AssertionEnvelope | Rejection:
    if isinstance(item, BaseModel):
        item = item.model_dump()
    if not isinstance(item, Mapping):
        return Rejection(
            index, RejectionCode.INVALID_FIELD, f"item is {type(item).__name__}, not an object"
        )

    # S2-3 condition 1: a reserved name anywhere in the item invalidates it.
    hits = [*reserved_hits(item), *gliner_annotation_hits(item)]
    if hits:
        return Rejection(
            index, RejectionCode.RESERVED_FIELD, "reserved system name in item", tuple(hits)
        )

    # S2-1: at least one cited turn (checked before the schema so a missing list reads as "uncited").
    cited = item.get("lines")
    if cited is None or (isinstance(cited, (list, tuple)) and len(cited) == 0):
        return Rejection(index, RejectionCode.UNCITED, "no cited lines")

    chg = item.get(CHANGE_KEY, False)
    if not isinstance(chg, bool):
        return Rejection(index, RejectionCode.INVALID_FIELD, "chg must be a boolean")
    try:
        parsed = ExtractionItem.model_validate(dict(item), strict=True)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        return Rejection(index, RejectionCode.INVALID_FIELD, f"{where}: {first['type']}")

    text = parsed.text.strip()
    if not text:
        return Rejection(index, RejectionCode.EMPTY_STATEMENT, "statement text is blank")

    # --- citations (S4-1, ER-11) ---
    aliases = sorted(set(parsed.lines))
    missing = [a for a in aliases if a not in ctx.window]
    if missing:
        return Rejection(
            index, RejectionCode.CITED_TURN_NOT_IN_WINDOW, f"aliases not in the window: {missing}"
        )
    lines = [(a, ctx.window[a]) for a in aliases]
    support = sorted(
        (p for p in lines if not p[1].is_context), key=lambda p: (p[1].seg_index, p[0])
    )
    binding = sorted((p for p in lines if p[1].is_context), key=lambda p: (p[1].seg_index, p[0]))
    if not support:
        return Rejection(index, RejectionCode.CONTEXT_ONLY, "only CONTEXT lines were cited")

    # --- kind (S2-3, registry callback) ---
    kind = normalise_kind(parsed.kind)
    if not kind:
        return Rejection(index, RejectionCode.INVALID_FIELD, "kind is blank")
    registered = ctx.kind_registry(kind)
    if registered is None:
        return Rejection(index, RejectionCode.KIND_CAPPED, f"kind {kind!r} refused by the registry")
    registered = normalise_kind(registered) or FALLBACK_KIND
    if registered != kind:
        notes.append(CompileNote(index, "kind_capped", f"{kind!r} -> {registered!r}"))
    kind = registered

    first_line = support[0][1]
    speaker = first_line.speaker

    # --- system fields from the weakest support line (G1: never above the weakest parent) ---
    policy = min(
        (ctx.role_policy.get(p[1].role, _UNKNOWN_ROLE_POLICY) for p in support),
        key=lambda pol: pol.trust,
    )

    # --- ids (S3-2, S3-3) ---
    subject_s, rel_s, object_s = _clean(parsed.s), _clean(parsed.r), _clean(parsed.o)
    w_id = write_id(ctx.scope, ctx.session_id or ctx.source_id, first_line.seg_index, text)
    # Assertion key (interpretation): surface text stands in for entity ids until resolution (LIB-04)
    # re-keys it. No subject -> the asserter. No relation -> a text-derived relation so that two different
    # statements of the same kind never collide into a false supersede.
    key = AssertionKey(
        src=_fold(subject_s)
        if subject_s
        else (_fold(speaker) if speaker else _fold(ctx.scope.owner)),
        rel=_fold(rel_s).replace(" ", "_") if rel_s else _hash("stmt", [_fold(text)])[:17],
        dst=_fold(object_s) if object_s else "",
        kind=kind,
    )
    o_id = occurrence_id(ctx.source_event_id or ctx.source_id, [p[1].turn_id for p in support], key)

    def mention(surface: str | None, role: str) -> Mention | None:
        if surface is None:
            return None
        return Mention(surface=surface, mention_id=_hash("mn", [w_id, role, surface]))

    # --- evidence ---
    evidence: list[Evidence] = []
    quote = parsed.span.strip()
    span_placed = False
    for _alias, line in support:
        span = None
        if not span_placed:
            loc = _locate(quote, line.content)
            if loc is not None:
                span = Span(start=loc[0], end=loc[1], quote=quote)
                span_placed = True
        evidence.append(_evidence("support", line, span))
    evidence.extend(_evidence("binding", line, None) for _alias, line in binding)

    # --- time (S6): text kept, anchoring injected ---
    start = _time_ref(ctx, parsed.when, first_line, notes, index)
    end = _time_ref(ctx, parsed.until, first_line, notes, index)
    time = TimeAxes(
        event=Interval(start=start, end=end) if (start or end) else None,
        source_time=first_line.source_at or ctx.source_time,
        recorded_at=ctx.recorded_at,
    )

    # --- assessment (S5-1) ---
    reported_speaker = _clean(parsed.reported_speaker)
    modality, negated = map_modality(parsed.mode, parsed.neg, reported_speaker is not None)
    extra = parsed.model_extra or {}
    hint, hint_key = _confidence_hint(extra)
    families = sorted({e.source_family for e in evidence if e.role == "support"})
    assessment = Assessment(
        source_trust=policy.trust,
        # Interpretation: this one occurrence is backed by the distinct families of its support lines.
        support=Support(independent=len(families), restated=0, families=families),
        binding=_binding(subject_s, speaker),
        time_precision=time.event_precision,  # type: ignore[arg-type]
        modality=modality,
        negated=negated,
        model_confidence_hint=hint,
    )

    # --- extras: unknown non-reserved fields, the change marker and two audit hints ---
    extras: dict[str, Any] = {k: v for k, v in extra.items() if k not in (CHANGE_KEY, hint_key)}
    if parsed.source is not None:
        extras[SOURCE_HINT_EXTRA_KEY] = parsed.source  # a hint; src_class comes from the cited role
    if quote and not span_placed:
        extras[SPAN_TEXT_EXTRA_KEY] = (
            quote  # not locatable here; the grounding step (W-17) judges it
        )
    extras = dict(sorted(extras.items()))

    try:
        return AssertionEnvelope(
            write_id=w_id,
            assertion_key=key,
            occurrence_id=o_id,
            scope=ctx.scope,
            provenance=Provenance(
                session_id=ctx.session_id,
                agent_id=ctx.agent_id,
                principal=ctx.principal,
                source_id=ctx.source_id,
                source_version=ctx.source_version,
            ),
            text=text,
            kind=kind,
            subject=mention(subject_s, "subject"),
            object=mention(object_s, "object"),
            attribute=rel_s,
            speaker=reported_speaker or speaker or _clean(parsed.speaker),
            change_hint=chg,
            time=time,
            evidence=evidence,
            assessment=assessment,
            src_class=policy.src_class,
            epoch=ctx.epoch,
            erasure_generation=ctx.erasure_generation,
            prompt_hash=ctx.prompt_hash,
            extras=extras,
        )
    except ValidationError as exc:
        first = exc.errors()[0]
        return Rejection(
            index, RejectionCode.INVALID_ENVELOPE, f"{first['type']}: {first['msg']}"[:200]
        )


def gliner_annotation_hits(value: Any, path: str = "") -> list[str]:
    """Find attempts to author the system-owned window annotation, recursively."""
    hits: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and key.lower() == "gliner_mentions":
                hits.append(child_path)
            hits.extend(gliner_annotation_hits(child, child_path))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            hits.extend(gliner_annotation_hits(child, f"{path}[{index}]"))
    return hits


def _evidence(role: str, line: WindowLine, span: Span | None) -> Evidence:
    return Evidence(
        role=role,  # type: ignore[arg-type]
        turn=TurnRef(
            source_id=line.source_id, source_version=line.source_version, seg_index=line.seg_index
        ),
        span=span,
        source_family=line.speaker or line.role,
    )


def _binding(subject: str | None, speaker: str | None) -> Binding:
    """Initial binding (the grounding step, W-17, may lower it): a named subject is ``bound``; with no
    subject surface but a known speaker the subject is implied (``partly``); otherwise ``unbound``."""
    if subject is not None:
        return "bound"
    return "partly" if speaker else "unbound"
