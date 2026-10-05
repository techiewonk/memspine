"""Secret / PII redaction at the write door (B8, firewall parity; #44 PII pack).

Deterministic regexes only (no model on the write path). Patterns are written
for memspine, informed by the categories peers screen for (cloud keys, VCS and
chat tokens, JWTs, private keys, bearer tokens, key=value credentials, email).
Matches are replaced by ``[REDACTED:<kind>]`` so the record keeps its shape.

The PII pack (``firewall.pii``) adds phone numbers, payment cards (Luhn
checked), US social security numbers, IBANs (ISO 13616 mod-97 checked) and
IPv4/IPv6 addresses. Checksums keep order numbers, dates and version strings
from being taken for cards or IBANs.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable

__all__ = ["PATTERNS", "PII_PATTERNS", "find_pii", "redact"]

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    ),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}")),
    (
        "credential",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|password|passwd|token|access[_-]?key)\b\s*[:=]\s*"
            r"['\"]?[^\s'\"]{8,}"
        ),
    ),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
)


def _digits(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


def _luhn_ok(text: str) -> bool:
    digits = _digits(text)
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for position, char in enumerate(reversed(digits)):
        value = int(char)
        if position % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _iban_ok(text: str) -> bool:
    compact = re.sub(r"\s", "", text).upper()
    if not 15 <= len(compact) <= 34:
        return False
    rotated = compact[4:] + compact[:4]
    numeric = "".join(str(int(ch, 36)) for ch in rotated)
    return int(numeric) % 97 == 1


def _phone_ok(text: str) -> bool:
    return 10 <= len(_digits(text)) <= 15


def _ipv4_ok(text: str) -> bool:
    try:
        ipaddress.IPv4Address(text)
    except ValueError:
        return False
    return True


def _ipv6_ok(text: str) -> bool:
    if text.count(":") < 2 or not any(ch.isalnum() for ch in text):
        return False
    try:
        ipaddress.IPv6Address(text)
    except ValueError:
        return False
    return True


#: (kind, pattern, validator). Order matters: the checksummed and most specific
#: kinds run first, so a card number is not also counted as a phone number.
PII_PATTERNS: tuple[tuple[str, re.Pattern[str], Callable[[str], bool] | None], ...] = (
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}\b"), _iban_ok),
    ("payment_card", re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])"), _luhn_ok),
    ("us_ssn", re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"), None),
    (
        "ipv6",
        re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])"),
        _ipv6_ok,
    ),
    ("ipv4", re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"), _ipv4_ok),
    (
        "phone",
        re.compile(
            r"(?<![\w+])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{1,4}\)[\s.-]?|\d{2,4}[\s.-])"
            r"\d{2,4}[\s.-]?\d{3,4}(?![\w])"
        ),
        _phone_ok,
    ),
)


def _sub_pii(
    text: str, kind: str, pattern: re.Pattern[str], valid: Callable[[str], bool] | None
) -> tuple[str, int]:
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        if valid is not None and not valid(match.group(0)):
            return match.group(0)
        count += 1
        return f"[REDACTED:{kind}]"

    return pattern.sub(replace, text), count


def find_pii(text: str) -> list[str]:
    """The PII kinds present in ``text``, in pack order (content is not changed)."""
    return redact(text, secrets=False, pii=True)[1]


def redact(text: str, *, secrets: bool = True, pii: bool = False) -> tuple[str, list[str]]:
    """Return ``(redacted_text, kinds_found)``; ``kinds_found`` is empty if clean.

    ``secrets`` runs the B8 secret patterns (and email), ``pii`` the #44 PII pack.
    The default (secrets only) is the B8 behaviour."""
    found: list[str] = []
    if secrets:
        for kind, pattern in PATTERNS:
            text, count = pattern.subn(f"[REDACTED:{kind}]", text)
            if count:
                found.append(kind)
    if pii:
        for kind, pattern, valid in PII_PATTERNS:
            text, count = _sub_pii(text, kind, pattern, valid)
            if count:
                found.append(kind)
    return text, found
