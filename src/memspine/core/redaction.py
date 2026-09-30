"""Secret / PII redaction at the write door (B8, firewall parity).

Deterministic regexes only (no model on the write path). Patterns are written
for memspine, informed by the categories peers screen for (cloud keys, VCS and
chat tokens, JWTs, private keys, bearer tokens, key=value credentials, email).
Matches are replaced by ``[REDACTED:<kind>]`` so the record keeps its shape.
"""

from __future__ import annotations

import re

__all__ = ["PATTERNS", "redact"]

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


def redact(text: str) -> tuple[str, list[str]]:
    """Return ``(redacted_text, kinds_found)``; ``kinds_found`` is empty if clean."""
    found: list[str] = []
    for kind, pattern in PATTERNS:
        text, count = pattern.subn(f"[REDACTED:{kind}]", text)
        if count:
            found.append(kind)
    return text, found
