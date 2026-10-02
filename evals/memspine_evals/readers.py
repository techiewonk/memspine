"""Readers: the $\\mathcal{G}$ stage, held fixed across systems in a run.

The framework's own finding is that generation is the field's least-engineered
stage. The harness treats it as a single, declared, swappable backbone — and
records which one ran, because Mastra's published pair (84.23 on ``gpt-4o``,
94.87 on ``gpt-5-mini``, same harness, same judge, same dataset) shows the
backbone alone moving a headline by 10.64 points.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from .contracts import ReaderAnswer
from .tokens import HeuristicTokenCounter, TokenCounter

DEFAULT_QA_PROMPT = (
    "Answer the question using only the context below. "
    "If the context does not contain the answer, say you do not know.\n\n"
    "Context:\n{context}\n\nQuestion: {question}\nAnswer:"
)

#: H12: date-aware QA prompt. Context lines carry their dates; the reader is told
#: to compute relative times from the line's own date and to answer briefly.
DATED_QA_PROMPT = (
    "Answer the question using only the context below. Each line starts with the date it "
    'was said, and phrases like "last Friday [= Fri 2023-07-14]" show the absolute date. '
    "When a question asks when something happened, give the date it happened, computed from "
    "the line's date, not the date of the conversation. Answer in one short sentence. If the "
    "context does not contain the answer, say you do not know.\n\n"
    "Context:\n{context}\n\nQuestion: {question}\nAnswer:"
)

#: H7: abstention-aware variant for adversarial questions (LoCoMo cat 5): answer only
#: what the context states about the person asked about.
ABSTAIN_QA_PROMPT = DATED_QA_PROMPT.replace(
    "If the context does not contain the answer, say you do not know.",
    "Answer only what the context states about the person the question names; if the "
    'context attributes it to someone else, or does not state it, reply "Not mentioned".',
)

QA_PROMPTS = {"default": DEFAULT_QA_PROMPT, "dated": DATED_QA_PROMPT, "abstain": ABSTAIN_QA_PROMPT}


class ContextOnlyReader:
    """No generation at all: the 'answer' is the retrieved context.

    Used for retrieval-only measurements — which is what MemPalace's 96.6
    actually is, and what R@k means. Runs using it are marked inadmissible for
    answer metrics by ``RunManifest.missing_protocol_fields`` (``reader.model``
    reads ``none``), so a retrieval number can never be mistaken for a QA one.
    """

    reader_id = "context-only"
    model = "none"
    makes_model_calls = False

    def __init__(self, counter: TokenCounter | None = None) -> None:
        self._counter = counter or HeuristicTokenCounter()

    def describe(self) -> Mapping[str, Any]:
        return {"reader_id": self.reader_id, "model": self.model, "generation": "none"}

    async def answer(self, question: str, context: str) -> ReaderAnswer:
        return ReaderAnswer(
            text=context,
            prompt_tokens=self._counter.count(context),
            completion_tokens=0,
            latency_ms=0.0,
            model_calls=0,
        )


class ScriptedReader:
    """Replays recorded answers keyed by question. Zero model calls.

    This is how the harness is tested end-to-end without a backend, and how a
    recorded run can be re-scored under a different judge without paying for
    generation twice.
    """

    reader_id = "scripted"
    model = "none"
    makes_model_calls = False

    def __init__(self, answers: Mapping[str, str], default: str = "") -> None:
        self._answers = dict(answers)
        self._default = default
        self._counter = HeuristicTokenCounter()

    def describe(self) -> Mapping[str, Any]:
        return {"reader_id": self.reader_id, "model": self.model, "n_scripted": len(self._answers)}

    async def answer(self, question: str, context: str) -> ReaderAnswer:
        text = self._answers.get(question, self._default)
        return ReaderAnswer(
            text=text,
            prompt_tokens=self._counter.count(context),
            completion_tokens=self._counter.count(text),
            latency_ms=0.0,
            model_calls=0,
        )


class OpenAICompatReader:
    """Any OpenAI-compatible ``/v1/chat/completions`` endpoint.

    Covers Ollama, vLLM, llama.cpp, LM Studio and the hosted APIs, which is the
    same surface memspine's own LLM service targets (D-39). Nothing is imported
    until the reader is constructed, so the harness core stays dependency-free.
    """

    makes_model_calls = True

    def __init__(
        self,
        model: str,
        base_url: str = "http://localhost:11434/v1",
        api_key: str = "not-needed",
        temperature: float = 0.0,
        max_tokens: int = 512,
        timeout: float = 120.0,
        prompt: str = DEFAULT_QA_PROMPT,
        reader_id: str | None = None,
    ) -> None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - needs the dependency
            raise RuntimeError("OpenAICompatReader needs httpx — `uv pip install httpx`") from exc
        self._httpx = httpx
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.reader_id = reader_id or f"openai-compat:{model}"
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.prompt = prompt
        self._headers = {"Authorization": f"Bearer {api_key}"}

    def describe(self) -> Mapping[str, Any]:
        return {
            "reader_id": self.reader_id,
            "model": self.model,
            "base_url": self.base_url,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "prompt_sha256": __import__("hashlib").sha256(self.prompt.encode()).hexdigest(),
        }

    async def answer(self, question: str, context: str) -> ReaderAnswer:
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": self.prompt.format(context=context, question=question),
                }
            ],
        }
        started = time.perf_counter()
        async with self._httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions", json=payload, headers=self._headers
            )
            response.raise_for_status()
            body = response.json()
        latency = (time.perf_counter() - started) * 1000
        usage = body.get("usage") or {}
        choice = body["choices"][0]
        finish = str(choice.get("finish_reason") or "")
        return ReaderAnswer(
            text=choice["message"]["content"].strip(),
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            latency_ms=latency,
            model_calls=1,
            truncated=finish == "length",
            finish_reason=finish,
        )


def openai_compat_chat(
    model: str,
    base_url: str = "http://localhost:11434/v1",
    api_key: str = "not-needed",
    temperature: float = 0.0,
    timeout: float = 120.0,
) -> Any:
    """A bare ``async (prompt) -> str`` callable, for ``LLMJudge``."""
    import httpx

    async def chat(prompt: str) -> str:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                json={
                    "model": model,
                    "temperature": temperature,
                    "messages": [{"role": "user", "content": prompt}],
                },
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            return str(response.json()["choices"][0]["message"]["content"])

    return chat
