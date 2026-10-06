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

import os
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .contracts import ReaderAnswer
from .readers import (
    ANSWER_EXTRACTOR_VERSION,
    DEFAULT_QA_PROMPT,
    RoutedQAPrompt,
    final_answer,
    prompt_describe,
    prompt_variant,
)
from .runner import ModelCallBudgetExceeded

__all__ = [
    "COHERE_EMBED_V4",
    "COHERE_EMBED_V4_DIM",
    "ENGINE_LLM_ROLES",
    "MEMSPINE_LLM_CHOICES",
    "QWEN3_32B",
    "QWEN3_NEXT_80B",
    "TITAN_V2",
    "TITAN_V2_DIM",
    "BudgetExceeded",
    "CallBudget",
    "LiteLLMReader",
    "aws_region_from_env",
    "bedrock_engine_config",
    "cached_prompt_tokens",
    "engine_llm_config",
    "estimate_prompt_tokens",
    "litellm_chat",
    "load_aws_credentials",
    "merge_engine_llm_roles",
    "parse_price",
    "parse_service_price",
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

#: Every LLM role the engine binds through ``Engine.llm`` / ``LLMRouter.for_role``:
#: fact mining and LLM entity extraction, graph edges, consolidation summaries,
#: profile reflection, anticipatory cues, the H17 relevance filter, P4 compose
#: rewrites, the G2a read planner, conflict adjudication, and chat.
ENGINE_LLM_ROLES: tuple[str, ...] = (
    "extract",
    "extract_edges",
    "summarize",
    "reflect",
    "anticipate",
    "relevance",
    "query_rewrite",
    "plan",
    "judge",
    "chat",
)

#: ``c0-1 --memspine-llm`` choices: which model the memspine arm's engine roles use.
MEMSPINE_LLM_CHOICES: dict[str, str | None] = {"none": None, "bedrock-qwen3": QWEN3_32B}

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


def aws_region_from_env(default: str = "us-east-1") -> str:
    """The AWS region the process is configured for (what ``load_aws_credentials`` set)."""
    return os.environ.get("AWS_REGION_NAME") or os.environ.get("AWS_DEFAULT_REGION") or default


def engine_llm_config(
    model: str, region: str, roles: tuple[str, ...] = ENGINE_LLM_ROLES
) -> dict[str, Any]:
    """``llm`` config binding every engine role to one LiteLLM model in ``region``."""
    return {"roles": {role: {"model": model, "aws_region": region} for role in roles}}


def merge_engine_llm_roles(
    memspine_config: Mapping[str, Any] | None,
    model: str,
    region: str,
    roles: tuple[str, ...] = ENGINE_LLM_ROLES,
) -> dict[str, Any]:
    """A copy of ``memspine_config`` with every missing engine role bound to ``model``.

    Roles the caller already bound are kept exactly as given: an explicit role
    always wins over the run-wide default.
    """
    merged = dict(memspine_config or {})
    llm = dict(merged.get("llm") or {})
    bound = dict(llm.get("roles") or {})
    for role, binding in engine_llm_config(model, region, roles)["roles"].items():
        bound.setdefault(role, binding)
    llm["roles"] = bound
    merged["llm"] = llm
    return merged


def parse_price(spec: str) -> tuple[str, float, float]:
    """``"model=IN,OUT"`` (USD per 1M input / output tokens) -> (model, in, out)."""
    model, sep, prices = spec.rpartition("=")
    parts = prices.split(",")
    if not sep or not model.strip() or len(parts) != 2:
        raise ValueError(f"--price takes model=IN_PER_MTOK,OUT_PER_MTOK, got {spec!r}")
    try:
        p_in, p_out = float(parts[0]), float(parts[1])
    except ValueError as exc:
        raise ValueError(f"--price prices must be numbers, got {spec!r}") from exc
    if p_in < 0 or p_out < 0:
        raise ValueError(f"--price prices must be >= 0, got {spec!r}")
    return model.strip(), p_in, p_out


#: C-6: the billing unit of each priced service: embeddings per 1M input tokens,
#: reranking per 1,000 searches.
SERVICE_UNITS: dict[str, float] = {"embed": 1e6, "rerank": 1e3}


def parse_service_price(spec: str) -> tuple[str, str, float] | None:
    """``"embed:MODEL=USD_PER_MTOK"`` or ``"rerank:MODEL=USD_PER_1K_SEARCHES"`` ->
    (kind, model, price); None when ``spec`` is an ordinary ``model=IN,OUT`` price."""
    kind, sep, rest = spec.partition(":")
    if not sep or kind not in SERVICE_UNITS:
        return None
    model, eq, price = rest.rpartition("=")
    if not eq or not model.strip():
        raise ValueError(f"--price takes {kind}:MODEL=PRICE, got {spec!r}")
    try:
        value = float(price)
    except ValueError as exc:
        raise ValueError(f"--price {kind} price must be one number, got {spec!r}") from exc
    if value < 0:
        raise ValueError(f"--price prices must be >= 0, got {spec!r}")
    return kind, model.strip(), value


def estimate_prompt_tokens(messages: list[dict[str, str]]) -> int:
    """A conservative prompt-size estimate for the pre-call dollar check.

    Three characters per token (real tokenizers average about four on English)
    plus a per-message allowance for the chat template, so the estimate errs high.
    """
    chars = sum(len(str(m.get("content", ""))) for m in messages)
    return chars // 3 + 1 + 8 * len(messages)


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


class BudgetExceeded(ModelCallBudgetExceeded):
    """The provider call budget is spent (R3-3).

    A ``ModelCallBudgetExceeded``, so the runner stops the arm cleanly with UNATTEMPTED
    rows instead of recording every later question as an ERROR row.
    """


@dataclass
class CallBudget:
    """Hard caps on paid calls and dollars; checked BEFORE each harness call.

    ``max_usd`` (G3) is checked with a conservative estimate of the call about to
    be made (its prompt tokens plus its full ``max_tokens``), so a capped run stops
    before the call that would cross the cap. Engine-side calls are only known
    after they happen: they are charged as observed (``charge``), and the cap then
    stops the next call (``check_usd``).
    """

    max_calls: int
    prices_per_mtok: Mapping[str, tuple[float, float]] | None = None  # model -> (in, out) $/1M
    max_usd: float | None = None
    calls: int = 0
    engine_calls: int = 0
    tokens: dict[str, list[int]] = field(default_factory=dict)  # model -> [in, out]
    cached: dict[str, int] = field(default_factory=dict)  # C9': model -> cache-read input
    #: C-6: ``"embed:<model>"`` -> USD per 1M input tokens, ``"rerank:<model>"`` -> USD
    #: per 1,000 searches (one rerank request is one search)
    service_prices: Mapping[str, float] | None = None
    #: C-6: ``"embed:<model>"`` -> tokens, ``"rerank:<model>"`` -> searches, as charged
    services: dict[str, float] = field(default_factory=dict)

    def price(self, model: str) -> tuple[float, float]:
        return (self.prices_per_mtok or {}).get(model, (0.0, 0.0))

    def estimate_usd(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        p_in, p_out = self.price(model)
        return prompt_tokens / 1e6 * p_in + completion_tokens / 1e6 * p_out

    def service_usd(self) -> float:
        """Dollars of the embedding and rerank units charged so far."""
        prices = self.service_prices or {}
        return sum(
            units / SERVICE_UNITS[key.split(":", 1)[0]] * prices.get(key, 0.0)
            for key, units in self.services.items()
        )

    def spent_usd(self) -> float:
        tokens = sum(self.estimate_usd(m, t[0], t[1]) for m, t in self.tokens.items())
        return tokens + self.service_usd()

    def charge_service(self, kind: str, model: str, units: float) -> None:
        """C-6: charge observed embedding tokens or rerank searches. Never raises."""
        if kind not in SERVICE_UNITS:
            raise ValueError(f"unknown service kind {kind!r}; known: {sorted(SERVICE_UNITS)}")
        if units:
            key = f"{kind}:{model}"
            self.services[key] = self.services.get(key, 0.0) + float(units)

    def check_usd(self, estimate: float = 0.0, what: str = "the next call") -> None:
        """Raise ``BudgetExceeded`` if spending ``estimate`` more would cross ``max_usd``."""
        if self.max_usd is None:
            return
        spent = self.spent_usd()
        if spent >= self.max_usd or spent + estimate > self.max_usd:
            raise BudgetExceeded(
                f"dollar cap: spent ${spent:.4f}, {what} may cost up to ${estimate:.4f}, "
                f"cap ${self.max_usd:.4f}"
            )

    def reserve(
        self, model: str | None = None, prompt_tokens: int = 0, max_completion_tokens: int = 0
    ) -> None:
        """Take one call from the budget; with ``model``, check its worst-case dollars too."""
        if self.calls >= self.max_calls:
            raise BudgetExceeded(f"call cap reached ({self.max_calls})")
        if model is not None:
            estimate = self.estimate_usd(model, prompt_tokens, max_completion_tokens)
            self.check_usd(estimate, what=f"a {model} call")
        self.calls += 1

    def charge(
        self, model: str, prompt_tokens: int, completion_tokens: int, calls: int = 0
    ) -> None:
        """Charge spend observed after the fact (engine-side calls). Never raises."""
        self.calls += calls
        self.engine_calls += calls
        self.record(model, prompt_tokens, completion_tokens)

    def record(
        self, model: str, prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0
    ) -> None:
        acc = self.tokens.setdefault(model, [0, 0])
        acc[0] += prompt_tokens
        acc[1] += completion_tokens
        if cached_tokens:
            self.cached[model] = self.cached.get(model, 0) + cached_tokens

    def usd(self) -> float | None:
        if self.prices_per_mtok is None and not self.service_prices:
            return None
        return self.spent_usd()

    def summary(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "engine_calls": self.engine_calls,
            "max_calls": self.max_calls,
            "max_usd": self.max_usd,
            "tokens": self.tokens,
            "cached_input_tokens": self.cached,
            "services": dict(self.services),
            "services_usd": self.service_usd(),
            "usd": self.usd(),
        }


async def _retry_transient(
    call: Callable[[], Awaitable[Any]],
    *,
    what: str,
    attempts: int = 5,
    base_delay: float = 1.0,
) -> Any:
    """The engine's transient-error retry (``memspine.services._retry``), or one plain
    attempt when the engine is not installed."""
    try:
        from memspine.services._retry import retry_transient
    except ImportError:  # pragma: no cover - bare harness environment
        return await call()
    return await retry_transient(call, what=what, attempts=attempts, base_delay=base_delay)


def cached_prompt_tokens(usage: Any) -> int:
    """C9': cache-served input tokens from a LiteLLM usage block, 0 if unreported.

    OpenAI-style providers report ``prompt_tokens_details.cached_tokens``;
    Anthropic/Bedrock-style ones ``cache_read_input_tokens``.
    """
    if usage is None:
        return 0
    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", None) if details is not None else None
    if not cached:
        cached = getattr(usage, "cache_read_input_tokens", None)
    try:
        return int(cached or 0)
    except (TypeError, ValueError):
        return 0


class LiteLLMReader:
    """Reader over any LiteLLM model (Bedrock Qwen3 by default), budget-capped."""

    makes_model_calls = True

    def __init__(
        self,
        budget: CallBudget,
        model: str = QWEN3_32B,
        temperature: float = 0.0,
        max_tokens: int = 256,
        prompt: str | RoutedQAPrompt = DEFAULT_QA_PROMPT,
        reader_id: str | None = None,
        no_think: bool | None = None,
        retry_attempts: int = 5,
        retry_base_delay: float = 1.0,
        extract_answer: bool = False,
    ) -> None:
        import litellm

        #: #34: ``answer`` keeps only the final answer of a reasoning prompt.
        self.extract_answer = extract_answer

        self.retry_attempts = max(1, int(retry_attempts))
        self.retry_base_delay = retry_base_delay

        litellm.suppress_debug_info = True
        self._litellm = litellm
        self.budget = budget
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.prompt = prompt
        self.reader_id = reader_id or f"litellm:{model}"
        # Qwen3 thinks by default and AWS documents no switch; Qwen's model card
        # documents the /no_think soft switch. Default ON for qwen3 models, so a
        # reader's token budget is not spent on hidden reasoning.
        self.no_think = ("qwen3" in model.lower()) if no_think is None else no_think

    def describe(self) -> Mapping[str, Any]:
        return {
            "reader_id": self.reader_id,
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "no_think": self.no_think,
            **prompt_describe(self.prompt),
            **(
                {"extract_answer": True, "answer_extractor": ANSWER_EXTRACTOR_VERSION}
                if self.extract_answer
                else {}
            ),
        }

    async def complete(self, content: str, system: str | None = None) -> ReaderAnswer:
        """One completion. ``system``, when given, is sent as a system message first."""
        if self.no_think:
            content = f"{content} /no_think"
        messages = [{"role": "user", "content": content}]
        if system is not None:
            messages.insert(0, {"role": "system", "content": system})
        # G3: refused before the call if its worst case (prompt + max_tokens) crosses the cap.
        self.budget.reserve(self.model, estimate_prompt_tokens(messages), self.max_tokens)
        started = time.perf_counter()
        # C-3: a dropped connection or throttling is retried with backoff (one budget
        # reservation covers the retries: a failed attempt returned no tokens); an
        # expired or invalid credential is not, and the runner stops the run on it.
        response = await _retry_transient(
            lambda: self._litellm.acompletion(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            ),
            what=f"{self.reader_id}.complete",
            attempts=self.retry_attempts,
            base_delay=self.retry_base_delay,
        )
        latency = (time.perf_counter() - started) * 1000
        usage = getattr(response, "usage", None)
        pt = int(getattr(usage, "prompt_tokens", 0) or 0)
        ct = int(getattr(usage, "completion_tokens", 0) or 0)
        cached = cached_prompt_tokens(usage)
        self.budget.record(self.model, pt, ct, cached)
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
            cached_prompt_tokens=cached,
        )

    async def answer(
        self, question: str, context: str, question_date: str | None = None
    ) -> ReaderAnswer:
        reply = await self.complete(
            self.prompt.format(
                context=context, question=question, question_date=question_date or "unknown"
            )
        )
        variant = prompt_variant(self.prompt, question)
        if variant is not None:
            reply = replace(reply, prompt_variant=variant)
        if not self.extract_answer:
            return reply
        return replace(reply, text=final_answer(reply.text), raw_text=reply.text)


def litellm_chat(
    budget: CallBudget, model: str = QWEN3_32B, temperature: float = 0.0, max_tokens: int = 96
) -> Any:
    """A bare ``async (prompt) -> str`` callable for ``LLMJudge``, budget-capped."""
    # 96 tokens: the LoCoMo judge format emits a JSON label; 16 truncated it.
    reader = LiteLLMReader(budget, model=model, temperature=temperature, max_tokens=max_tokens)

    async def chat(prompt: str, system: str | None = None) -> str:
        return (await reader.complete(prompt, system=system)).text

    # R3-11: the judge records these in its spec.
    chat.params = {  # type: ignore[attr-defined]
        "endpoint": "litellm",
        "temperature": temperature,
        "max_tokens": max_tokens,
        "no_think": reader.no_think,
    }
    return chat
