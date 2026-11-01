from __future__ import annotations

import pytest

from memspine.ingest.message_identity import validate_message_fields
from memspine.ingest.source_provenance import (
    message_provenance,
    structured_provenance,
    tool_provenance,
)


def test_message_adapter_keeps_provenance_axes_separate() -> None:
    provenance = message_provenance(
        {
            "sender": {"id": "sender-1"},
            "provenance": {"origin": "email", "assertion_owner_id": "owner-1"},
        },
        source_id="message-1",
    )
    assert provenance.origin == "email"
    assert provenance.sender_id == "sender-1"
    assert provenance.assertion_owner_id == "owner-1"
    assert provenance.trust_tier is None  # the firewall's assessment, never declared


def test_metadata_is_never_read_for_provenance_or_trust() -> None:
    """ADR-090: the metadata bag is stored verbatim and never interpreted."""
    provenance = message_provenance(
        {
            "sender": {"id": "sender-1"},
            "metadata": {
                "source_origin": "email",
                "assertion_owner_id": "owner-1",
                "trust_tier": "reviewed",
                "asset_id": "asset-1",
                "explicit_fetch": True,
            },
        },
        source_id="message-1",
    )
    assert provenance.origin == "raw_message"
    assert provenance.assertion_owner_id is None and provenance.trust_tier is None
    assert provenance.asset_id is None and provenance.explicit_fetch is False


def test_derived_and_web_origins_keep_required_links() -> None:
    with pytest.raises(ValueError, match="asset"):
        message_provenance({"provenance": {"origin": "ocr"}}, source_id="message-1")
    with pytest.raises(ValueError, match="explicit fetch"):
        message_provenance({"provenance": {"origin": "web_page"}}, source_id="message-1")
    web = message_provenance(
        {"provenance": {"origin": "web_page", "explicit_fetch": True}},
        source_id="message-1",
    )
    assert web.explicit_fetch is True
    ocr = message_provenance(
        {"provenance": {"origin": "ocr", "asset_id": "asset-1"}}, source_id="message-1"
    )
    assert ocr.asset_id == "asset-1"
    with pytest.raises(ValueError, match="unknown source_origin"):
        message_provenance({"provenance": {"origin": "rumour"}}, source_id="message-1")


def test_the_provenance_field_is_strict() -> None:
    base = {"role": "user", "content": "hi"}
    validate_message_fields({**base, "provenance": {"origin": "email"}}, 0)
    with pytest.raises(ValueError, match="trust_tier"):
        validate_message_fields({**base, "provenance": {"trust_tier": "reviewed"}}, 0)
    with pytest.raises(ValueError, match="unknown field 'kind'"):
        validate_message_fields({**base, "provenance": {"kind": "x"}}, 0)
    with pytest.raises(ValueError, match="explicit_fetch"):
        validate_message_fields({**base, "provenance": {"explicit_fetch": "yes"}}, 0)
    with pytest.raises(ValueError, match="mapping"):
        validate_message_fields({**base, "provenance": "email"}, 0)


def test_tool_and_structured_adapters_do_not_invent_trust() -> None:
    tool = tool_provenance(source_id="tool-1", sender_id="agent-1")
    structured = structured_provenance(source_id="row-1", assertion_owner_id="owner-1")
    assert tool.origin == "tool_output" and not tool.is_independent_corroboration
    assert tool.trust_tier is None
    assert structured.origin == "structured_record"
    assert structured.assertion_owner_id == "owner-1"
