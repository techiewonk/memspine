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
import weakref
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .contracts import ReaderAnswer
from .timing import extract_server_timing
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

#: Grounded prompt (reader-gap fix, ``analysis/READER_GAPS.md``): explains the line dates and
#: the ``[= date]`` annotations, asks for the best-supported short answer, and keeps "not
#: mentioned" for questions nothing in the memories bears on (the default prompt's blanket
#: "say you do not know" drew 92 of 182 refusals with the gold evidence in context).
GROUNDED_QA_PROMPT = (
    "Answer the question from the memories below. Each memory is one line: [YYYY-MM-DD] is "
    'the date it was said, and a bracket like "last Saturday [= 2023-05-20]" gives the '
    "absolute date of that relative phrase. Resolve other relative times (yesterday, last "
    "week, two days ago) against the date of the line they appear in, not today's date. "
    "Give a short, direct answer. Give the best-supported answer from the memories, even if "
    "it is indirect; say it is not mentioned only when nothing in the memories bears on the "
    "question. When a date is asked, answer in the wording the memories use (for example "
    '"the week before 9 June 2023" or "2022"), at the precision asked.\n\n'
    "Memories:\n{context}\n\nQuestion: {question}\nAnswer:"
)

#: I3: a prompt for any memory benchmark (no dataset wording). Answers from the memories, treats
#: them as optional context, lets the newest statement win a conflict unless the question is
#: about the past, resolves relative dates against each line's own date and the question date,
#: and refuses with the neutral I1 string when the memories lack the answer.
GROUNDED_GENERIC_QA_PROMPT = (
    "Respond to the user's message using the memories below, which are notes from earlier "
    "conversations. Each line may start with [YYYY-MM-DD], the date it was said. Treat the "
    "memories as optional context: use a memory only when it bears on the request, and do not "
    "bring in personal details that are unrelated to it. When two statements conflict, prefer "
    "the most recent one unless the question asks about the past. Resolve relative times "
    "(yesterday, last week) against the date of the line they appear in; if a question date is "
    "given, resolve times in the question against it. If the memories do not contain the "
    "answer, reply exactly: Not mentioned in the conversation. Never invent specifics "
    "(names, dates, numbers) the memories do not state. Keep the answer short.\n\n"
    "Memories:\n{context}\n\nQuestion date: {question_date}\n"
    "Message: {question}\nAnswer:"
)

#: Dev reasoning 2026-10-10: ``grounded`` plus three rules from the read failures of the
#: development conversations - references to earlier lines ("that book you recommended",
#: "we did it yesterday"), photo captions as evidence, and yes/no inference questions answered
#: with general knowledge. Keeps the short answer (``grounded_detail``'s longer answers broke
#: single-hop questions).
GROUNDED_V2_QA_PROMPT = GROUNDED_QA_PROMPT.replace(
    "Give a short, direct answer. ",
    'A memory may refer back to something said earlier ("that book you recommended", "we '
    'did it yesterday", a photo): find the earlier line it refers to and use its details. '
    "Text in [image: ...] describes a photo shared in that line and counts as evidence. For "
    'questions like "would X likely..." or "is X...", answer yes or no from what the '
    "memories show together with general knowledge, then give the reason in a few words. "
    "Give a short, direct answer. ",
)
assert GROUNDED_V2_QA_PROMPT != GROUNDED_QA_PROMPT

#: ``grounded_v2`` minus its yes/no rule, which made the reader open "when"/"what" answers with
#: "No, ..." (dev screen 2026-10-10: 81.5% vs 86.7%, 5 gained / 17 lost). Keeps the
#: earlier-line reference rule and captions as evidence.
GROUNDED_V3_QA_PROMPT = GROUNDED_QA_PROMPT.replace(
    "Give a short, direct answer. ",
    'A memory may refer back to something said earlier ("that book you recommended", "we '
    'did it yesterday", a photo): find the earlier line it refers to and use its details. '
    "Text in [image: ...] describes a photo shared in that line and counts as evidence. "
    "Give a short, direct answer. ",
)

#: R2-4: ``grounded`` plus one sentence for ``--memspine-context-order hits_first|hit_blocks``
#: (the first lines are the most relevant memories).
GROUNDED_ORDERED_QA_PROMPT = GROUNDED_QA_PROMPT.replace(
    "Give a short, direct answer. ",
    "The first lines are the memories retrieved as most relevant to the question, most "
    "relevant first. Give a short, direct answer. ",
)
assert GROUNDED_ORDERED_QA_PROMPT != GROUNDED_QA_PROMPT

#: C2: ``grounded`` plus the hit-marker legend (``--memspine-mark-hits``), a one-sentence
#: answer that keeps the specific detail, and exhaustive lists for multi-item questions.
GROUNDED_DETAIL_QA_PROMPT = (
    "Answer the question from the memories below. Each memory is one line: [YYYY-MM-DD] is "
    'the date it was said, and a bracket like "last Saturday [= 2023-05-20]" gives the '
    "absolute date of that relative phrase. Resolve other relative times (yesterday, last "
    "week, two days ago) against the date of the line they appear in, not today's date. "
    "If lines start with * or [hit k], those are the memories retrieved as most relevant "
    "(k = rank); unmarked lines are surrounding conversation. Answer in one sentence that "
    "includes the specific detail from the memory (names, objects, places, numbers). When the "
    'question asks for several items or "how many", list every matching item found across '
    "the memories, then count them. Give the best-supported answer from the memories, even "
    "if it is indirect; say it is not mentioned only when nothing in the memories bears on "
    "the question. When a date is asked, answer in the wording the memories use (for example "
    '"the week before 9 June 2023" or "2022"), at the precision asked.\n\n'
    "Memories:\n{context}\n\nQuestion: {question}\nAnswer:"
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
    "grounded": GROUNDED_QA_PROMPT,
    "grounded_detail": GROUNDED_DETAIL_QA_PROMPT,
    "grounded_ordered": GROUNDED_ORDERED_QA_PROMPT,
    "grounded_v2": GROUNDED_V2_QA_PROMPT,
    "grounded_v3": GROUNDED_V3_QA_PROMPT,
    "grounded_generic": GROUNDED_GENERIC_QA_PROMPT,
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


class SystemQAPrompt:
    """A QA prompt with a system message and a no-context variant (OP-Bench).

    Duck-types ``str.format`` for the readers (``format(context=, question=)`` renders the
    user message); a reader that finds a ``system`` attribute sends it as the system message.
    An empty context renders ``without_context``, as the official generation code does.
    """

    def __init__(self, name: str, system: str, with_context: str, without_context: str) -> None:
        self.name = name
        self.system = system
        self.with_context = with_context
        self.without_context = without_context

    def format(self, *, context: str, question: str, question_date: str = "unknown") -> str:
        if context.strip():
            return self.with_context.format(memory=context, question=question)
        return self.without_context.format(question=question)

    def describe(self) -> dict[str, Any]:
        def sha(text: str) -> str:
            return hashlib.sha256(text.encode()).hexdigest()

        joined = "\n".join([self.system, self.with_context, self.without_context])
        return {
            "prompt_sha256": sha(joined),
            "qa_prompt": self.name,
            "system_prompt_sha256": sha(self.system),
        }


#: OP-Bench's memory-augmented assistant (github yulinlp/OP-Bench @ 17c7efd,
#: ``src/opbench/prompts.py`` ``ANSWER_SYSTEM_PROMPT`` / ``ANSWER_USER_PROMPT_WITH_MEMORY`` /
#: ``ANSWER_USER_PROMPT_WITHOUT_MEMORY``; the wording of the paper's appendix prompt). Our
#: retrieved memories take the place of the official memory block. The official frame also
#: titles the block "Memories for user <name>:"; the reader is not told the persona, so the
#: speaker names in each memory line carry that. Docs: ``analysis/OPBENCH_PROTOCOL.md``.
OPBENCH_ASSISTANT_PROMPT = SystemQAPrompt(
    "opbench_assistant",
    system=(
        "You are a communication expert with outstanding communication habits. Throughout the "
        "conversation, you should embody the role of a friend of the user."
    ),
    with_context=(
        "Reply in a natural, spoken tone. When relevant, appropriately incorporate the user's "
        "memory and personality information to make the response personalized and engaging.\n"
        "Memory:\n{memory}\nUser's Latest Input:\n{question}\n"
    ),
    without_context="Reply in a natural, spoken tone.\nUser's Latest Input:\n{question}\n",
)

#: Prompts with a system message, selectable by ``--qa-prompt`` (not in :data:`QA_PROMPTS`:
#: those are plain strings other code hashes and scans).
SYSTEM_QA_PROMPTS: Mapping[str, SystemQAPrompt] = {"opbench_assistant": OPBENCH_ASSISTANT_PROMPT}


def prompt_describe(prompt: str | RoutedQAPrompt | SystemQAPrompt) -> dict[str, Any]:
    """A reader's prompt keys for ``describe()``: ``prompt_sha256`` alone for a fixed
    prompt (unchanged), the routed keys for a :class:`RoutedQAPrompt`, the system-prompt keys
    for a :class:`SystemQAPrompt`."""
    if isinstance(prompt, (RoutedQAPrompt, SystemQAPrompt)):
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


@dataclass(frozen=True, slots=True)
class SamplerConfig:
    """A8 [SRV-2]: the sampler a request asks for, sent explicitly on every reader and judge
    call. Ollama otherwise applies its own model defaults (``presence_penalty`` 1.5 for
    Qwen3.5), which the harness silently inherited before. ``seed`` None = not sent."""

    presence_penalty: float = 0.0
    frequency_penalty: float = 0.0
    top_p: float = 1.0
    seed: int | None = None

    def payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "presence_penalty": self.presence_penalty,
            "frequency_penalty": self.frequency_penalty,
            "top_p": self.top_p,
        }
        if self.seed is not None:
            out["seed"] = self.seed
        return out

    def describe(self) -> dict[str, Any]:
        return self.payload()


class ServerContextExceeded(RuntimeError):
    """D2 [HAR-3]: a call filled the server's context window (``--strict-ctx``)."""


class CtxGuard:
    """D2 [HAR-3]: flags calls whose prompt + completion tokens reach the server's context
    window (minus a margin of 8), where the server truncates silently. One guard is shared by
    the reader and the judge of an arm; the runner drains it after each stage."""

    MARGIN = 8

    def __init__(self, server_ctx: int = 8192, strict: bool = False) -> None:
        self.server_ctx = int(server_ctx)
        self.strict = strict
        #: calls flagged so far, by role
        self.suspected: dict[str, int] = {"reader": 0, "judge": 0}
        self._pending: list[str] = []

    def describe(self) -> dict[str, Any]:
        return {"server_ctx": self.server_ctx, "strict_ctx": self.strict}

    def check(self, prompt_tokens: int, completion_tokens: int, role: str) -> bool:
        """True (and counted) when the call reached the window; raises under ``strict``."""
        if self.server_ctx <= 0 or prompt_tokens + completion_tokens <= 0:
            return False  # no usage reported: cannot tell
        if prompt_tokens + completion_tokens < self.server_ctx - self.MARGIN:
            return False
        self.suspected[role] = self.suspected.get(role, 0) + 1
        self._pending.append(role)
        if self.strict:
            raise ServerContextExceeded(
                f"{role} call used {prompt_tokens}+{completion_tokens} tokens against a server "
                f"context of {self.server_ctx}: the server may have truncated the prompt"
            )
        return True

    def consume(self) -> list[str]:
        """Roles flagged since the last call to ``consume`` (and forget them)."""
        out, self._pending = self._pending, []
        return out


def find_guard(reader: Any) -> CtxGuard | None:
    """The :class:`CtxGuard` of a reader, through wrapper readers (``.inner``)."""
    seen = 0
    while reader is not None and seen < 8:
        guard = getattr(reader, "guard", None)
        if isinstance(guard, CtxGuard):
            return guard
        reader = getattr(reader, "inner", None)
        seen += 1
    return None


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
        prompt: str | RoutedQAPrompt | SystemQAPrompt = DEFAULT_QA_PROMPT,
        reader_id: str | None = None,
        extract_answer: bool = False,
        sampler: SamplerConfig | None = None,
        guard: CtxGuard | None = None,
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
        self.sampler = sampler or SamplerConfig()
        self.guard = guard
        self._headers = {"Authorization": f"Bearer {api_key}"}

    def describe(self) -> Mapping[str, Any]:
        return {
            "reader_id": self.reader_id,
            "model": self.model,
            "base_url": self.base_url,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "sampler": self.sampler.describe(),
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
        messages = [
            {
                "role": "user",
                "content": self.prompt.format(
                    context=context,
                    question=question,
                    question_date=question_date or "unknown",
                ),
            }
        ]
        system = getattr(self.prompt, "system", None)  # SystemQAPrompt only (OP-Bench)
        if system:
            messages.insert(0, {"role": "system", "content": system})
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            **self.sampler.payload(),
            **thinking_off(self.model, reader=True),
            "messages": messages,
        }
        started = time.perf_counter()
        client = _shared_client(self._httpx, self.timeout)
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
        prompt_tokens = int(usage.get("prompt_tokens", 0))
        completion_tokens = int(usage.get("completion_tokens", 0))
        if self.guard is not None:
            self.guard.check(prompt_tokens, completion_tokens, "reader")
        return ReaderAnswer(
            text=final_answer(text) if self.extract_answer else text,
            raw_text=text if self.extract_answer else None,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency,
            model_calls=1,
            truncated=finish == "length",
            finish_reason=finish,
            prompt_variant=prompt_variant(self.prompt, question),
            # D5: server-side timings, when the endpoint returns them (Ollama's /v1 does not)
            extra_meta=(
                {"server_timing": timing} if (timing := extract_server_timing(body)) else {}
            ),
        )


#: loop -> {(httpx module, timeout): client}. Weak on the loop so an entry dies with its loop;
#: keying by ``id()`` let a recycled address hand a new loop or fake module a stale client.
_CLIENTS: weakref.WeakKeyDictionary[Any, dict[tuple[Any, float], Any]] = (
    weakref.WeakKeyDictionary()
)


def _shared_client(httpx: Any, timeout: float) -> Any:
    """One pooled ``httpx.AsyncClient`` per event loop and timeout.

    A new client per call paid ~130 ms of connection and TLS-context setup on every reader
    and judge request (measured on Windows, 2026-10-09: 0.67 s of model time per call), so
    one QA question spent ~0.25 s just opening clients. Keyed by the running loop because an
    AsyncClient is bound to the loop that first uses it.
    """
    import asyncio

    per_loop = _CLIENTS.setdefault(asyncio.get_running_loop(), {})
    key = (httpx, float(timeout))
    client = per_loop.get(key)
    if client is None or getattr(client, "is_closed", False):
        client = per_loop[key] = httpx.AsyncClient(timeout=timeout)
    return client


def openai_compat_chat(
    model: str,
    base_url: str = "http://127.0.0.1:11434/v1",
    api_key: str = "not-needed",
    temperature: float = 0.0,
    timeout: float = 120.0,
    sampler: SamplerConfig | None = None,
    guard: CtxGuard | None = None,
) -> Any:
    """A bare ``async (prompt) -> str`` callable, for ``LLMJudge``."""
    import httpx

    sampler = sampler or SamplerConfig()

    async def chat(prompt: str, system: str | None = None) -> str:
        messages = [{"role": "user", "content": prompt}]
        if system is not None:
            messages.insert(0, {"role": "system", "content": system})
        client = _shared_client(httpx, timeout)
        response = await client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            json={
                "model": model,
                "temperature": temperature,
                **sampler.payload(),
                **thinking_off(model),
                "messages": messages,
            },
            headers={"Authorization": f"Bearer {api_key}"},
        )
        response.raise_for_status()
        body = response.json()
        if (timing := extract_server_timing(body)) is not None:
            chat.server_timings.append(timing)  # type: ignore[attr-defined]
        if guard is not None:
            usage = body.get("usage") or {}
            guard.check(
                int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)), "judge"
            )
        return str(body["choices"][0]["message"]["content"])

    # R3-11: the judge records these in its spec.
    chat.params = {  # type: ignore[attr-defined]
        "endpoint": "openai-compat",
        "base_url": base_url,
        "temperature": temperature,
        "max_tokens": None,
        "sampler": sampler.describe(),
    }
    chat.guard = guard  # type: ignore[attr-defined]
    chat.server_timings = []  # type: ignore[attr-defined]  # D5: per-call server timings
    return chat
