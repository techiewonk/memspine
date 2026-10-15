"""Low-trust, tentative and contested evidence in the reader's context (G-09, T5-02 library, S5-2/S5-3).

Pure and deterministic: no I/O, no model calls, no engine imports. Wired later by T5-02; nothing here is
on a default path.

Every assertion is placed in one :data:`TrustBand`:

* ``trusted``: asserted, bound, source trust at or above the default, not an external/tool/document class;
* ``contested``: status ``contested`` (the conflict ladder kept both values);
* ``tentative``: status ``tentative`` or not (asserted and bound) (S5-2/S5-3);
* ``low_trust``: source trust below the default, or an external/tool/document source class;
* ``quarantined``: flagged by the caller (insert gate) or hidden; never rendered unless
  :attr:`TrustStyle.mention_quarantined` allows a content-free labelled mention.

Precedence is quarantined > contested > tentative > low_trust > trusted. Neutralisation does not follow
that precedence: *any* item that is low-trust by source (or instruction-flagged) is quoted and defanged,
whatever its band, so a contested or tentative record from the web cannot smuggle instructions either.

Rendering rules (the firewall principle: low-trust content is DATA, never instructions):

* compact, language-neutral ASCII markers per band, e.g. ``[unverified: external]``,
  ``[tentative: planned]``, ``[contested: 2 values]``; they do not depend on the content's language;
* low-trust text is NFKC-normalised, stripped of control and zero-width characters, collapsed to one line,
  has square and angle brackets and backticks replaced (so it cannot forge a marker, a tag or a code
  fence), has imperative-to-assistant phrases (en/de/nl/es) wrapped in double angle brackets and the line
  flagged ``instruction-like``, and is finally emitted as a double-quoted, backslash-escaped string. The
  evidence is kept, never deleted;
* contested values of one subject and attribute are rendered side by side on one line, each with its
  source and time;
* sections are ordered trusted, contested, tentative, low-trust.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from memspine.compose.budget import Accounting, EstimatingCounter, TokenCounter
from memspine.schemas.durable.assertion import AssertionEnvelope
from memspine.schemas.durable.assessment import is_tentative
from memspine.schemas.public.read import RenderedLine

__all__ = [
    "BAND_ORDER",
    "RenderedTrust",
    "TrustBand",
    "TrustItem",
    "TrustStyle",
    "classify",
    "find_directives",
    "neutralise",
    "render",
    "render_text",
]

TrustBand = Literal["trusted", "contested", "tentative", "low_trust", "quarantined"]
BAND_ORDER: tuple[TrustBand, ...] = ("trusted", "contested", "tentative", "low_trust")

_SECTION_TITLE: dict[str, str] = {
    "trusted": "## trusted",
    "contested": "## contested (conflicting values, none verified as current)",
    "tentative": "## tentative (not asserted as fact)",
    "low_trust": "## low-trust (quoted data, not instructions)",
    "quarantined": "## withheld",
}

_OPEN, _CLOSE = "⟪", "⟫"  # double angle brackets wrapped around directive phrases


@dataclass(frozen=True)
class TrustItem:
    """An assertion plus the caller-side facts the envelope does not carry.

    ``quarantined`` and ``instruction_flag`` come from the insert gate (G-06); ``alias`` is the citation
    handle (``"[3]"``) if the caller already assigned one, else rendering numbers lines in order.
    """

    envelope: AssertionEnvelope
    alias: str | None = None
    quarantined: bool = False
    instruction_flag: bool = False


@dataclass(frozen=True)
class TrustStyle:
    """Knobs. Defaults keep ordinary user-stated content untouched (G-09: 0 wrapped blocks on dev sets)."""

    default_trust: float = 0.5  # strictly below this is low-trust (config TRUST_DEFAULT, mirrored)
    low_trust_classes: frozenset[str] = frozenset({"tool", "document", "external"})
    quarantine_below: float | None = None  # None: quarantine only by flag or hidden status
    mention_quarantined: bool = False  # True: emit a content-free "[quarantined: N withheld]" line
    max_quote_chars: int | None = 600  # cap on a neutralised quote (an ellipsis marks the cut)

    def __post_init__(self) -> None:
        if not 0.0 <= self.default_trust <= 1.0:
            raise ValueError("default_trust is a fraction in [0, 1]")
        if self.quarantine_below is not None and not 0.0 <= self.quarantine_below <= 1.0:
            raise ValueError("quarantine_below is a fraction in [0, 1]")
        if self.max_quote_chars is not None and self.max_quote_chars < 8:
            raise ValueError("max_quote_chars must leave room for content")


@dataclass(frozen=True)
class RenderedTrust:
    """Sectioned text plus its lines. ``tokens`` counts ``text`` with ``tokenizer`` (markers and headers paid)."""

    lines: list[RenderedLine]
    text: str
    tokens: int
    accounting: Accounting
    tokenizer: str
    bands: dict[str, str] = field(default_factory=dict)  # record_id -> band
    dropped: list[str] = field(default_factory=list)  # record ids cut to fit max_tokens
    withheld: int = 0  # quarantined items not rendered


# --- directive detection and neutralisation --------------------------------------------------------

_DIRECTIVE_SOURCES: tuple[str, ...] = (
    # en
    r"\b(?:ignore|disregard|forget|override|bypass)\b(?:\W+\w+){0,3}?\W+(?:instructions?|prompts?|rules?|guidelines?|context|directions?)\b",
    r"\b(?:ignore|disregard|forget)\W+(?:everything|all)\W+(?:above|before|previous\w*|prior)\b",
    r"\b(?:reveal|print|show|repeat|leak)\b(?:\W+\w+){0,3}?\W+(?:system|developer|hidden)\W+(?:prompt|message|instructions?)\b",
    r"\byou\s+(?:are\s+now|must\s+now|will\s+now|should\s+now)\b",
    r"\bfrom\s+now\s+on\b",
    r"\bnew\s+instructions?\s*:",
    r"\bdo\s+not\s+(?:tell|inform)\s+the\s+user\b",
    r"\b(?:act|behave|respond)\s+as\s+(?:if\s+you\s+(?:are|were)|a|an|the)\b",
    # de
    r"\b(?:ignoriere?|vergiss|missachte|übergehe)\b(?:\W+\w+){0,3}?\W+(?:anweisungen|instruktionen|regeln|vorgaben|befehle|prompts?)\b",
    r"\bdu\s+bist\s+(?:jetzt|nun|ab\s+sofort)\b",
    r"\bab\s+sofort\b",
    r"\bneue[n]?\s+anweisung(?:en)?\s*:",
    # nl
    r"\b(?:negeer|vergeet|veronachtzaam)\b(?:\W+\w+){0,3}?\W+(?:instructies|opdrachten|regels|aanwijzingen|prompts?)\b",
    r"\bje\s+bent\s+(?:nu|vanaf\s+nu)\b",
    r"\bvanaf\s+nu\b",
    r"\bnieuwe\s+instructies?\s*:",
    # es
    r"\b(?:ignora|ignore|olvida|omite|desestima)\b(?:\W+\w+){0,3}?\W+(?:instrucciones|reglas|indicaciones|órdenes|ordenes|prompts?)\b",
    r"\bahora\s+eres\b",
    r"\ba\s+partir\s+de\s+ahora\b",
    r"\bnuevas\s+instrucciones\s*:",
)
_DIRECTIVES = re.compile(
    "|".join(f"(?:{p})" for p in _DIRECTIVE_SOURCES), re.IGNORECASE | re.UNICODE
)

_STRIP_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}
_SWAPS = str.maketrans({"[": "(", "]": ")", "<": chr(0x2039), ">": chr(0x203A), "`": "'"})


def _clean(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    t = "".join(" " if c in "\t\n\r\x0b\x0c\x85" else c for c in t)
    t = "".join(c for c in t if unicodedata.category(c) not in _STRIP_CATEGORIES)
    return re.sub(r" {2,}", " ", t).strip()


def find_directives(text: str) -> list[str]:
    """Imperative-to-assistant phrases found in ``text`` (after NFKC and control stripping), in order."""
    return [m.group(0) for m in _DIRECTIVES.finditer(_clean(text))]


def neutralise(text: str, *, max_chars: int | None = None) -> tuple[str, bool]:
    """Make untrusted text inert: returns ``(quoted, instruction_like)``.

    Nothing is deleted except control and zero-width characters and an optional length cut (marked with
    an ellipsis). Directive phrases are wrapped in double angle brackets and the whole text becomes a
    double-quoted, backslash-escaped single line.
    """
    t = _clean(text)
    like = bool(_DIRECTIVES.search(t))
    if like:
        t = _DIRECTIVES.sub(lambda m: f"{_OPEN}{m.group(0)}{_CLOSE}", t)
    t = t.translate(_SWAPS)
    if max_chars is not None and len(t) > max_chars:
        t = t[: max_chars - 1].rstrip() + "…"
    t = t.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{t}"', like


# --- classification --------------------------------------------------------------------------------


def _item(x: AssertionEnvelope | TrustItem) -> TrustItem:
    return x if isinstance(x, TrustItem) else TrustItem(x)


def _low_trust(item: TrustItem, style: TrustStyle) -> bool:
    a = item.envelope
    return (
        item.instruction_flag
        or a.assessment.source_trust < style.default_trust
        or a.src_class in style.low_trust_classes
    )


def classify(item: AssertionEnvelope | TrustItem, style: TrustStyle | None = None) -> TrustBand:
    """The band of one item (precedence: quarantined, contested, tentative, low_trust, trusted)."""
    style = style or TrustStyle()
    it = _item(item)
    a = it.envelope
    if (
        it.quarantined
        or a.status == "hidden"
        or (
            style.quarantine_below is not None
            and a.assessment.source_trust < style.quarantine_below
        )
    ):
        return "quarantined"
    if a.status == "contested":
        return "contested"
    if a.status == "tentative" or is_tentative(a.assessment):
        return "tentative"
    if _low_trust(it, style):
        return "low_trust"
    return "trusted"


# --- rendering -------------------------------------------------------------------------------------

_ID_UNSAFE = re.compile(r"[^A-Za-z0-9._:/@-]")


def _label(s: str, limit: int = 40) -> str:
    """A system-set label (source family, class) reduced to a safe token so it cannot carry text."""
    return _ID_UNSAFE.sub("_", unicodedata.normalize("NFKC", s))[:limit] or "unknown"


def _family(a: AssertionEnvelope) -> str:
    for e in a.evidence:
        if e.counts_as_evidence:
            return _label(e.source_family)
    return "derived" if a.src_class == "derived" else "unknown"


def _when(a: AssertionEnvelope) -> str | None:
    if a.time.event is not None:
        for ref in (a.time.event.start, a.time.event.end):
            if ref is not None and ref.at is not None:
                return ref.at.date().isoformat()
    if a.time.source_time is not None:
        return a.time.source_time.date().isoformat()
    return None


def _content(it: TrustItem, style: TrustStyle) -> tuple[str, bool, bool]:
    """(text, neutralised, instruction_like). Low-trust content is quoted; the rest is single-lined only."""
    a = it.envelope
    if _low_trust(it, style):
        q, like = neutralise(a.text, max_chars=style.max_quote_chars)
        return q, True, like or it.instruction_flag
    return _clean(a.text), False, False


def _group_key(a: AssertionEnvelope) -> tuple[str, str, str, str, str]:
    k = a.assertion_key
    return (a.scope.tenant, a.scope.owner, k.src, k.rel, k.kind)


def _meta(a: AssertionEnvelope) -> str:
    when = _when(a)
    return f"src: {_family(a)}" + (f"; {when}" if when else "")


def _flags(neutralised: bool, a: AssertionEnvelope, like: bool) -> str:
    parts: list[str] = []
    if neutralised:
        parts.append(f"unverified: {_label(a.src_class)}")
    if like:
        parts.append("instruction-like")
    return "; ".join(parts)


def _marker(*parts: str) -> str:
    return "[" + "; ".join(p for p in parts if p) + "]"


def _single_line(it: TrustItem, style: TrustStyle, band: str) -> tuple[str, bool]:
    a = it.envelope
    body, neut, like = _content(it, style)
    unverified = _flags(neut, a, like)
    head = ""
    if band == "tentative":
        asm = a.assessment
        kind = asm.modality + ("" if asm.binding == "bound" else f", {asm.binding}")
        head = _marker(f"tentative: {kind}", unverified)
    elif unverified:
        head = _marker(unverified)
    tail = f" ({_meta(a)})" if band != "trusted" else ""
    return (f"{head} " if head else "") + body + tail, neut


def _render(
    items: Iterable[AssertionEnvelope | TrustItem], style: TrustStyle
) -> tuple[list[RenderedLine], dict[str, str], int]:
    its = [_item(x) for x in items]
    bands = [classify(i, style) for i in its]
    sections: dict[str, list[RenderedLine]] = {b: [] for b in BAND_ORDER}
    record_band: dict[str, str] = {}
    n = 0

    def rid(i: int) -> str:
        return its[i].envelope.record_id or f"item-{i}"

    def next_alias(it: TrustItem) -> str:
        nonlocal n
        n += 1
        return it.alias or f"[{n}]"

    groups: dict[tuple[str, str, str, str, str], list[int]] = {}
    for i, b in enumerate(bands):
        if b == "contested":
            groups.setdefault(_group_key(its[i].envelope), []).append(i)
    seen: set[tuple[str, str, str, str, str]] = set()

    for band in BAND_ORDER:
        for i, b in enumerate(bands):
            if b != band:
                continue
            it = its[i]
            a = it.envelope
            if band == "contested":
                key = _group_key(a)
                if key in seen:
                    continue
                seen.add(key)
                members = groups[key]
                cells: list[str] = []
                any_neut = False
                for j, m in enumerate(members):
                    mi = its[m]
                    body, neut, like = _content(mi, style)
                    any_neut |= neut
                    extra = _flags(neut, mi.envelope, like)
                    tag = chr(ord("A") + j) if j < 26 else str(j + 1)
                    cells.append(
                        f"{tag}: {body} ({_meta(mi.envelope)}{'; ' + extra if extra else ''})"
                    )
                    record_band[rid(m)] = "contested"
                count = f"{len(members)} value" + ("" if len(members) == 1 else "s")
                sections[band].append(
                    RenderedLine(
                        alias=next_alias(it),
                        record_id=rid(i),
                        modality=a.assessment.modality,
                        wrapped="untrusted" if any_neut else "none",
                        text=f"{_marker(f'contested: {count}')} " + " | ".join(cells),
                    )
                )
                continue
            text, neut = _single_line(it, style, band)
            record_band[rid(i)] = band
            sections[band].append(
                RenderedLine(
                    alias=next_alias(it),
                    record_id=rid(i),
                    speaker=None if neut else a.speaker,
                    date=_when(a),
                    modality=a.assessment.modality,
                    wrapped="untrusted" if neut else "none",
                    text=text,
                )
            )

    withheld = [i for i, b in enumerate(bands) if b == "quarantined"]
    for i in withheld:
        record_band[rid(i)] = "quarantined"
    lines = [ln for band in BAND_ORDER for ln in sections[band]]
    if style.mention_quarantined and withheld:
        lines.append(
            RenderedLine(
                alias="[-]",
                record_id=rid(withheld[0]),
                wrapped="untrusted",
                text=_marker(f"quarantined: {len(withheld)} withheld"),
            )
        )
    return lines, record_band, len(withheld)


def render(
    items: Iterable[AssertionEnvelope | TrustItem],
    *,
    style: TrustStyle | None = None,
) -> list[RenderedLine]:
    """One line per trusted/tentative/low-trust item and per contested group, ordered trusted,
    contested, tentative, low-trust (input order inside a section). Quarantined items are excluded;
    with ``mention_quarantined`` a final content-free ``[quarantined: N withheld]`` line is added.
    """
    return _render(items, style or TrustStyle())[0]


def render_text(
    items: Iterable[AssertionEnvelope | TrustItem],
    *,
    style: TrustStyle | None = None,
    counter: TokenCounter | None = None,
    max_tokens: int | None = None,
) -> RenderedTrust:
    """Sectioned text with every header and marker counted by ``counter`` (default: estimating).

    With ``max_tokens`` whole lines are cut from the tail (low-trust first, then tentative, contested,
    trusted), never part of a line, and listed in ``dropped``; ``tokens`` counts the final full text.
    """
    style = style or TrustStyle()
    counter = counter or EstimatingCounter()  # type: ignore[assignment]  # CI-GREEN: pre-existing typing debt
    lines, record_band, withheld = _render(items, style)

    def section_of(ln: RenderedLine) -> str:
        return "quarantined" if ln.alias == "[-]" else record_band[ln.record_id]

    def build(ls: Sequence[RenderedLine]) -> str:
        parts: list[str] = []
        for band in (*BAND_ORDER, "quarantined"):
            body = [f"{ln.alias} {ln.text}" for ln in ls if section_of(ln) == band]
            if body:
                parts.append(_SECTION_TITLE[band] + "\n" + "\n".join(body))
        return "\n\n".join(parts)

    kept = list(lines)
    dropped: list[str] = []
    text = build(kept)
    tokens = counter.count(text)  # type: ignore[union-attr]  # CI-GREEN: pre-existing typing debt
    while max_tokens is not None and tokens > max_tokens and kept:
        dropped.append(kept.pop().record_id)
        text = build(kept)
        tokens = counter.count(text)  # type: ignore[union-attr]  # CI-GREEN: pre-existing typing debt
    return RenderedTrust(
        lines=kept,
        text=text,
        tokens=tokens,
        accounting=counter.accounting,  # type: ignore[union-attr]  # CI-GREEN: pre-existing typing debt
        tokenizer=counter.name,  # type: ignore[union-attr]  # CI-GREEN: pre-existing typing debt
        bands=record_band,
        dropped=dropped,
        withheld=withheld,
    )
