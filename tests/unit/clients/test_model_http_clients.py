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
