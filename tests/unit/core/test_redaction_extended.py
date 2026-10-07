"""N23 (plan v3.2): provider keys, URL credentials and the cue-anchored PII kinds."""

from __future__ import annotations

import pytest

from memspine.config.schema import FirewallConfig
from memspine.core.redaction import find_pii, redact


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("LLM_API_KEY=sk-zP82qkAv7rTY3xLm5N9Gh4wB2pQD7VcK", "llm_api_key"),
        ("key: sk-proj-AbCdEfGhIjKlMnOpQrStUvWx123", "llm_api_key"),
        ("DATABASE_URL=postgres://user:hunter22@localhost:5432/db", "url_credential"),
        ("MY_SERVICE_TOKEN=abcdef1234567890", "credential"),
    ],
)
def test_secret_patterns_mask_provider_keys_and_url_credentials(text: str, kind: str) -> None:
    masked, kinds = redact(text)
    assert kind in kinds
    assert f"[REDACTED:{kind}]" in masked


@pytest.mark.parametrize(
    ("text", "kind", "secret"),
    [
        ("Credit Card Number: 4929 7438 1627 5830", "payment_card", "4929 7438 1627 5830"),
        ("The card in question ends with number 4553 2841 9726 1398.", "payment_card", "4553"),
        ("My bank account number is 349875612398465", "bank_account", "349875612398465"),
        ("Bank Account Number: 024851937462", "bank_account", "024851937462"),
        ("Passport Number: YA9237614", "passport", "YA9237614"),
        ("my passport number for verification: 548296371", "passport", "548296371"),
        ("my driver’s licence number M630481927650", "driving_licence", "M630481927650"),
        ("License plate: KJL 4821", "licence_plate", "KJL 4821"),
        ("I live at 42 Palm Grove Street in Accra", "street_address", "42 Palm Grove Street"),
        ("Postal Address: 17 Rue Lepic, Paris", "street_address", "17 Rue Lepic"),
    ],
)
def test_extended_pack_masks_the_value_and_keeps_the_cue(text: str, kind: str, secret: str) -> None:
    masked, kinds = redact(text, secrets=False, pii=True, pii_extended=True)
    assert kind in kinds
    assert secret not in masked
    assert f"[REDACTED:{kind}]" in masked
    assert find_pii(text, extended=True)
    # The #44 pack alone leaves these (no checksum, no phone shape).
    assert kind not in redact(text, secrets=False, pii=True)[1]


@pytest.mark.parametrize(
    "text",
    [
        "I read 300 pages of my passport application guide last night.",
        "We stayed at the hotel for 12 nights in 2023.",
        "My account was hacked last year, so I changed everything.",
        "The card game lasted until 11 pm.",
        "Our street has 3 big trees.",
    ],
)
def test_extended_pack_leaves_ordinary_chat_alone(text: str) -> None:
    assert redact(text, secrets=True, pii=True, pii_extended=True) == (text, [])


def test_pii_extended_defaults_off() -> None:
    assert FirewallConfig().pii_extended is False
