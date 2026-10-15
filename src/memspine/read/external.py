# ruff: noqa: E501
"""External public knowledge as a separate, labelled, opt-in context block (LIB-45).

Pure and unwired (wired later by R3-13; decisions R-23, ER-25 and the N1 firewall). Imports only the
standard library, pydantic and :mod:`memspine.read.contract_rules` (LIB-16). No I/O, no network, no clock
read (``now`` is an argument), no model call, deterministic. The provider is injected.

Rules encoded:

* Opt-in only (:func:`plan_external`): config enabled AND the question is generic or explicitly asks for
  public facts. A personal-only question is never planned unless the asker explicitly asked for public
  facts. An undecided contract (``personal=None``) is planned only on an explicit ask.
* Separate labelled block: :class:`ExternalBlock` has label ``public`` and carries the clause "this never
  proves a private fact". It is never mixed with personal memory.
* Never deposited: every block and line is marked (``deposit_forbidden``), and
  :func:`assert_not_deposited` is the guard the write path calls to refuse it.
* Low trust: the family is the registrable domain (so one site cannot count as several independent
  sources) and the trust is capped at ``trust_cap`` (the external family cap).
* Uncertainty stays separate: undated, stale, non-https, truncated, instruction-like and caller-supplied
  asset/OCR flags are carried on each line in ``flags``; they are never folded into trust.
* Untrusted text (N1 firewall): this module does not render. It returns structured lines; the compose
  layer (LIB-25) neutralises and renders them. ``instruction_like`` is only a flag to help that step.

Interpretations: the registrable domain uses a small built-in set of multi-label public suffixes, not the
full public suffix list (no data file, no I/O); unknown multi-label suffixes fall back to the last two
labels. Dedupe key is ``(registrable domain, sha256 of case- and whitespace-folded text)`` and keeps the
first occurrence (the provider's rank order). Packing follows the R-18 packer rule: skip an oversize line
and continue.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter as _Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, runtime_checkable
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from memspine.read.contract_rules import TypedContract

__all__ = [
    "BLOCK_LABEL",
    "EXTERNAL_FAMILY",
    "NEVER_PROVES_CLAUSE",
    "ExternalBlock",
    "ExternalConfig",
    "ExternalDepositError",
    "ExternalLine",
    "ExternalPlan",
    "ExternalProvider",
    "ExternalSnippet",
    "TextCounter",
    "assert_not_deposited",
    "external_block",
    "is_external",
    "plan_external",
    "registrable_domain",
]

BLOCK_LABEL = "public"
EXTERNAL_FAMILY = "external"
NEVER_PROVES_CLAUSE = "public knowledge never proves a private fact"

Flag = Literal[
    "undated", "stale", "non_https", "truncated", "instruction_like", "asset", "ocr", "uncertain"
]


@runtime_checkable
class TextCounter(Protocol):
    """Anything that counts tokens of a string (structurally the compose ``TokenCounter``)."""

    def count(self, text: str) -> int: ...


class ExternalSnippet(BaseModel):
    """One provider result. ``uncertainty`` holds caller flags such as ``asset`` or ``ocr``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    text: str
    title: str = ""
    retrieved_at: datetime | None = None
    uncertainty: tuple[str, ...] = ()


@runtime_checkable
class ExternalProvider(Protocol):
    """Injected lookup: a (privacy-filtered) generic query to snippets, best first."""

    def search(self, query: str, *, limit: int) -> Sequence[ExternalSnippet]: ...


class ExternalConfig(BaseModel):
    """Policy for the external block. ``enabled`` is the opt-in (default off)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    max_calls: int = Field(default=20, ge=0)
    max_snippets: int = Field(default=3, ge=0)
    per_domain_cap: int = Field(default=2, ge=1)
    max_age_days: int | None = Field(default=365, ge=0)  # None: no freshness filter
    drop_undated: bool = False  # True: an undated snippet is dropped, not just flagged
    require_https: bool = False  # True: a non-https snippet is dropped, not just flagged
    max_chars_per_snippet: int = Field(default=600, ge=1)
    trust_cap: float = Field(default=0.3, ge=0.0, le=1.0)  # the external family cap
    allow_personal: bool = False  # even a personal question may be planned if asked explicitly


class ExternalPlan(BaseModel):
    """Whether to call the provider, with the reason (always set, for the trace)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call: bool
    reason: str

    def __bool__(self) -> bool:
        return self.call


class ExternalLine(BaseModel):
    """One structured line of the block. Compose renders it; this layer never does."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    domain: str
    family: str  # registrable domain
    title: str
    text: str
    retrieved_at: datetime | None
    content_hash: str
    trust: float
    flags: tuple[str, ...]
    tokens: int
    provenance: Literal["external"] = EXTERNAL_FAMILY
    deposit_forbidden: Literal[True] = True


class ExternalBlock(BaseModel):
    """The labelled ``public`` block. Never personal memory, never deposited."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: Literal["public"] = BLOCK_LABEL
    clause: str = NEVER_PROVES_CLAUSE
    lines: tuple[ExternalLine, ...] = ()
    tokens: int = 0
    budget: int = 0
    dropped: dict[str, int] = Field(default_factory=dict)  # reason -> count
    provenance: Literal["external"] = EXTERNAL_FAMILY
    deposit_forbidden: Literal[True] = True

    @property
    def empty(self) -> bool:
        return not self.lines


class ExternalDepositError(ValueError):
    """An external-knowledge object reached the write path."""


# --- planning

_ASKS_PUBLIC = re.compile(
    r"\b(?:look (?:it |this |that )?up|search (?:the )?(?:web|internet|online)|google|"
    r"on the (?:web|internet)|online|publicly|public(?:ly)? (?:fact|info|information|record)s?|"
    r"in general|generally speaking|according to (?:wikipedia|the web)|wikipedia|"
    r"world knowledge|general knowledge|what is known)\b",
    re.IGNORECASE,
)


def plan_external(
    contract: TypedContract,
    config: ExternalConfig,
    *,
    query: str = "",
    explicit: bool | None = None,
    calls_used: int = 0,
) -> ExternalPlan:
    """Decide whether to call the provider. Opt-in AND (generic OR explicit ask), within the call cap.

    ``explicit`` overrides the phrase check on ``query`` (a caller that knows the user asked).
    """
    if not config.enabled:
        return ExternalPlan(call=False, reason="opt_out")
    if calls_used >= config.max_calls:
        return ExternalPlan(call=False, reason="call_budget_spent")
    asked = explicit if explicit is not None else bool(_ASKS_PUBLIC.search(query))
    if contract.personal is False:
        return ExternalPlan(call=True, reason="generic_question")
    if asked:
        if contract.personal is True and not config.allow_personal:
            return ExternalPlan(call=False, reason="personal_not_allowed")
        return ExternalPlan(call=True, reason="explicit_public_ask")
    if contract.personal is True:
        return ExternalPlan(call=False, reason="personal_question")
    return ExternalPlan(call=False, reason="undecided_not_asked")


# --- domains

# Second-level public suffixes under which the registrable domain has three labels.
_MULTI_SUFFIX = frozenset(
    {
        "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "com.au", "net.au", "org.au", "edu.au",
        "gov.au", "co.nz", "org.nz", "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp", "co.in",
        "org.in", "net.in", "ac.in", "gov.in", "com.br", "org.br", "gov.br", "com.cn", "org.cn",
        "gov.cn", "com.mx", "co.za", "org.za", "com.tr", "com.sg", "com.hk", "co.kr", "or.kr",
        "co.il", "com.ar", "com.co",
    }
)  # fmt: skip


def registrable_domain(url_or_host: str) -> str:
    """Lower-cased registrable domain of a URL or host; ``""`` when there is no host."""
    raw = url_or_host.strip()
    if "//" not in raw:
        raw = "//" + raw
    host = (urlsplit(raw).hostname or "").strip(".").lower()
    if not host:
        return ""
    if re.fullmatch(r"[0-9.]+|[0-9a-f:]+", host) and (":" in host or host.count(".") == 3):
        return host  # an IP literal is its own family
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if ".".join(labels[-2:]) in _MULTI_SUFFIX:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


# --- the block

_WS = re.compile(r"\s+")
_INSTRUCTION = re.compile(
    r"\b(?:ignore (?:all |any |the )?(?:previous|prior|above) (?:instructions?|prompts?)|"
    r"disregard (?:all |any |the )?(?:previous|prior|above)|you (?:must|should) now|"
    r"system prompt|new instructions?|act as|from now on|"
    r"(?:remember|store|save|memori[sz]e) (?:this|that|the following))\b|"
    r"<\s*/?\s*(?:system|assistant|instructions?)\s*>|^\s*(?:system|assistant)\s*:",
    re.IGNORECASE | re.MULTILINE,
)


def _fold(text: str) -> str:
    return _WS.sub(" ", text).strip().casefold()


def _hash(text: str) -> str:
    return hashlib.sha256(_fold(text).encode("utf-8")).hexdigest()[:16]


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _line_text(domain: str, title: str, text: str) -> str:
    head = f"{title} ({domain})" if title else domain
    return f"{head}: {text}"


def external_block(
    snippets: Sequence[ExternalSnippet],
    budget: int,
    counter: TextCounter,
    *,
    config: ExternalConfig | None = None,
    now: datetime | None = None,
) -> ExternalBlock:
    """Build the labelled ``public`` block from provider snippets (best first).

    Order of filters: freshness / scheme, dedupe by (domain, content hash), per-domain cap, then pack to
    ``budget`` tokens (skip an oversize line and continue) up to ``max_snippets``. Freshness needs
    ``now``; without it no snippet is judged stale (and the config's age filter is not applied).
    """
    if budget < 0:
        raise ValueError("budget is non-negative")
    cfg = config or ExternalConfig()
    dropped: _Counter[str] = _Counter()
    seen: set[tuple[str, str]] = set()
    per_domain: _Counter[str] = _Counter()
    lines: list[ExternalLine] = []
    used = 0
    cutoff = (
        _aware(now) - timedelta(days=cfg.max_age_days)
        if now is not None and cfg.max_age_days is not None
        else None
    )
    for sn in snippets:
        if len(lines) >= cfg.max_snippets:
            dropped["over_max_snippets"] += 1
            continue
        domain = registrable_domain(sn.url)
        body = _WS.sub(" ", sn.text).strip()
        if not domain or not body:
            dropped["empty_or_no_host"] += 1
            continue
        flags: list[str] = []
        scheme = urlsplit(sn.url.strip()).scheme.lower()
        if scheme and scheme != "https":
            if cfg.require_https:
                dropped["non_https"] += 1
                continue
            flags.append("non_https")
        if sn.retrieved_at is None:
            if cfg.drop_undated:
                dropped["undated"] += 1
                continue
            flags.append("undated")
        elif cutoff is not None and _aware(sn.retrieved_at) < cutoff:
            dropped["stale"] += 1
            continue
        key = (domain, _hash(body))
        if key in seen:
            dropped["duplicate"] += 1
            continue
        if per_domain[domain] >= cfg.per_domain_cap:
            dropped["domain_cap"] += 1
            continue
        if len(body) > cfg.max_chars_per_snippet:
            body = body[: cfg.max_chars_per_snippet].rstrip()
            flags.append("truncated")
        title = _WS.sub(" ", sn.title).strip()
        if _INSTRUCTION.search(body) or _INSTRUCTION.search(title):
            flags.append("instruction_like")
        for u in sn.uncertainty:
            tag = u.strip().lower()
            if tag and tag not in flags:
                flags.append(tag if tag in ("asset", "ocr") else "uncertain")
        flags = sorted(set(flags))
        tokens = counter.count(_line_text(domain, title, body))
        if tokens > budget - used:
            dropped["over_budget"] += 1
            continue
        seen.add(key)
        per_domain[domain] += 1
        used += tokens
        lines.append(
            ExternalLine(
                url=sn.url.strip(),
                domain=(urlsplit(sn.url.strip()).hostname or domain).lower(),
                family=domain,
                title=title,
                text=body,
                retrieved_at=sn.retrieved_at,
                content_hash=key[1],
                trust=cfg.trust_cap,
                flags=tuple(flags),
                tokens=tokens,
            )
        )
    return ExternalBlock(
        lines=tuple(lines), tokens=used, budget=budget, dropped=dict(sorted(dropped.items()))
    )


# --- the guard


def is_external(obj: object) -> bool:
    """True for an external block, line or snippet, or any object marked ``deposit_forbidden`` / external."""
    if isinstance(obj, (ExternalBlock, ExternalLine, ExternalSnippet)):
        return True
    if isinstance(obj, dict):
        return bool(obj.get("deposit_forbidden")) or obj.get("provenance") == EXTERNAL_FAMILY
    return bool(getattr(obj, "deposit_forbidden", False)) or (
        getattr(obj, "provenance", None) == EXTERNAL_FAMILY
    )


def assert_not_deposited(obj: object) -> None:
    """Write-path guard: raise :class:`ExternalDepositError` if ``obj`` is external knowledge.

    Call it on anything about to become a memory (and on each element of a sequence). Returns ``None``
    for personal material.
    """
    if isinstance(obj, (list, tuple, set, frozenset)):
        for item in obj:
            assert_not_deposited(item)
        return
    if is_external(obj):
        raise ExternalDepositError("external public knowledge is never deposited as memory (R-23)")
