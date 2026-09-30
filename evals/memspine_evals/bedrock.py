"""AWS Bedrock backends for the harness: Qwen3 (reader/judge/agent) + Cohere Embed v4.

Titan v2 remains available as an alternative embedder.

Credentials come from the repo's ``.env``, but **only the AWS keys are exported**
to the process (boto3/LiteLLM read them from the environment). Nothing else in
``.env`` is loaded — it may hold unrelated production secrets.

Every call goes through a :class:`CallBudget`, a hard cap that raises before the
call is made, so "how many paid calls did this run make" is a checked property
rather than a promise. Token counts are always recorded; dollars only if a price
table is supplied (take it from your AWS Bedrock pricing page — no prices are
hard-coded here, because they change and differ by region).
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import ReaderAnswer
from .readers import DEFAULT_QA_PROMPT

__all__ = [
    "COHERE_EMBED_V4",
    "COHERE_EMBED_V4_DIM",
    "QWEN3_32B",
    "QWEN3_NEXT_80B",
    "TITAN_V2",
    "TITAN_V2_DIM",
    "BudgetExceeded",
    "CallBudget",
    "LiteLLMReader",
    "bedrock_engine_config",
    "litellm_chat",
    "load_aws_credentials",
]

#: LiteLLM model ids, verified on-demand in us-east-1 on 2026-09-30.
QWEN3_32B = "bedrock/converse/qwen.qwen3-32b-v1:0"
QWEN3_NEXT_80B = "bedrock/converse/qwen.qwen3-next-80b-a3b"
#: Default embedder: Cohere v4 requested at 1024 dims (Matryoshka; 256/512/1024/1536
#: are valid, 1536 is the model default). On a 62-question paraphrase test (results/
#: cohere_dim_test.json) 1024 matched 1536 within noise at 2/3 the storage, and it is
#: the same size as Titan v2, so the two are swappable without resizing a store.
COHERE_EMBED_V4 = "bedrock/cohere.embed-v4:0"
COHERE_EMBED_V4_DIM = 1024
TITAN_V2 = "bedrock/amazon.titan-embed-text-v2:0"
TITAN_V2_DIM = 1024

_AWS_KEYS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
_THINK = re.compile(r"<think>.*?</think>", re.S)


def _parse_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_aws_credentials(dotenv_path: str | Path = ".env", override: bool = False) -> str:
    """Export ONLY the AWS credential keys (+ region) from ``dotenv_path``.

    Returns the region. Process env wins unless ``override``. Raises if no
    access key is available from either source.
    """
    path = Path(dotenv_path)
    values = _parse_dotenv(path) if path.exists() else {}
    for key in _AWS_KEYS:
        if values.get(key) and (override or not os.environ.get(key)):
            os.environ[key] = values[key]
    region = (
        os.environ.get("AWS_REGION_NAME")
        or values.get("AWS_MODEL_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-east-1"
    )
    os.environ.setdefault("AWS_REGION_NAME", region)
    os.environ.setdefault("AWS_DEFAULT_REGION", region)
    if not os.environ.get("AWS_ACCESS_KEY_ID"):
        raise RuntimeError(f"no AWS_ACCESS_KEY_ID in the environment or {path}")
    return region


def bedrock_engine_config(
    region: str,
    llm_model: str = QWEN3_32B,
    embed_model: str = COHERE_EMBED_V4,
    embed_dim: int = COHERE_EMBED_V4_DIM,
    roles: tuple[str, ...] = ("extract", "judge", "chat"),
) -> dict[str, Any]:
    """``Engine(**...)`` overrides: Cohere v4 embeddings + Qwen3 for every LLM role.

    Pass ``embed_model=TITAN_V2, embed_dim=TITAN_V2_DIM`` for Titan instead. Changing
    embedder on an existing store needs a rebuild: vectors of different models
    (and dimensions) are not comparable.
    """
    embedding: dict[str, Any] = {
        "provider": "litellm",
        "model": embed_model,
        "dim": embed_dim,
        "aws_region": region,
        "request_dimensions": True,
    }
    if "cohere" in embed_model:
        # asymmetric retrieval: +5 pp R@1 on the same test vs one input type for both
        embedding["query_input_type"] = "search_query"
        embedding["document_input_type"] = "search_document"
    return {
        "embedding": embedding,
        "llm": {"roles": {role: {"model": llm_model, "aws_region": region} for role in roles}},
    }


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class CallBudget:
    """Hard cap on paid calls; checked BEFORE each call."""

    max_calls: int
    prices_per_mtok: Mapping[str, tuple[float, float]] | None = None  # model -> (in, out) $/1M
    calls: int = 0
    tokens: dict[str, list[int]] = field(default_factory=dict)  # model -> [in, out]

    def reserve(self) -> None:
        if self.calls >= self.max_calls:
            raise BudgetExceeded(f"call cap reached ({self.max_calls})")
        self.calls += 1

    def record(self, model: str, prompt_tokens: int, completion_tokens: int) -> None:
        acc = self.tokens.setdefault(model, [0, 0])
        acc[0] += prompt_tokens
        acc[1] += completion_tokens

    def usd(self) -> float | None:
        if self.prices_per_mtok is None:
            return None
        total = 0.0
        for model, (tin, tout) in self.tokens.items():
            pin, pout = self.prices_per_mtok.get(model, (0.0, 0.0))
            total += tin / 1e6 * pin + tout / 1e6 * pout
        return total

    def summary(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "max_calls": self.max_calls,
            "tokens": self.tokens,
            "usd": self.usd(),
        }


class LiteLLMReader:
    """Reader over any LiteLLM model (Bedrock Qwen3 by default), budget-capped."""

    makes_model_calls = True

    def __init__(
        self,
        budget: CallBudget,
        model: str = QWEN3_32B,
        temperature: float = 0.0,
        max_tokens: int = 256,
        prompt: str = DEFAULT_QA_PROMPT,
        reader_id: str | None = None,
    ) -> None:
        import litellm

        litellm.suppress_debug_info = True
        self._litellm = litellm
        self.budget = budget
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.prompt = prompt
        self.reader_id = reader_id or f"litellm:{model}"

    def describe(self) -> Mapping[str, Any]:
        return {
            "reader_id": self.reader_id,
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "prompt_sha256": hashlib.sha256(self.prompt.encode()).hexdigest(),
        }

    async def complete(self, content: str) -> ReaderAnswer:
        self.budget.reserve()
        started = time.perf_counter()
        response = await self._litellm.acompletion(
            model=self.model,
            messages=[{"role": "user", "content": content}],
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        latency = (time.perf_counter() - started) * 1000
        usage = getattr(response, "usage", None)
        pt = int(getattr(usage, "prompt_tokens", 0) or 0)
        ct = int(getattr(usage, "completion_tokens", 0) or 0)
        self.budget.record(self.model, pt, ct)
        choice = response.choices[0]
        text = _THINK.sub("", choice.message.content or "").strip()
        finish = str(getattr(choice, "finish_reason", "") or "")
        return ReaderAnswer(
            text=text,
            prompt_tokens=pt,
            completion_tokens=ct,
            latency_ms=latency,
            model_calls=1,
            truncated=finish == "length",
            finish_reason=finish,
        )

    async def answer(self, question: str, context: str) -> ReaderAnswer:
        return await self.complete(self.prompt.format(context=context, question=question))


def litellm_chat(budget: CallBudget, model: str = QWEN3_32B, temperature: float = 0.0) -> Any:
    """A bare ``async (prompt) -> str`` callable for ``LLMJudge``, budget-capped."""
    reader = LiteLLMReader(budget, model=model, temperature=temperature, max_tokens=16)

    async def chat(prompt: str) -> str:
        return (await reader.complete(prompt)).text

    return chat
