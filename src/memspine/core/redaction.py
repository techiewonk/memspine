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
    # N23 (plan v3.2): model-provider keys (``sk-``, ``sk-proj-``, ``sk-ant-api03-``),
    # Google API keys, Stripe live keys; PersonaMem-v2 kept 46 / 46 ``sk-`` keys.
    ("llm_api_key", re.compile(r"\bsk-(?:proj-|ant-(?:api\d{2}-)?)?[A-Za-z0-9_-]{20,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{16,}\b")),
    # A password in a connection URL: postgres://user:password@host.
    ("url_credential", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s@/]+@")),
    # An env-style assignment the ``credential`` word boundary misses (LLM_API_KEY=...).
    (
        "credential",
        re.compile(
            r"\b[A-Z][A-Z0-9_]*(?:API_KEY|SECRET|PASSWORD|TOKEN|ACCESS_KEY)\s*=\s*"
            r"['\"]?(?!\[REDACTED:)[^\s'\"]{8,}"
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


#: ISO 13616 IBAN length per country: a candidate is cut to its country's
#: length before the mod-97 check, so a word after the IBAN is never swallowed.
_IBAN_LENGTHS: dict[str, int] = {
    "AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16, "BG": 22,
    "BH": 22, "BI": 27, "BR": 29, "BY": 28, "CH": 21, "CR": 22, "CY": 28, "CZ": 24,
    "DE": 22, "DJ": 27, "DK": 18, "DO": 28, "EE": 20, "EG": 29, "ES": 24, "FI": 18,
    "FK": 18, "FO": 18, "FR": 27, "GB": 22, "GE": 22, "GI": 23, "GL": 18, "GR": 27,
    "GT": 28, "HR": 21, "HU": 28, "IE": 22, "IL": 23, "IQ": 23, "IS": 26, "IT": 27,
    "JO": 30, "KW": 30, "KZ": 20, "LB": 28, "LC": 32, "LI": 21, "LT": 20, "LU": 20,
    "LV": 21, "LY": 25, "MC": 27, "MD": 24, "ME": 22, "MK": 19, "MN": 20, "MR": 27,
    "MT": 31, "MU": 30, "NI": 28, "NL": 18, "NO": 15, "OM": 23, "PK": 24, "PL": 28,
    "PS": 29, "PT": 25, "QA": 29, "RO": 24, "RS": 22, "RU": 33, "SA": 24, "SC": 31,
    "SD": 18, "SE": 24, "SI": 19, "SK": 24, "SM": 27, "SO": 23, "ST": 25, "SV": 28,
    "TL": 23, "TN": 24, "TR": 26, "UA": 29, "VA": 22, "VG": 24, "XK": 20, "YE": 30,
}  # fmt: skip


def _iban_ok(text: str) -> bool:
    compact = re.sub(r"\s", "", text).upper()
    if not 15 <= len(compact) <= 34:
        return False
    rotated = compact[4:] + compact[:4]
    numeric = "".join(str(int(ch, 36)) for ch in rotated)
    return int(numeric) % 97 == 1


def _iban_span(match: re.Match[str]) -> tuple[int, int] | None:
    """The IBAN at the start of ``match``: its country's length of characters
    when the country is known, else the longest prefix (shrinking from the end)
    that passes mod-97."""
    text = match.group(0)
    ends = [i + 1 for i, ch in enumerate(text) if not ch.isspace()]
    known = _IBAN_LENGTHS.get(text[:2].upper())
    if known is not None:
        candidates = [ends[known - 1]] if len(ends) >= known else []
    else:
        candidates = [end for count, end in enumerate(ends, 1) if count >= 15][::-1]
    for end in candidates:
        if _iban_ok(text[:end]):
            return match.start(), match.start() + end
    return None


def _phone_ok(text: str) -> bool:
    return 10 <= len(_digits(text)) <= 15


def _ipv4_ok(text: str) -> bool:
    try:
        ipaddress.IPv4Address(text)
    except ValueError:
        return False
    return True


#: A version cue right before a dotted quad ("version 1.2.3.4", "v10.0.0.1").
_VERSION_CUE = re.compile(r"(?i)(?:\bv|\bver\.?|\bversion|\brelease|\bbuild)\s*:?\s*$")


def _ipv4_span(match: re.Match[str]) -> tuple[int, int] | None:
    """A dotted quad is an address unless a version cue precedes it."""
    if not _ipv4_ok(match.group(0)):
        return None
    if _VERSION_CUE.search(match.string[max(0, match.start() - 12) : match.start()]):
        return None
    return match.span()


def _ipv6_ok(text: str) -> bool:
    """An IPv6 address, not a hex-ish word pair: a compressed form (``::``)
    needs three groups or one full-width (4-digit) group, so ``fe80::1`` and
    ``2001:db8::1`` count while ``ab::cd`` and ``1::2`` do not."""
    groups = [group for group in text.split(":") if group]
    if "::" in text and len(groups) < 3 and not any(len(group) == 4 for group in groups):
        return False
    if len(groups) < 2:
        return False
    try:
        ipaddress.IPv6Address(text)
    except ValueError:
        return False
    return True


Validator = Callable[[re.Match[str]], tuple[int, int] | None]


def _whole(check: Callable[[str], bool]) -> Validator:
    """A validator that redacts the whole match (or the ``pii`` group) when
    ``check`` accepts its text."""

    def validate(match: re.Match[str]) -> tuple[int, int] | None:
        group = "pii" if "pii" in match.re.groupindex else 0
        return match.span(group) if check(match.group(group)) else None

    return validate


_SEP = r"[\s.-]"
#: (kind, pattern, validator). Order matters: the checksummed and most specific
#: kinds run first, so a card number is not also counted as a phone number. A
#: kind may have several patterns. The validator returns the span to redact.
PII_PATTERNS: tuple[tuple[str, re.Pattern[str], Validator | None], ...] = (
    ("iban", re.compile(r"(?i)\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}\b"), _iban_span),
    ("payment_card", re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])"), _whole(_luhn_ok)),
    ("us_ssn", re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"), None),
    (
        "ipv6",
        re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])"),
        _whole(_ipv6_ok),
    ),
    ("ipv4", re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"), _ipv4_span),
    # E.164 / international: a leading + (separators optional: +14155552671).
    (
        "phone",
        re.compile(rf"(?<![\w+])\+\d{{1,3}}(?:{_SEP}?\(\d{{1,4}}\)|{_SEP}?\d{{1,4}}){{1,5}}(?!\w)"),
        _whole(_phone_ok),
    ),
    # A phone cue ("tel", "phone", "call" ...) makes any digit grouping a phone.
    (
        "phone",
        re.compile(
            r"(?i)\b(?:tel|phone|mobile|cell|fax|call|text|whatsapp)\b\.?:?\s*(?:me\s+at\s+|at\s+)?"
            r"(?P<pii>\(?\d[\d\s().-]{6,}\d)(?!\w)"
        ),
        _whole(_phone_ok),
    ),
    # A parenthesised area code: (415) 555-0123.
    (
        "phone",
        re.compile(rf"(?<!\w)\(\d{{2,4}}\){_SEP}?\d{{3,4}}{_SEP}?\d{{3,4}}(?!\w)"),
        _whole(_phone_ok),
    ),
    # A phone-shaped grouping: 3 digits, then 3-4, then 4 (415-555-0123, 020 7946 0958).
    (
        "phone",
        re.compile(rf"(?<![\w+.-])\d{{3}}{_SEP}\d{{3,4}}{_SEP}\d{{4}}(?![\w]|[.-]\d)"),
        _whole(_phone_ok),
    ),
    # A bare NANP number (area and exchange codes start 2-9): 4155552671.
    ("phone", re.compile(r"(?<![\w+.-])1?[2-9]\d{2}[2-9]\d{6}(?![\w]|[.-]\d)"), _whole(_phone_ok)),
)


_STREET = (
    r"(?:Street|St|Road|Rd|Avenue|Ave|Lane|Ln|Drive|Dr|Boulevard|Blvd|Grove|Way|Court|Ct|"
    r"Place|Pl|Terrace|Close|Crescent|Square|Sq|Highway|Hwy|Parkway|Pkwy)\.?"
)
#: N23: up to 30 non-digit characters between a cue and its value ("number is",
#: "in question ends with number", "for verification:").
_CUE_GAP = r"[^\d\n]{0,30}?"
#: An identifier with at least one digit (passport, licence): 6 to 15 letters / digits.
_ID_VALUE = r"(?P<pii>(?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{5,14})\b"
#: N23 (plan v3.2, ``firewall.pii_extended``): identifiers that have no checksum, found by
#: their cue ("Passport number: ...", "Bank Account Number: ...", "Address: ...") or by a
#: street-address shape. Cue-anchored, so a bare number is never taken; runs after the
#: base pack (which takes Luhn-valid cards and phone numbers first). Only the value is
#: masked, never the cue.
PII_EXTENDED_PATTERNS: tuple[tuple[str, re.Pattern[str], Validator | None], ...] = (
    (
        "payment_card",
        re.compile(r"(?i)\bcard\b" + _CUE_GAP + r"(?P<pii>\d(?:[ -]?[\d*]){11,18})(?![\d*])"),
        _whole(lambda text: True),
    ),
    (
        "bank_account",
        re.compile(
            r"(?i)\b(?:bank )?(?:account|acct) (?:number|no\b\.?|#)"
            + _CUE_GAP
            + r"(?P<pii>\d[\d -]{6,20}\d)(?!\d)"
        ),
        _whole(lambda text: True),
    ),
    ("passport", re.compile(r"(?i)\bpassport\b" + _CUE_GAP + _ID_VALUE), _whole(lambda t: True)),
    (
        "driving_licence",
        re.compile(r"(?i)\b(?:driver[’']?s?|driving) licen[cs]e\b" + _CUE_GAP + _ID_VALUE),
        _whole(lambda text: True),
    ),
    (
        "licence_plate",
        re.compile(
            r"(?i)\b(?:licen[cs]e plate|plate number|registration (?:number|no\.?))\s*[:=]?\s*"
            r"(?P<pii>[A-Z0-9][A-Z0-9 -]{3,9}[A-Z0-9])\b"
        ),
        _whole(lambda text: any(ch.isdigit() for ch in text)),
    ),
    (
        "street_address",
        re.compile(rf"\b\d{{1,5}}(?: [A-Z][a-z]+){{1,3}} {_STREET}(?=\W|$)"),
        None,
    ),
    (
        "street_address",
        re.compile(
            r"(?i)\b(?:postal |mailing |home |street |billing |shipping )?address\s*:\s*"
            r"(?P<pii>\d{1,5}[^\n;]{5,80})"
        ),
        _whole(lambda text: True),
    ),
)


def _sub_pii(
    text: str, kind: str, pattern: re.Pattern[str], valid: Validator | None
) -> tuple[str, int]:
    count = 0
    out: list[str] = []
    cursor = 0
    for match in pattern.finditer(text):
        span = match.span() if valid is None else valid(match)
        if span is None or span[0] < cursor:
            continue
        out.append(text[cursor : span[0]])
        out.append(f"[REDACTED:{kind}]")
        cursor = span[1]
        count += 1
    out.append(text[cursor:])
    return "".join(out), count


def find_pii(text: str, *, extended: bool = False) -> list[str]:
    """The PII kinds present in ``text``, in pack order (content is not changed).

    ``extended`` (N23) adds the cue-anchored kinds of :data:`PII_EXTENDED_PATTERNS`."""
    return redact(text, secrets=False, pii=True, pii_extended=extended)[1]


def redact(
    text: str, *, secrets: bool = True, pii: bool = False, pii_extended: bool = False
) -> tuple[str, list[str]]:
    """Return ``(redacted_text, kinds_found)``; ``kinds_found`` is empty if clean.

    ``secrets`` runs the B8 secret patterns (and email), ``pii`` the #44 PII pack,
    and ``pii_extended`` (with ``pii``) the N23 cue-anchored kinds after it. The
    default (secrets only) is the B8 behaviour."""
    found: list[str] = []
    if secrets:
        for kind, pattern in PATTERNS:
            text, count = pattern.subn(f"[REDACTED:{kind}]", text)
            if count:
                found.append(kind)
    if pii:
        pack = (*PII_PATTERNS, *PII_EXTENDED_PATTERNS) if pii_extended else PII_PATTERNS
        for kind, pattern, valid in pack:
            text, count = _sub_pii(text, kind, pattern, valid)
            if count and kind not in found:
                found.append(kind)
    return text, found
