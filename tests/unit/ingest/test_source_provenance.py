from __future__ import annotations

import pytest

from memspine.ingest.source_provenance import (
    message_provenance,
    structured_provenance,
    tool_provenance,
)


def test_message_adapter_keeps_provenance_axes_separate() -> None:
    provenance = message_provenance(
        {
            "sender": {"id": "sender-1"},
            "metadata": {
                "source_origin": "email",
                "assertion_owner_id": "owner-1",
                "trust_tier": "reviewed",
            },
        },
        source_id="message-1",
    )
    assert provenance.origin == "email"
    assert provenance.sender_id == "sender-1"
    assert provenance.assertion_owner_id == "owner-1"
    assert provenance.trust_tier == "reviewed"


def test_derived_and_web_origins_keep_required_links() -> None:
    with pytest.raises(ValueError, match="asset"):
        message_provenance(
            {"metadata": {"source_origin": "ocr"}}, source_id="message-1"
        )
    with pytest.raises(ValueError, match="explicit fetch"):
        message_provenance(
            {"metadata": {"source_origin": "web_page"}}, source_id="message-1"
        )
    web = message_provenance(
        {"metadata": {"source_origin": "web_page", "explicit_fetch": True}},
        source_id="message-1",
    )
    assert web.explicit_fetch is True


def test_tool_and_structured_adapters_do_not_invent_trust() -> None:
    tool = tool_provenance(source_id="tool-1", sender_id="agent-1")
    structured = structured_provenance(source_id="row-1", assertion_owner_id="owner-1")
    assert tool.origin == "tool_output" and not tool.is_independent_corroboration
    assert tool.trust_tier is None
    assert structured.origin == "structured_record"
    assert structured.assertion_owner_id == "owner-1"
