"""Trust: shared module-level objects moved out of the engine with the trust mechanisms (F-12)."""

from __future__ import annotations

import re
import unicodedata
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

#: #3: tag on a held record an operator rejected (archived, never releasable).
_QUARANTINE_REJECTED_TAG = "quarantine_rejected"


#: A thousands separator between digit groups ("1,000", "1_000").
_FACT_THOUSANDS = re.compile(r"(?<=\d)[,_](?=\d{3}(?!\d))")


#: Tokens of a fact value: a number (sign and decimal point kept, the sign only
#: where no word precedes it), a word, or any other single character.
_FACT_TOKEN = re.compile(r"(?<![^\W_])[-+]?\d+(?:\.\d+)*|[^\W_]+|\S")


#: Symbols that change a value's meaning and so stay as tokens.
_FACT_SYMBOLS = frozenset("%+#")


def _fact_value(content: str) -> str:
    """#3: a fact's value for corroboration: NFKC-folded, case-folded and
    whitespace-normalised, with thousands separators dropped and plain
    punctuation ignored. Signs, decimal points, percent, currency symbols and
    ``+``/``#`` are kept, so "-500" differs from "500", "C++" from "C", "$100"
    from "€100" and "3.5" from "3 5". Paraphrases do not match."""
    text = unicodedata.normalize("NFKC", content).casefold().replace("\u2212", "-")
    text = _FACT_THOUSANDS.sub("", text)
    tokens = [
        token
        for token in _FACT_TOKEN.findall(text)
        if len(token) > 1
        or token.isalnum()
        or token in _FACT_SYMBOLS
        or unicodedata.category(token) == "Sc"
    ]
    return " ".join(tokens)


@dataclass(frozen=True)
class AuthorizeDecision:
    """``Engine.authorize`` result (B6): decision, weakest evidence trust, why."""

    allowed: bool
    min_trust: float
    weakest_id: str | None
    reason: str


#: #30: contents of the engine's own list cards a re-derived card replaces. A new card
#: repeats its predecessor's prefix by construction, so those (and only those) are
#: left out of the MINJA bridge-prefix comparison of that one write.
_REPLACED_CONTENTS: ContextVar[frozenset[str]] = ContextVar(
    "memspine_replaced_contents", default=frozenset()
)


#: Roles whose writes may corroborate a quarantined record out of quarantine
#: (E1). Excludes assistant/tool — the indirect-injection authorship surface.
_CORROBORATION_ROLES = frozenset({"operator", "system", "user"})


def _as_options_dict(raw: Any) -> dict[str, object] | None:
    """Policy sub-blocks arrive as plain config dicts; anything else is a typo
    the policy's own extra='forbid' validation will surface."""
    return dict(raw) if isinstance(raw, dict) else None


#: N3: tag stamped on a record archived by a taint rollback or repair. Duplicates
#: ``memspine.read.pipeline.state._TAINT_ARCHIVED_TAG`` (trust is a foundation and may
#: not import read).
_TAINT_ARCHIVED_TAG = "taint_archived"

