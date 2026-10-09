"""Readers: the $\\mathcal{G}$ stage, held fixed across systems in a run.

The framework's own finding is that generation is the field's least-engineered
stage. The harness treats it as a single, declared, swappable backbone — and
records which one ran, because Mastra's published pair (84.23 on ``gpt-4o``,
94.87 on ``gpt-5-mini``, same harness, same judge, same dataset) shows the
backbone alone moving a headline by 10.64 points.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Mapping
from typing import Any

from .contracts import ReaderAnswer
from .tokens import HeuristicTokenCounter, TokenCounter
from .vendor_judges import EVERMEMOS_COT_QA_PROMPT

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

#: N14 (plan v3.2, MemOS answer-prompt clauses): ``dated`` plus three generic rules, no
#: extra call: use world knowledge to interpret what the context states (open-domain
#: questions), the latest statement of a changed fact wins, and keep people's names apart.
#: Prompt arms lost before (L4), so this is measured only in a paid QA screen (U6).
DATED_WORLD_QA_PROMPT = DATED_QA_PROMPT.replace(
    "Answer in one short sentence.",
    "Use general world knowledge to interpret what the context states (for example, a "
    "named park tells you the state it is in), but do not invent facts about the people. "
    "When the context gives a fact more than once with different values, the latest line "
    "is the current one. Do not confuse what one person said or did with another. Answer "
    "in one short sentence.",
)

#: H7: abstention-aware variant for adversarial questions (LoCoMo cat 5): answer only
#: what the context states about the person asked about.
ABSTAIN_QA_PROMPT = DATED_QA_PROMPT.replace(
    "If the context does not contain the answer, say you do not know.",
    "Answer only what the context states about the person the question names; if the "
    'context attributes it to someone else, or does not state it, reply "Not mentioned".',
)

#: LoCoMo-Plus: the "question" is a later conversational message; the reader replies to it
#: as the assistant, using whatever it remembers.
CONVERSE_QA_PROMPT = (
    "You are a helpful assistant in a long-running conversation. Your memory notes from earlier "
    "in the conversation are below. Reply to the latest message in two or three sentences, "
    "taking into account anything from the notes that matters for it.\n\n"
    "Memory notes:\n{context}\n\nLatest message: {question}\nReply:"
)

#: MemoryAgentBench fact consolidation: the benchmark tells the reader that a larger
#: serial number means a newer fact (paraphrase of its instruction; declared per run).
MAB_FC_QA_PROMPT = (
    "You are a knowledge management system. Each fact below starts with a serial number; a "
    "fact with a larger serial number is newer and overrides an older fact it contradicts. "
    "Answer the question using the newest facts only, with just the answer (a few words).\n\n"
    "Facts:\n{context}\n\nQuestion: {question}\nAnswer:"
)

#: R3-6: the dated prompt plus the date the question is asked. LongMemEval questions carry a
#: ``question_date`` and are relative to it ("how many weeks ago..."); without it the reader
#: has no anchor. The runner passes the date; "unknown" when the dataset has none.
QUESTION_DATED_QA_PROMPT = DATED_QA_PROMPT.replace(
    "Context:\n{context}\n\nQuestion: {question}\nAnswer:",
    "Context:\n{context}\n\nThe question is asked on {question_date}.\n"
    "Question: {question}\nAnswer:",
)

#: G10: the dated prompt plus an inference rule. In combo-A's open-domain errors, half
#: were refusals on "would / might / likely" questions the context supports; the dated
#: prompt's "say you do not know" made the reader refuse instead of inferring.
INFER_RULE = (
    "If the question asks what someone would, might, or is likely to do, be, or think, "
    "infer the most plausible answer from the context and give it (e.g. 'Likely yes, "
    "because ...'); say you do not know only when nothing in the context bears on it. "
    "Otherwise, if the context does not contain the answer, say you do not know."
)
DATED_INFER_QA_PROMPT = DATED_QA_PROMPT.replace(
    "If the context does not contain the answer, say you do not know.", INFER_RULE
)

#: G12: the dated prompt plus a said-vs-happened rule. In 10 of combo-A's 29 wrong
#: absolute dates the reader answered with the date the event was mentioned (the line's
#: session date), not the date it happened.
SAID_HAPPENED_RULE = (
    "A line's leading [YYYY-MM-DD] is when it was said; a bracketed [= ...] after a "
    'relative phrase is the resolved date the event happened. For "when did X happen", '
    "answer with the happened date (the [= ...] value when present), not the date it was "
    "said."
)
DATED2_QA_PROMPT = DATED_QA_PROMPT.replace(
    "Answer in one short sentence.", f"{SAID_HAPPENED_RULE} Answer in one short sentence."
)

#: #34 (SM-13): brief reasoning, then a final "Answer:" line the reader keeps
#: (:func:`final_answer`). Mirrors the engine's ``chat@dated3``: quote the specific detail
#: (48 of combo-A's cat-4 wrong answers paraphrased a gold phrase), dates in the granularity
#: asked, said vs happened (#29), merge repeated mentions before counting (#60), and no
#: blanket refusal clause ("Not mentioned" only when nothing bears on the question).
DATED3_QA_PROMPT = (
    "Answer the question using only the context below. Each line starts with the date it "
    'was said, and phrases like "last Friday [= Fri 2023-07-14]" show the absolute date. '
    "A date in brackets is when it was said; the event may be earlier. A bracketed [= ...] "
    'after a relative phrase, or the "happened" date of a [said ... \u00b7 happened ...] '
    "line, is when the event happened: when a question asks when something happened, give "
    "that date, computed from the line's date, not the date of the conversation. Answer a "
    'date in the style the question asks: a year for "which year", a month for "which '
    "month\", a day otherwise; relative to the line's date when the context gives no more "
    '("the week before 2024-03-14"). Use the specific detail from the context, quoting its '
    'words (a name, a title, a phrase such as "magical") rather than paraphrasing it. For a '
    '"how many" question, merge repeated mentions of the same event (the same thing on the '
    "same date) and count distinct events. First write one or two short sentences of "
    "reasoning that point at the context lines you use. Then write a final line that starts "
    'with "Answer:" followed by the short answer only. If nothing in the context bears on '
    'the question, answer "Not mentioned".\n\n'
    "Context:\n{context}\n\nQuestion: {question}\nReasoning:"
)

#: N46 (MemMachine answer clause, our wording): ``dated`` plus "a plan the context
#: states counts as done unless the context says it did not happen". QA only (paid).
DATED_PLANNED_QA_PROMPT = DATED_QA_PROMPT.replace(
    "Answer in one short sentence.",
    "When the context says someone planned or intended to do something and never says it "
    "did not happen, treat it as done. Answer in one short sentence.",
)

#: N56 (Mem0 answer prompt, our wording): ``dated`` without the refusal clause; the
#: reader always gives its best answer from the context. QA only (paid).
DATED_NOABSTAIN_QA_PROMPT = DATED_QA_PROMPT.replace(
    "If the context does not contain the answer, say you do not know.",
    "Always give your best answer from the context; never reply that you do not know.",
)

#: N65 (EverMemOS): its 7-step chain-of-thought answer prompt, verbatim from
#: ``benchmarks/run.py:102`` @ 933f818 (code-traced notes,
#: ``docs/survey/_staging/EverMemOS/PROMPTS.md``). The reply ends in a
#: "STEP 7: FINAL ANSWER" section, which :func:`final_answer` reads. QA only (paid). The text
#: lives in ``vendor_judges.py`` (vendor texts kept verbatim, long lines allowed).

QA_PROMPTS = {
    "mab_fc": MAB_FC_QA_PROMPT,
    "question_dated": QUESTION_DATED_QA_PROMPT,
    "default": DEFAULT_QA_PROMPT,
    "dated": DATED_QA_PROMPT,
    "dated_world": DATED_WORLD_QA_PROMPT,
    "dated2": DATED2_QA_PROMPT,
    "dated_infer": DATED_INFER_QA_PROMPT,
    "dated3": DATED3_QA_PROMPT,
    "abstain": ABSTAIN_QA_PROMPT,
    "converse": CONVERSE_QA_PROMPT,
    "dated_planned": DATED_PLANNED_QA_PROMPT,
    "dated_noabstain": DATED_NOABSTAIN_QA_PROMPT,
    "evermemos_cot": EVERMEMOS_COT_QA_PROMPT,
}

#: C1 / H11: the ``routed`` QA prompt picks one of three variants per question by its
#: shape (``memspine.core.query_shape``). The infer arm gained only on temporal questions
#: (+1.3, others -0.3) and dated3's terse answers and conditional refusal cost -3.3, so:
#: every variant keeps ``dated``'s one-sentence answer, the temporal one adds the
#: relative-date inference and a date format, the inference one drops the refusal.
_DATED_REFUSAL = "If the context does not contain the answer, say you do not know."
ROUTED_TEMPORAL_RULE = (
    "If the date is not stated outright, infer the most plausible date from the line's "
    "date and any relative phrase (e.g. 'the week before 14 March 2024'); say you do not "
    "know only when nothing in the context bears on it. Answer dates as DD Month YYYY "
    "(or the granularity the question asks)."
)
ROUTED_INFERENCE_RULE = (
    "If the context does not state it, give the most plausible answer and say 'likely'."
)
ROUTED_QA_VARIANTS: Mapping[str, str] = {
    "plain": DATED_QA_PROMPT,
    "temporal": DATED_QA_PROMPT.replace(_DATED_REFUSAL, ROUTED_TEMPORAL_RULE),
    "inference": DATED_QA_PROMPT.replace(_DATED_REFUSAL, ROUTED_INFERENCE_RULE),
}
#: Bump whenever the routing decision (the shape rules or their order) changes.
QA_ROUTER_VERSION = "shape-v1"


def qa_shape(question: str) -> str:
    """The ``routed`` variant for ``question``: ``temporal`` when it asks for a date or a
    span, else ``inference`` for a "would / likely / might / could ...?" question, else
    ``plain``. Temporal wins ("When would ...?" is a date question)."""
    from memspine.core.query_shape import is_inference, is_temporal

    if is_temporal(question):
        return "temporal"
    if is_inference(question):
        return "inference"
    return "plain"


class RoutedQAPrompt:
    """A QA prompt chosen per question (C1). Duck-types ``str.format`` for the readers:
    ``format(question=...)`` renders the variant :func:`qa_shape` picks."""

    name = "routed"

    def __init__(self, variants: Mapping[str, str] = ROUTED_QA_VARIANTS) -> None:
        self.variants = dict(variants)

    def variant_for(self, question: str) -> str:
        return qa_shape(question)

    def format(self, *, context: str, question: str, question_date: str = "unknown") -> str:
        return self.variants[self.variant_for(question)].format(
            context=context, question=question, question_date=question_date
        )

    def describe(self) -> dict[str, Any]:
        """``describe()`` keys of a routed reader: the name, the router version and one
        hash per variant; ``prompt_sha256`` hashes the three together."""
        hashes = {k: hashlib.sha256(v.encode()).hexdigest() for k, v in self.variants.items()}
        joined = "\n".join(f"{k}={hashes[k]}" for k in sorted(hashes))
        return {
            "prompt_sha256": hashlib.sha256(f"{QA_ROUTER_VERSION}\n{joined}".encode()).hexdigest(),
            "qa_prompt": self.name,
            "qa_router": QA_ROUTER_VERSION,
            "prompt_variants_sha256": hashes,
        }


#: Per-question QA prompts, selectable by ``--qa-prompt`` next to :data:`QA_PROMPTS`.
ROUTED_QA_PROMPTS: Mapping[str, RoutedQAPrompt] = {"routed": RoutedQAPrompt()}


def prompt_describe(prompt: str | RoutedQAPrompt) -> dict[str, Any]:
    """A reader's prompt keys for ``describe()``: ``prompt_sha256`` alone for a fixed
    prompt (unchanged), the routed keys for a :class:`RoutedQAPrompt`."""
    if isinstance(prompt, RoutedQAPrompt):
        return prompt.describe()
    return {"prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()}


def prompt_variant(prompt: str | RoutedQAPrompt, question: str) -> str | None:
    """The routed variant used for ``question``; None for a fixed prompt."""
    return prompt.variant_for(question) if isinstance(prompt, RoutedQAPrompt) else None


#: #34: prompts whose reply reasons first; the reader keeps only the final answer and
#: gets :data:`REASONING_MAX_TOKENS` so the reasoning cannot truncate the answer.
REASONING_QA_PROMPTS = frozenset({"dated3", "evermemos_cot"})
REASONING_MAX_TOKENS = 512
#: N65: a prompt whose reasoning needs more room than :data:`REASONING_MAX_TOKENS`.
REASONING_MAX_TOKENS_BY_PROMPT = {"evermemos_cot": 1536}


def reasoning_max_tokens(prompt_name: str) -> int:
    """The reply cap for a reasoning prompt (#34; N65 needs more for its seven steps)."""
    return REASONING_MAX_TOKENS_BY_PROMPT.get(prompt_name, REASONING_MAX_TOKENS)


_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
#: Version of :func:`final_answer` recorded in ``describe()`` when ``extract_answer`` is
#: set. "v2" is the Wave 1 rewrite (``<think>`` handling, dangling markers, first line
#: after the last marker); bump it whenever ``final_answer``'s output can change.
ANSWER_EXTRACTOR_VERSION = "v3"

#: Cap on the raw reply kept in a row's ``meta["reader_raw"]`` (extraction only).
READER_RAW_MAX_CHARS = 4000


def reader_raw_meta(raw_text: str | None) -> dict[str, Any]:
    """Row ``meta`` keys for a reader's raw reply: ``{}`` when no extraction ran (rows of
    every non-extracting prompt stay byte-identical), else ``reader_raw`` capped at
    :data:`READER_RAW_MAX_CHARS`, plus ``reader_raw_truncated: True`` when the cap cut it."""
    if raw_text is None:
        return {}
    if len(raw_text) <= READER_RAW_MAX_CHARS:
        return {"reader_raw": raw_text}
    return {"reader_raw": raw_text[:READER_RAW_MAX_CHARS], "reader_raw_truncated": True}


_MARKER = re.compile(
    r"(?:^|(?<=[\s*>#_(\[]))\**\s*(?:final\s+|short\s+)?answer\s*\**\s*[:\uff1a]\s*\**"
    # v3 (N65): EverMemOS's "## STEP 7: FINAL ANSWER" section heading (no colon after it).
    r"|(?:^|(?<=\n))[ \t]*#*[ \t]*STEP[ \t]*7[ \t]*[:.][ \t]*FINAL[ \t]+ANSWER\b[^\n]*",
    re.I,
)


_OPEN_THINK = re.compile(r"<think>", re.I)


def _first_line(text: str) -> str:
    """The first non-empty line of ``text``, Markdown bold stripped."""
    for line in text.splitlines():
        stripped = line.strip().strip("*").strip()
        if stripped:
            return stripped
    return ""


def final_answer(text: str) -> str:
    """#34: the first non-empty line after the last ``Answer:`` marker, ``<think>``
    blocks (closed or cut off) dropped; a reply without the marker comes back whole
    (stripped), only dangling markers give the reply before the first one, and a lone
    marker gives "". Same code as ``memspine.core.answer.final_answer`` (kept here so the
    harness core stays stdlib-only)."""
    cleaned = _THINK.sub("", text)
    if "</think>" in cleaned.lower():  # an unclosed block: keep what follows its end
        cleaned = re.split(r"</think>", cleaned, flags=re.I)[-1]
    opened = _OPEN_THINK.search(cleaned)
    if opened is not None:  # a block never closed (cut off): dropped unless it holds a marker
        rest = cleaned[opened.end() :]
        cleaned = cleaned[: opened.start()] + (rest if _MARKER.search(rest) else "")
    cleaned = cleaned.strip()
    matches = list(_MARKER.finditer(cleaned))
    for match in reversed(matches):
        answer = _first_line(cleaned[match.end() :])
        if answer:
            return answer
    if matches:  # only dangling markers: the reply before the first one
        return cleaned[: matches[0].start()].strip()
    return cleaned


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

    async def answer(
        self, question: str, context: str, question_date: str | None = None
    ) -> ReaderAnswer:
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

    async def answer(
        self, question: str, context: str, question_date: str | None = None
    ) -> ReaderAnswer:
        text = self._answers.get(question, self._default)
        return ReaderAnswer(
            text=text,
            prompt_tokens=self._counter.count(context),
            completion_tokens=self._counter.count(text),
            latency_ms=0.0,
            model_calls=0,
        )


def thinking_off(model: str, *, reader: bool = False) -> dict[str, Any]:
    """Request body fields that switch reasoning off for models that think by default.

    Qwen3.5 dropped the ``/no_think`` soft switch (appending it leaves the answer empty
    while the model keeps reasoning); an OpenAI-compatible endpoint turns thinking off
    with ``reasoning_effort: "none"`` (verified on Ollama /v1, 2026-10-09). Qwen3 (no
    ``.5``) keeps its own switch and is untouched.
    """
    import os

    # think-mode comparison runs: only the reader (the model under test) may think;
    # the judge always answers without reasoning so the comparison isolates the reader.
    if reader and os.environ.get("MEMSPINE_EVAL_THINK") == "on":
        return {}
    name = model.lower()
    if "qwen3.5" in name or "qwen3.6" in name:
        return {"reasoning_effort": "none"}
    return {}


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
        base_url: str = "http://127.0.0.1:11434/v1",
        api_key: str = "not-needed",
        temperature: float = 0.0,
        max_tokens: int = 512,
        timeout: float = 120.0,
        prompt: str | RoutedQAPrompt = DEFAULT_QA_PROMPT,
        reader_id: str | None = None,
        extract_answer: bool = False,
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
        import os

        if os.environ.get("MEMSPINE_EVAL_THINK") == "on":
            # reasoning tokens count against max_tokens: leave room for the answer
            self.max_tokens = max(self.max_tokens, 4096)
            self.timeout = max(self.timeout, 600.0)
        #: #34: keep only the final answer of a reasoning prompt (:func:`final_answer`).
        self.extract_answer = extract_answer
        self._headers = {"Authorization": f"Bearer {api_key}"}

    def describe(self) -> Mapping[str, Any]:
        return {
            "reader_id": self.reader_id,
            "model": self.model,
            "base_url": self.base_url,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            **prompt_describe(self.prompt),
            **(
                {"extract_answer": True, "answer_extractor": ANSWER_EXTRACTOR_VERSION}
                if self.extract_answer
                else {}
            ),
        }

    async def answer(
        self, question: str, context: str, question_date: str | None = None
    ) -> ReaderAnswer:
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            **thinking_off(self.model, reader=True),
            "messages": [
                {
                    "role": "user",
                    "content": self.prompt.format(
                        context=context,
                        question=question,
                        question_date=question_date or "unknown",
                    ),
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
        text = choice["message"]["content"].strip()
        return ReaderAnswer(
            text=final_answer(text) if self.extract_answer else text,
            raw_text=text if self.extract_answer else None,
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            latency_ms=latency,
            model_calls=1,
            truncated=finish == "length",
            finish_reason=finish,
            prompt_variant=prompt_variant(self.prompt, question),
        )


def openai_compat_chat(
    model: str,
    base_url: str = "http://127.0.0.1:11434/v1",
    api_key: str = "not-needed",
    temperature: float = 0.0,
    timeout: float = 120.0,
) -> Any:
    """A bare ``async (prompt) -> str`` callable, for ``LLMJudge``."""
    import httpx

    async def chat(prompt: str, system: str | None = None) -> str:
        messages = [{"role": "user", "content": prompt}]
        if system is not None:
            messages.insert(0, {"role": "system", "content": system})
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                json={
                    "model": model,
                    "temperature": temperature,
                    **thinking_off(model),
                    "messages": messages,
                },
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            return str(response.json()["choices"][0]["message"]["content"])

    # R3-11: the judge records these in its spec.
    chat.params = {  # type: ignore[attr-defined]
        "endpoint": "openai-compat",
        "base_url": base_url,
        "temperature": temperature,
        "max_tokens": None,
    }
    return chat
