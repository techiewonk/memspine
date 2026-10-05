"""#48-#50 pure primitives: purpose gate, read scope, audit chain, retention classes,
and the remote-LLM gate's provider classification and text variants."""

from __future__ import annotations

from typing import Any

import orjson

from memspine.config.schema import ConsentConfig, RetentionClassConfig
from memspine.core.events import EventKind, MemoryEvent
from memspine.core.privacy import (
    AUDIT_GENESIS,
    chain_digest,
    current_purpose,
    matching_class,
    purpose_allows,
    read_scope,
    verify_audit_chain,
)
from memspine.core.records import MemoryRecord
from memspine.services.llm.tier_gate import is_local_provider, withheld_variants


def _rec(*purposes: str, ns: str = "u", memory_type: str = "semantic") -> MemoryRecord:
    return MemoryRecord(
        namespace=ns, memory_type=memory_type, content="c", consent_tags=list(purposes)
    )


def test_purpose_gate_rules() -> None:
    off = ConsentConfig()
    on = ConsentConfig(enforce=True)
    deny = ConsentConfig(enforce=True, untagged="deny")
    assert purpose_allows(_rec("support"), None, off)  # off: everything passes
    assert purpose_allows(_rec("support"), "support", on)
    assert not purpose_allows(_rec("support"), "marketing", on)
    assert not purpose_allows(_rec("support"), None, on)  # no purpose: no tagged record
    assert purpose_allows(_rec("*"), "anything", on) and purpose_allows(_rec("*"), None, on)
    assert purpose_allows(_rec(), "x", on) and not purpose_allows(_rec(), "x", deny)


def test_read_scope_outermost_and_purpose_inheritance() -> None:
    with read_scope("support") as outer:
        assert outer and current_purpose() == "support"
        with read_scope(None) as inner:
            assert not inner and current_purpose() == "support"
        with read_scope("billing"):
            assert current_purpose() == "billing"
        assert current_purpose() == "support"
    assert current_purpose() is None


def _chain(n: int) -> list[MemoryEvent]:
    events: list[MemoryEvent] = []
    prev = AUDIT_GENESIS
    for seq in range(1, n + 1):
        body: dict[str, Any] = {"verb": "search", "record_ids": [f"r{seq}"], "at": "t"}
        digest = chain_digest(prev, EventKind.READ_AUDIT.value, "u", "p", body)
        body["chain"] = {"prev": prev, "hash": digest}
        events.append(
            MemoryEvent(kind=EventKind.READ_AUDIT, namespace="u", actor="p", payload=body, seq=seq)
        )
        prev = digest
    return events


def test_audit_chain_verifies_and_detects_edit_removal_and_reorder() -> None:
    events = _chain(4)
    other = MemoryEvent(kind=EventKind.WRITE, namespace="u", payload={"x": 1}, seq=99)
    report = verify_audit_chain([*events, other])
    assert report.ok and report.events == 4
    assert report.head == events[-1].payload["chain"]["hash"]

    edited = [e.model_copy(deep=True) for e in events]
    edited[1].payload["record_ids"] = []
    assert verify_audit_chain(edited).broken_at == 2

    assert verify_audit_chain([events[0], *events[2:]]).broken_at == 3  # one removed
    assert verify_audit_chain(events[1:]).broken_at == 2  # head removed, full log
    assert verify_audit_chain(events[1:], anchored=False).ok  # rolling log anchor
    assert not verify_audit_chain([events[1], events[0], *events[2:]]).ok


def test_retention_class_first_match_wins() -> None:
    classes = [
        RetentionClassConfig(namespace="tmp/*", memory_type="episodic", ttl_days=1),
        RetentionClassConfig(namespace="tmp/*", ttl_days=30),
    ]
    episodic = matching_class(_rec(ns="tmp/a", memory_type="episodic"), classes)
    assert episodic is not None and episodic.ttl_days == 1
    semantic = matching_class(_rec(ns="tmp/a"), classes)
    assert semantic is not None and semantic.ttl_days == 30
    assert matching_class(_rec(ns="keep"), classes) is None


def test_provider_locality() -> None:
    assert is_local_provider("llamacpp/models/q.gguf", None)
    assert is_local_provider("ollama/llama3", None)
    assert is_local_provider("openai/qwen", "http://localhost:8000/v1")
    assert is_local_provider("hosted_vllm/q", "http://gpu-box.lan:8000", ["gpu-box.lan"])
    assert not is_local_provider("openai/gpt-4o", None)
    assert not is_local_provider("bedrock/anthropic.claude", None)
    assert not is_local_provider("ollama/llama3", "https://ollama.example.com")


def test_withheld_variants_cover_escaped_and_truncated_forms() -> None:
    text = 'He said "hi"\n' + "x" * 500
    variants = withheld_variants([text, "short"])
    assert text in variants and text[:400] in variants
    assert orjson.dumps(text).decode()[1:-1] in variants
    assert "short" not in variants  # too short to match safely
