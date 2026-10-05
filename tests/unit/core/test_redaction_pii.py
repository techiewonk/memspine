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


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("call me on 4155552671 today", "phone"),
        ("whatsapp +14155552671", "phone"),
        ("reach me at +442079460958", "phone"),
        ("uk office 020 7946 0958", "phone"),
        ("tel: 2024 1005 1234", "phone"),
        ("iban gb82west12345698765432", "iban"),
        ("iban gb82 west 1234 5698 7654 32", "iban"),
        ("host 10.0.0.1 is up", "ipv4"),
        ("ping 1.2.3.4 now", "ipv4"),
        ("link-local fe80::1 on eth0", "ipv6"),
        ("prefix 2001:db8::1 routed", "ipv6"),
    ],
)
def test_pii_variants_are_found(text: str, kind: str) -> None:
    redacted, kinds = redact(text, secrets=False, pii=True)
    assert kinds == [kind], redacted
    assert f"[REDACTED:{kind}]" in redacted


def test_spaced_iban_followed_by_a_word_redacts_the_whole_iban() -> None:
    redacted, kinds = redact("pay GB82 WEST 1234 5698 7654 32 NOW please", secrets=False, pii=True)
    assert kinds == ["iban"]
    assert redacted == "pay [REDACTED:iban] NOW please"
    assert "WEST" not in redacted


@pytest.mark.parametrize(
    "text",
    [
        "version 1.2.3.4 shipped",
        "upgrade to v10.0.0.1 today",
        "release 2.0.0.1 notes",
        "the C++ scope ab::cd resolves",
        "ratio 1::2 in the recipe",
        "order 2024 1005 1234 shipped",
        "invoice 1700000000 settled",
    ],
)
def test_pii_false_positives_are_left_alone(text: str) -> None:
    assert redact(text, secrets=False, pii=True) == (text, [])
