"""#44: the PII pack (phone, Luhn card, SSN, IBAN, IP) and its checksums."""

from __future__ import annotations

import pytest

from memspine.core.redaction import find_pii, redact


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("call +1 415-555-0123 now", "phone"),
        ("call (415) 555-0123", "phone"),
        ("uk +44 20 7946 0958", "phone"),
        ("card 4111 1111 1111 1111 exp", "payment_card"),
        ("card 5500-0000-0000-0004", "payment_card"),
        ("ssn 123-45-6789", "us_ssn"),
        ("iban GB82 WEST 1234 5698 7654 32", "iban"),
        ("iban DE89370400440532013000", "iban"),
        ("host 192.168.1.20 down", "ipv4"),
        ("host 2001:db8::8a2e:370:7334 up", "ipv6"),
    ],
)
def test_pii_kinds_are_found_and_redacted(text: str, kind: str) -> None:
    redacted, kinds = redact(text, secrets=False, pii=True)
    assert kinds == [kind]
    assert f"[REDACTED:{kind}]" in redacted
    assert find_pii(text) == [kind]


@pytest.mark.parametrize(
    "text",
    [
        "card 4111111111111112 fails Luhn",
        "iban GB00WEST12345698765432 fails mod-97",
        "meeting 2023-10-05 at 10:30:00",
        "version 1.2.3.4567 and 999.1.1.1",
        "order 12345 shipped, 3 items",
        "ssn-shaped 000-12-3456 is invalid",
        "years 1999-2004",
    ],
)
def test_near_misses_are_left_alone(text: str) -> None:
    assert redact(text, secrets=False, pii=True) == (text, [])


def test_default_redact_is_secrets_only() -> None:
    assert redact("call +1 415-555-0123") == ("call +1 415-555-0123", [])
