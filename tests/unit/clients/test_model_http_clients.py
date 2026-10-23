"""ADR-085 connector request contracts; no model calls or credentials are required."""

from __future__ import annotations

import builtins
import io
import json
import sys
from types import SimpleNamespace

import httpx
import pytest

from memspine.clients.http import HttpConnector, HttpSpec
from memspine.clients.openai_compat import OpenAICompatConnector, OpenAICompatSpec
from memspine.exceptions import MissingServiceError
from memspine.services.llm.openai_compat import OpenAICompatLLM


async def test_cohere_base_with_version_uses_one_version_segment() -> None:
    seen = []

    def respond(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"results": []})

    pool = HttpConnector(HttpSpec(transport=httpx.MockTransport(respond)))
    connector = OpenAICompatConnector(
        OpenAICompatSpec(api_base="https://example.invalid/v2", family="cohere"), http=pool
    )
    try:
        await connector.arerank(model="rerank", query="question", documents=["passage"])
        assert seen == ["/v2/rerank"]
    finally:
        await pool.close()


async def test_native_ollama_schema_no_think_usage_and_timeout() -> None:
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "message": {"content": '<think>x</think>{"ok":true}'},
                "prompt_eval_count": 12,
                "eval_count": 8,
            },
        )

    pool = HttpConnector(HttpSpec(transport=httpx.MockTransport(respond)))
    connector = OpenAICompatConnector(
        OpenAICompatSpec(api_base="http://localhost:11434/v1", family="ollama"), http=pool
    )
    service = OpenAICompatLLM("qwen3:8b", connector=connector, provider="ollama", timeout_seconds=7)
    from memspine.services.llm.capabilities import schema_options

    _, opts = schema_options(service, {"type": "object"}, "reply")
    messages = [{"role": "user", "content": "question"}]
    assert await service.chat(messages, **opts) == '{"ok":true}'
    request = seen[0]
    body = json.loads(request.content)
    assert request.url.path == "/api/chat"
    assert body["format"] == {"type": "object"}
    assert body["think"] is False and body["stream"] is False
    assert body["messages"][-1]["content"] == "question /no_think"
    assert messages[-1]["content"] == "question"
    assert body["options"]["num_predict"] == 2048
    assert request.extensions["timeout"]["read"] == 7
    assert service.last_usage == (12, 8) and service.usage_totals == [12, 8]
    assert await connector.health()
    assert len(seen) == 1
    await pool.close()


async def test_embedding_dimensions_and_rerank_dialect(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_KEY", "test-secret")
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={"data": [{"embedding": [1.0, 2.0]}]}
            if request.url.path.endswith("embeddings")
            else {"results": []},
        )

    pool = HttpConnector(HttpSpec(transport=httpx.MockTransport(respond)))
    connector = OpenAICompatConnector(
        OpenAICompatSpec(
            api_base="http://localhost:8000/v1", family="vllm", api_key_env="MODEL_KEY"
        ),
        http=pool,
    )
    await connector.aembedding(model="embedding-model", input=["x"], dimensions=2, timeout=3)
    await connector.arerank(model="ranker", query="q", documents=["x"])
    assert seen[0].url.path == "/v1/embeddings" and seen[1].url.path == "/v1/rerank"
    assert json.loads(seen[0].content)["dimensions"] == 2
    assert seen[0].headers["authorization"] == "Bearer test-secret"
    assert "test-secret" not in repr(connector.identity())
    assert "test-secret" not in repr(connector.spec)
    await pool.close()


@pytest.mark.parametrize(
    "url", ["http://user:password@host/v1", "http://host/v1?token=secret", "ftp://host"]
)
def test_endpoint_rejects_embedded_credentials(url) -> None:
    with pytest.raises(ValueError):
        OpenAICompatConnector(OpenAICompatSpec(api_base=url))


async def test_error_body_and_credentials_never_escape() -> None:
    pool = HttpConnector(
        HttpSpec(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(401, text="secret-key in vendor error")
            )
        )
    )
    connector = OpenAICompatConnector(
        OpenAICompatSpec(api_base="https://remote.example/v1", api_key="secret-key"), http=pool
    )
    with pytest.raises(RuntimeError, match="HTTP 401") as error:
        await connector.acompletion(model="x", messages=[])
    assert "secret-key" not in str(error.value)
    assert connector.locality() == "cluster"
    await pool.close()


async def test_anyllm_fake_and_lazy_import(monkeypatch) -> None:
    from memspine.clients.anyllm import AnyLLMConnector

    seen = {}

    class Provider:
        async def acompletion(self, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(model_dump=lambda: {"choices": [{"message": {"content": "ok"}}]})

    class Factory:
        @staticmethod
        def create(provider, **kwargs):
            seen["upstream"] = provider
            return Provider()

    monkeypatch.setitem(sys.modules, "any_llm", SimpleNamespace(AnyLLM=Factory))
    connector = AnyLLMConnector("anthropic")
    assert seen == {}
    assert (await connector.acompletion(model="claude", messages=[]))["choices"][0]["message"][
        "content"
    ] == "ok"
    assert seen["upstream"] == "anthropic"


async def test_anyllm_missing_extra_names_install(monkeypatch) -> None:
    from memspine.clients.anyllm import AnyLLMConnector

    original = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "any_llm":
            raise ImportError("missing")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    with pytest.raises(MissingServiceError, match=r"memspine\[anyllm\]"):
        await AnyLLMConnector("anthropic").open()


async def test_bedrock_converse_stubber() -> None:
    boto3 = pytest.importorskip("boto3")
    from botocore.stub import Stubber

    from memspine.clients.bedrock import BedrockConnector, BedrockSpec

    client = boto3.client(
        "bedrock-runtime",
        region_name="us-east-1",
        aws_access_key_id="fake",
        aws_secret_access_key="fake",
    )
    expected = {
        "modelId": "qwen.qwen3-32b-v1:0",
        "messages": [{"role": "user", "content": [{"text": "question"}]}],
        "system": [{"text": "system"}],
        "inferenceConfig": {"temperature": 0.2, "topP": 0.9, "maxTokens": 8},
    }
    response = {
        "output": {"message": {"role": "assistant", "content": [{"text": "answer"}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 3, "outputTokens": 2, "totalTokens": 5},
        "metrics": {"latencyMs": 1},
    }
    with Stubber(client) as stub:
        stub.add_response("converse", response, expected)
        connector = BedrockConnector(BedrockSpec("us-east-1"), client=client)
        result = await connector.acompletion(
            model="qwen.qwen3-32b-v1:0",
            messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "question"},
            ],
            temperature=0.2,
            top_p=0.9,
            max_tokens=8,
        )
        assert result["choices"][0]["message"]["content"] == "answer"
        assert result["usage"] == {"prompt_tokens": 3, "completion_tokens": 2}
        stub.assert_no_pending_responses()
    await connector.close()


async def test_bedrock_cohere_embedding_stubber() -> None:
    boto3 = pytest.importorskip("boto3")
    from botocore.response import StreamingBody
    from botocore.stub import Stubber

    from memspine.clients.bedrock import BedrockConnector, BedrockSpec

    client = boto3.client(
        "bedrock-runtime",
        region_name="us-east-1",
        aws_access_key_id="fake",
        aws_secret_access_key="fake",
    )
    body = json.dumps(
        {
            "texts": ["query"],
            "input_type": "search_query",
            "embedding_types": ["float"],
            "output_dimension": 256,
        }
    )
    blob = json.dumps({"embeddings": {"float": [[0.1] * 256]}}).encode()
    with Stubber(client) as stub:
        stub.add_response(
            "invoke_model",
            {"body": StreamingBody(io.BytesIO(blob), len(blob)), "contentType": "application/json"},
            {
                "modelId": "cohere.embed-v4:0",
                "contentType": "application/json",
                "accept": "application/json",
                "body": body,
            },
        )
        connector = BedrockConnector(BedrockSpec("us-east-1"), client=client)
        result = await connector.aembedding(
            model="cohere.embed-v4:0", input=["query"], dimensions=256, input_type="search_query"
        )
        assert result["data"][0]["embedding"] == [0.1] * 256
        stub.assert_no_pending_responses()
    await connector.close()


async def test_bedrock_rerank_stubber() -> None:
    boto3 = pytest.importorskip("boto3")
    from botocore.stub import Stubber

    from memspine.clients.bedrock import BedrockConnector, BedrockSpec

    client = boto3.client(
        "bedrock-agent-runtime",
        region_name="us-east-1",
        aws_access_key_id="fake",
        aws_secret_access_key="fake",
    )
    expected = {
        "queries": [{"type": "TEXT", "textQuery": {"text": "q"}}],
        "sources": [
            {
                "type": "INLINE",
                "inlineDocumentSource": {"type": "TEXT", "textDocument": {"text": "doc"}},
            }
        ],
        "rerankingConfiguration": {
            "type": "BEDROCK_RERANKING_MODEL",
            "bedrockRerankingConfiguration": {
                "modelConfiguration": {
                    "modelArn": "arn:aws:bedrock:us-east-1::foundation-model/amazon.rerank-v1:0"
                },
                "numberOfResults": 1,
            },
        },
    }
    with Stubber(client) as stub:
        stub.add_response("rerank", {"results": [{"index": 0, "relevanceScore": 0.75}]}, expected)
        connector = BedrockConnector(BedrockSpec("us-east-1"), rerank_client=client)
        result = await connector.arerank(
            model="amazon.rerank-v1:0", query="q", documents=["doc"], top_n=1
        )
        assert result["results"] == [{"index": 0, "relevance_score": 0.75}]
        stub.assert_no_pending_responses()
    await connector.close()


async def test_bedrock_throttling_is_retryable_and_auth_errors_are_redacted() -> None:
    boto3 = pytest.importorskip("boto3")
    from botocore.stub import Stubber

    from memspine.clients.bedrock import (
        BedrockConnector,
        BedrockRequestError,
        BedrockSpec,
        BedrockTransientError,
    )

    client = boto3.client(
        "bedrock-runtime",
        region_name="us-east-1",
        aws_access_key_id="fake",
        aws_secret_access_key="fake",
    )
    connector = BedrockConnector(BedrockSpec("us-east-1"), client=client)
    with Stubber(client) as stub:
        stub.add_client_error(
            "converse",
            service_error_code="ThrottlingException",
            service_message="secret-vendor-detail",
            http_status_code=429,
        )
        with pytest.raises(BedrockTransientError) as failure:
            await connector.acompletion(model="test", messages=[])
        assert "secret-vendor-detail" not in str(failure.value)
        stub.add_client_error(
            "converse",
            service_error_code="AccessDeniedException",
            service_message="secret-vendor-detail",
            http_status_code=403,
        )
        with pytest.raises(BedrockRequestError) as failure:
            await connector.acompletion(model="test", messages=[])
        assert not isinstance(failure.value, BedrockTransientError)
        assert "secret-vendor-detail" not in str(failure.value)
    await connector.close()


@pytest.mark.parametrize(
    ("preset", "base"),
    [
        ("ollama", "http://localhost:11434"),
        ("vllm", "http://localhost:8000/v1"),
        ("lmstudio", "http://localhost:1234/v1"),
        ("llamacpp_server", "http://localhost:8080/v1"),
    ],
)
async def test_embedding_presets_have_typed_endpoints_and_dimensions(
    preset, base, monkeypatch
) -> None:
    from scripts.model_transport_stub import install_transport, transport_stub

    from memspine.providers.registry import get_registry, validate_settings

    install_transport(monkeypatch)

    async def response(**kwargs):
        return SimpleNamespace(data=[{"index": 0, "embedding": [0.5, 0.25]}])

    monkeypatch.setattr(transport_stub, "aembedding", response)
    descriptor = get_registry("embedders").get(preset)
    settings = validate_settings(
        descriptor, {"model": "embedding-model", "dim": 2, "request_timeout_s": 3.0}
    )
    service = (await descriptor.builder.build(settings, None)).service
    assert service._connector.spec.api_base == base
    assert service._connector.spec.family == preset
    assert service.embedder_id.startswith(f"{preset}:embedding-model#")
    assert await service.embed(["x"]) == [[0.5, 0.25]]
    await service.aclose()


async def test_vllm_reranker_preset_and_unsupported_ollama() -> None:
    from memspine.exceptions import ConfigError
    from memspine.providers.registry import get_registry, validate_settings

    descriptor = get_registry("rerankers").get("vllm")
    service = (
        await descriptor.builder.build(
            validate_settings(descriptor, {"model": "ranker", "request_timeout_s": 3.0}), None
        )
    ).service
    assert service._connector.spec.family == "vllm"
    assert service.reranker_id == "vllm:ranker"
    await service.aclose()
    descriptor = get_registry("rerankers").get("ollama")
    with pytest.raises(ConfigError, match="does not expose a rerank API"):
        await descriptor.builder.build(
            validate_settings(descriptor, {"model": "ranker", "request_timeout_s": 3.0}), None
        )


async def test_cohere_rerank_path_and_explicit_override() -> None:
    seen = []

    def respond(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"results": []})

    pool = HttpConnector(HttpSpec(transport=httpx.MockTransport(respond)))
    connector = OpenAICompatConnector(
        OpenAICompatSpec(api_base="https://cohere.example", family="cohere"), http=pool
    )
    await connector.arerank(model="ranker", query="q", documents=[])
    connector = OpenAICompatConnector(
        OpenAICompatSpec(api_base="https://jina.example", rerank_path="/custom/rerank"), http=pool
    )
    await connector.arerank(model="ranker", query="q", documents=[])
    assert seen == ["/v2/rerank", "/custom/rerank"]
    await pool.close()


async def test_anyllm_closes_owned_provider_sdk_and_uses_wall_clock_for_unsupported_timeout(
    monkeypatch,
) -> None:
    from memspine.clients.anyllm import AnyLLMConnector

    seen = {}

    class SDK:
        async def close(self):
            seen["closed"] = True

    class Provider:
        TIMEOUT_SUPPORT = "unsupported"
        client = SDK()

        async def acompletion(self, **kwargs):
            assert "timeout" not in kwargs
            return SimpleNamespace(model_dump=lambda: {"choices": []})

    class Factory:
        @staticmethod
        def create(*args, **kwargs):
            assert kwargs["unified_exceptions"] is True
            return Provider()

    monkeypatch.setitem(sys.modules, "any_llm", SimpleNamespace(AnyLLM=Factory))
    connector = AnyLLMConnector("fake")
    await connector.acompletion(model="x", messages=[], timeout=3)
    await connector.close()
    await connector.close()
    assert seen["closed"]


async def test_anyllm_embedding_sdk_query_dimensions_and_descriptor_identity(monkeypatch):
    from memspine.providers.registry import get_registry, validate_settings

    seen = []

    class Provider:
        TIMEOUT_SUPPORT = "native"

        async def aembedding(self, **kwargs):
            seen.append(kwargs)
            return SimpleNamespace(
                model_dump=lambda: {"data": [{"index": 0, "embedding": [0.1, 0.2]}]}
            )

    class Factory:
        @staticmethod
        def create(*args, **kwargs):
            return Provider()

    monkeypatch.setitem(sys.modules, "any_llm", SimpleNamespace(AnyLLM=Factory))
    descriptor = get_registry("embedders").get("anyllm")
    settings = validate_settings(
        descriptor,
        {
            "model": "embed-model",
            "upstream_provider": "cohere",
            "dim": 2,
            "request_dimensions": True,
            "query_input_type": "search_query",
            "document_input_type": "search_document",
            "request_timeout_s": 1.0,
        },
    )
    service = (await descriptor.builder.build(settings, None)).service
    assert await service.embed_queries(["query"]) == [[0.1, 0.2]]
    assert await service.embed(["document"]) == [[0.1, 0.2]]
    assert (
        seen[0]["inputs"] == ["query"]
        and seen[0]["dimensions"] == 2
        and seen[0]["input_type"] == "search_query"
    )
    assert seen[1]["input_type"] == "search_document"
    assert service.embedder_id.startswith("anyllm:cohere:embed-model#")
    assert descriptor.target_identity(settings)["embedder_id"] == service.embedder_id
    other = settings.model_copy(update={"upstream_provider": "voyage"})
    assert descriptor.target_identity(other)["embedder_id"] != service.embedder_id
    await service.aclose()


async def test_anyllm_embedding_hard_timeout_cancels_sdk_call(monkeypatch):
    import asyncio

    from memspine.clients.anyllm import AnyLLMConnector
    from memspine.services.embedding import openai_compat as adapter
    from memspine.services.embedding.openai_compat import OpenAICompatEmbedding

    cancelled = []

    class Provider:
        TIMEOUT_SUPPORT = "unsupported"

        async def aembedding(self, **kwargs):
            assert "timeout" not in kwargs
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

    class Factory:
        @staticmethod
        def create(*args, **kwargs):
            return Provider()

    async def single_attempt(call, **kwargs):
        return await call()

    monkeypatch.setitem(sys.modules, "any_llm", SimpleNamespace(AnyLLM=Factory))
    monkeypatch.setattr(adapter, "retry_transient", single_attempt)
    service = OpenAICompatEmbedding(
        "embed-model",
        2,
        provider="anyllm",
        connector=AnyLLMConnector("cohere"),
        request_timeout_s=0.01,
    )
    with pytest.raises(TimeoutError):
        await service.embed(["query"])
    assert cancelled == [True]
    await service.aclose()


@pytest.mark.parametrize(
    "preset", ["openai_compatible", "ollama", "vllm", "lmstudio", "llamacpp_server"]
)
async def test_remote_embedding_endpoint_and_family_change_vector_identity(preset):
    from memspine.providers.registry import get_registry, validate_settings

    descriptor = get_registry("embedders").get(preset)
    raw = {
        "model": "embed-model",
        "dim": 2,
        "request_timeout_s": 1.0,
        "api_base": "https://one.example/v1",
    }
    settings = validate_settings(descriptor, raw)
    service = (await descriptor.builder.build(settings, None)).service
    identity = descriptor.target_identity(settings)["embedder_id"]
    assert service.embedder_id == identity
    changed = settings.model_copy(update={"api_base": "https://two.example/v1"})
    assert descriptor.target_identity(changed)["embedder_id"] != identity
    normalized = settings.model_copy(update={"api_base": "https://ONE.example/v1/"})
    assert descriptor.target_identity(normalized)["embedder_id"] == identity
    if preset == "openai_compatible":
        changed = settings.model_copy(update={"family": "ollama"})
        assert descriptor.target_identity(changed)["embedder_id"] != identity
    await service.aclose()
