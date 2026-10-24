"""#48-#50 pure primitives: purpose gate, read scope, audit chain, retention classes,
and the remote-LLM gate's provider classification and text variants."""

from __future__ import annotations

import orjson

from memspine.config.schema import RetentionClassConfig
from memspine.services.llm.tier_gate import is_local_provider, withheld_variants
from memspine.store.records import MemoryRecord
from memspine.trust.privacy import (
    current_purpose,
    matching_class,
    read_scope,
)


def _rec(*purposes: str, ns: str = "u", memory_type: str = "semantic") -> MemoryRecord:
    return MemoryRecord(
        namespace=ns, memory_type=memory_type, content="c", consent_tags=list(purposes)
    )


def test_read_scope_outermost_and_purpose_inheritance() -> None:
    with read_scope("support") as outer:
        assert outer and current_purpose() == "support"
        with read_scope(None) as inner:
            assert not inner and current_purpose() == "support"
        with read_scope("billing"):
            assert current_purpose() == "billing"
        assert current_purpose() == "support"
    assert current_purpose() is None


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
