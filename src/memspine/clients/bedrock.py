"""Bedrock runtime connector. All AWS vocabulary stays here (ADR-085)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from memspine.exceptions import MissingServiceError


class BedrockRequestError(RuntimeError):
    """Credential-safe SDK failure: only the operation and a stable code escape."""


class BedrockTransientError(BedrockRequestError):
    """Retryable throttling, service unavailability or transport interruption."""


async def _sdk_call(call: Callable[..., Any], **kwargs: Any) -> Any:
    try:
        return await asyncio.to_thread(call, **kwargs)
    except Exception as exc:
        response = getattr(exc, "response", {})
        code = (
            response.get("Error", {}).get("Code", type(exc).__name__)
            if isinstance(response, dict)
            else type(exc).__name__
        )
        transient = code in {
            "ThrottlingException",
            "TooManyRequestsException",
            "ServiceUnavailableException",
            "InternalServerException",
            "ModelTimeoutException",
            "ModelNotReadyException",
            "ReadTimeoutError",
            "ConnectTimeoutError",
            "EndpointConnectionError",
            "ConnectionClosedError",
        }
        safe_code = code if isinstance(code, str) and code.isidentifier() else "SDKError"
        error = BedrockTransientError if transient else BedrockRequestError
        raise error(f"Bedrock request failed ({safe_code})") from None


@dataclass(frozen=True, slots=True)
class BedrockSpec:
    region: str | None = None
    timeout_s: float = 60.0


class BedrockConnector:
    kind = "bedrock"
    scope = "thread"

    def __init__(self, spec: BedrockSpec, *, client: Any = None, rerank_client: Any = None) -> None:
        self.spec = spec
        self._client = client
        self._rerank_client = rerank_client

    async def open(self) -> None:
        if self._client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError:
                raise MissingServiceError("bedrock", "aws") from None
            self._client = await asyncio.to_thread(
                boto3.client,
                "bedrock-runtime",
                region_name=self.spec.region,
                config=Config(
                    read_timeout=self.spec.timeout_s, connect_timeout=self.spec.timeout_s
                ),
            )

    def identity(self) -> dict[str, Any]:
        return {"region": self.spec.region}

    def locality(self) -> str:
        return "cluster"

    async def health(self, deadline_s: float | None = None) -> bool:
        return self._client is not None

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await asyncio.to_thread(client.close)
        rerank, self._rerank_client = self._rerank_client, None
        if rerank is not None:
            await asyncio.to_thread(rerank.close)

    async def acompletion(self, **kwargs: Any) -> dict[str, Any]:
        await self.open()
        messages = kwargs["messages"]
        request: dict[str, Any] = {
            "modelId": kwargs["model"],
            "messages": [
                {"role": m["role"], "content": [{"text": m["content"]}]}
                for m in messages
                if m["role"] != "system"
            ],
            "inferenceConfig": {
                dst: kwargs[src]
                for src, dst in [
                    ("temperature", "temperature"),
                    ("top_p", "topP"),
                    ("max_tokens", "maxTokens"),
                ]
                if src in kwargs
            },
        }
        system = [{"text": m["content"]} for m in messages if m["role"] == "system"]
        if system:
            request["system"] = system
        response = await _sdk_call(self._client.converse, **request)
        content = "".join(p.get("text", "") for p in response["output"]["message"]["content"])
        usage = response.get("usage", {})
        return {
            "choices": [
                {
                    "message": {"content": content},
                    "finish_reason": "length"
                    if response.get("stopReason") == "max_tokens"
                    else "stop",
                }
            ],
            "usage": {
                "prompt_tokens": usage.get("inputTokens", 0),
                "completion_tokens": usage.get("outputTokens", 0),
            },
        }

    async def aembedding(self, **kwargs: Any) -> dict[str, Any]:
        await self.open()
        inputs = kwargs["input"]
        model = kwargs["model"]
        data = []
        for index, text in enumerate(inputs):
            if "cohere.embed" in model:
                body = {"texts": [text], "input_type": kwargs.get("input_type", "search_document")}
                if "v4" in model:
                    body["embedding_types"] = ["float"]
                    if "dimensions" in kwargs:
                        body["output_dimension"] = kwargs["dimensions"]
            else:
                body = {"inputText": text}
                if "dimensions" in kwargs:
                    body["dimensions"] = kwargs["dimensions"]
            response = await _sdk_call(
                self._client.invoke_model,
                modelId=model,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(body),
            )
            result = json.loads(await asyncio.to_thread(response["body"].read))
            vector = (
                (
                    result["embeddings"]["float"][0]
                    if isinstance(result.get("embeddings"), dict)
                    else result["embeddings"][0]
                )
                if "embeddings" in result
                else result["embedding"]
            )
            data.append({"index": index, "embedding": vector})
        return {"data": data}

    async def arerank(self, **kwargs: Any) -> dict[str, Any]:
        if self._rerank_client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError:
                raise MissingServiceError("bedrock", "aws") from None
            self._rerank_client = await asyncio.to_thread(
                boto3.client,
                "bedrock-agent-runtime",
                region_name=self.spec.region,
                config=Config(read_timeout=self.spec.timeout_s),
            )
        model = kwargs["model"]
        region = self.spec.region or self._rerank_client.meta.region_name
        arn = (
            model
            if model.startswith("arn:")
            else f"arn:aws:bedrock:{region}::foundation-model/{model}"
        )
        response = await _sdk_call(
            self._rerank_client.rerank,
            queries=[{"type": "TEXT", "textQuery": {"text": kwargs["query"]}}],
            sources=[
                {
                    "type": "INLINE",
                    "inlineDocumentSource": {"type": "TEXT", "textDocument": {"text": text}},
                }
                for text in kwargs["documents"]
            ],
            rerankingConfiguration={
                "type": "BEDROCK_RERANKING_MODEL",
                "bedrockRerankingConfiguration": {
                    "modelConfiguration": {"modelArn": arn},
                    "numberOfResults": kwargs["top_n"],
                },
            },
        )
        return {
            "results": [
                {"index": item["index"], "relevance_score": item["relevanceScore"]}
                for item in response["results"]
            ]
        }
