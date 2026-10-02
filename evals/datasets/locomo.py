"""LoCoMo dataset adapter — loader, ingestion driver, and per-sample memory cache.

LoCoMo (Maharana et al., *Evaluating Very Long-Term Conversational Memory of LLM
Agents*) is a very-long-term dialogue benchmark: ~300-700 turns per sample spread
over up to 35 dated sessions between two personas, plus a QA set whose questions
carry a category code and evidence pointers back into the dialogue.

**Licence position — read before touching data.** This adapter vendors *no data*.
The LoCoMo corpus is released by Snap Inc. under its own terms; we neither
redistribute it nor commit it to this repository. Every entry point takes a
filesystem path the operator supplies (or ``MEMSPINE_LOCOMO_PATH``). If you find a
`locomo*.json` inside this repo, that is a bug, not a fixture — note that no
`.gitignore` rule currently enforces it, so the guarantee is a review discipline,
not a mechanism. The tiny fixtures used by the unit tests are hand-written and
inline, not excerpts of the corpus.

What lives here (and what deliberately does not):

* **Parsing** — ``load_locomo`` / ``iter_locomo`` turn the published JSON into frozen,
  fully typed records. Category codes are resolved to :class:`LocomoCategory` and
  carried on every question so per-category statistics are reportable end to end
  (MAGMA harvest §2.4).
* **Ingestion** — ``ingest_sample`` feeds turns into an engine through
  :meth:`Engine.write_messages`, i.e. the *normal write door*. Nothing is stubbed and
  nothing is bypassed; measuring the real write cost is the point. Be precise about
  *which* layers that bills, though: these are ``episodic`` writes, so they pay
  firewall assessment, event append, all four projectors, corroboration and link
  evolution — but **not** MinHash-LSH dedup and **not** the M4 conflict ladder, both
  of which ``Engine._write_locked`` gates on ``memory_type == "semantic"``. Those two
  layers are priced on the consolidation path, not here.
* **Caching** — the built store is cached per sample through
  :class:`evals.datasets.base.BuildCache`, keyed by ``(dataset, sample_id, profile,
  config_hash, options_hash, adapter_version)`` with an explicit ``rebuild`` escape
  hatch (MAGMA harvest §2.3). Construction is the expensive half; read-side ablations
  must not pay for it twice. ``options_hash`` is what stops a run with the sleep
  cadence changed from silently reusing a store that never consolidated. The cache
  holds the engine's own on-disk store plus a bookkeeping manifest — it is *not* a
  second source of truth for memory content (the event log remains that).

The dataset-agnostic half of this adapter lives in :mod:`evals.datasets.base` and is
shared with the LongMemEval adapter: the neutral sample model, the build cache, the
ingestion accounting and the namespace rule. :meth:`LocomoSample.to_benchmark_sample`
projects LoCoMo's richer records onto that shared shape.

Scoring, judging and CLI wiring live elsewhere in ``evals/``; this module has no
opinion about them and imports nothing from them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

from .base import (
    MANIFEST_VERSION,
    Ability,
    BenchmarkQuery,
    BenchmarkSample,
    BuildCache,
    BuildKey,
    BuildManifest,
    BuildSlot,
    ChatTurn,
    DatasetAdapter,
    IngestionStats,
    IngestUnit,
    config_hash,
    content_digest,
    options_digest,
)
from .base import sample_namespace as _namespace_for

__all__ = [
    "ABILITY_BY_CATEGORY",
    "ADAPTER_NAME",
    "ADAPTER_VERSION",
    "CATEGORY_LABELS",
    "DATASET_PATH_ENV",
    "MANIFEST_VERSION",
    "BuildCache",
    "BuildKey",
    "BuildManifest",
    "BuildOutcome",
    "BuildSlot",
    "DatasetAdapter",
    "EngineFactory",
    "IngestOptions",
    "IngestionStats",
    "LocomoAdapter",
    "LocomoCategory",
    "LocomoFormatError",
    "LocomoQuestion",
    "LocomoSample",
    "LocomoSession",
    "LocomoTurn",
    "SupportsEngineWrite",
    "adapter",
    "build_or_load_memory",
    "category_counts",
    "config_hash",
    "ingest_sample",
    "iter_locomo",
    "load_locomo",
    "load_samples",
    "parse_locomo",
    "resolve_dataset_path",
    "sample_namespace",
]

ADAPTER_NAME: Final = "locomo"
"""Dataset name: the first namespace segment and the cache-tree root directory."""

ADAPTER_VERSION: Final = "1"
"""Bumped whenever a change here alters what gets ingested; folded into every cache key."""

#: Environment variable holding the path to the operator's own LoCoMo JSON.
#: The corpus is never vendored (see the module docstring), so the harness needs
#: *some* way to be told where it is without threading a flag through every call.
DATASET_PATH_ENV: Final = "MEMSPINE_LOCOMO_PATH"

#: Provenance channel stamped on every ingested turn. Deliberately *not* one of
#: the firewall's external channels (``retrieved``/``web``/``ingest``/``rest``,
#: see ``core/policies/trust.py``): a benchmark transcript is first-party
#: conversation, and capping its trust would quarantine the corpus and price the
#: firewall against a workload nobody runs.
DEFAULT_CHANNEL: Final = "locomo"

#: Both LoCoMo speakers are humans in a peer conversation; neither is an agent.
#: Mapping both to ``user`` keeps the trust matrix honest. Speaker identity is
#: preserved in the rendered content prefix (``"Ada: ..."``), not in the
#: trust-bearing ``role`` field. Override via ``IngestOptions.role_for_speaker``.
DEFAULT_SPEAKER_ROLE: Final = "user"

_SESSION_KEY = re.compile(r"^session_(\d+)$")
_DATE_KEY_SUFFIX: Final = "_date_time"


class LocomoFormatError(ValueError):
    """The supplied JSON does not match the published LoCoMo layout."""


class LocomoCategory(IntEnum):
    """LoCoMo question categories, as encoded in the released ``qa[].category``.

    The numeric encoding is the dataset's own. The mapping below was confirmed
    against the corpus rather than assumed: category 1 carries ~3.1 evidence
    turns per question against ~1.1 for category 4, and category 5 is the only
    one that uses ``adversarial_answer`` instead of ``answer``.
    """

    UNKNOWN = 0
    MULTI_HOP = 1
    TEMPORAL = 2
    OPEN_DOMAIN = 3
    SINGLE_HOP = 4
    ADVERSARIAL = 5

    @classmethod
    def from_code(cls, code: int) -> LocomoCategory:
        """Resolve a raw code, degrading to :attr:`UNKNOWN` instead of raising.

        A future LoCoMo revision that adds a category must not make the loader
        unusable; the raw code stays on :attr:`LocomoQuestion.raw_category` so
        nothing is lost from per-category reporting.
        """
        try:
            return cls(code)
        except ValueError:
            return cls.UNKNOWN


#: Human-readable labels for report tables and CLI output.
CATEGORY_LABELS: Final[Mapping[LocomoCategory, str]] = {
    LocomoCategory.UNKNOWN: "unknown",
    LocomoCategory.MULTI_HOP: "multi-hop",
    LocomoCategory.TEMPORAL: "temporal",
    LocomoCategory.OPEN_DOMAIN: "open-domain",
    LocomoCategory.SINGLE_HOP: "single-hop",
    LocomoCategory.ADVERSARIAL: "adversarial",
}

#: LoCoMo category -> the shared coarse :class:`~evals.datasets.base.Ability` label.
#:
#: **This grouping is ours**, not LoCoMo's — the benchmark publishes numeric codes and
#: no ability taxonomy at all. It exists so one frontier table can carry both
#: benchmarks; the dataset-native label survives verbatim on every question
#: (``question_type``), so any consumer preferring a different grouping can rebuild it.
#: ``open-domain`` maps to ``OTHER`` deliberately: it asks about world knowledge rather
#: than memory, and folding it into information extraction would flatter the IE column.
ABILITY_BY_CATEGORY: Final[Mapping[LocomoCategory, Ability]] = {
    LocomoCategory.UNKNOWN: Ability.OTHER,
    LocomoCategory.MULTI_HOP: Ability.MULTI_SESSION_REASONING,
    LocomoCategory.TEMPORAL: Ability.TEMPORAL_REASONING,
    LocomoCategory.OPEN_DOMAIN: Ability.OTHER,
    LocomoCategory.SINGLE_HOP: Ability.INFORMATION_EXTRACTION,
    LocomoCategory.ADVERSARIAL: Ability.ABSTENTION,
}


# ── parsed records ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class LocomoTurn:
    """One dialogue turn, addressable by its published ``dia_id`` (e.g. ``D3:14``)."""

    dia_id: str
    speaker: str
    text: str
    session_index: int
    session_date: str | None
    turn_index: int
    image_urls: tuple[str, ...] = ()
    image_caption: str | None = None
    image_query: str | None = None

    def render(self, *, with_timestamp: bool = True) -> str:
        """The exact string handed to the engine — one turn, one record.

        The session timestamp is folded into the text because the write door has
        no event-time parameter today (``MemoryRecord.valid_from`` defaults to
        wall-clock now), so a temporal question would otherwise have nothing to
        resolve against. Image turns carry their caption inline rather than being
        silently dropped; the URL is not fetched.
        """
        parts: list[str] = []
        if with_timestamp and self.session_date:
            parts.append(f"[{self.session_date}] ")
        parts.append(f"{self.speaker}: {self.text}")
        if self.image_caption:
            parts.append(f" [shared image: {self.image_caption}]")
        return "".join(parts)


@dataclass(frozen=True, slots=True)
class LocomoSession:
    """One dated session; the natural episode boundary for ingestion."""

    index: int
    date_time: str | None
    turns: tuple[LocomoTurn, ...]


@dataclass(frozen=True, slots=True)
class LocomoQuestion:
    """One QA item with its gold answer, category and evidence pointers."""

    question_id: str
    question: str
    category: LocomoCategory
    raw_category: int
    answer: str | None
    adversarial_answer: str | None
    evidence: tuple[str, ...]

    @property
    def gold(self) -> str | None:
        """The reference string a judge should score against.

        Adversarial items *usually* publish their reference under
        ``adversarial_answer`` and leave ``answer`` null; collapsing the two here keeps
        scoring code from having to know that.

        **Precedence is a decision, not a fact.** Two items in the released corpus
        (``conv-26:q167`` and ``conv-26:q178``) carry *both* keys with contradictory
        values — ``answer="No"``, ``adversarial_answer="Yes"``. ``answer`` wins, so
        those two are scored against ``"No"`` while the other 444 adversarial items are
        scored against ``adversarial_answer``. Both raw fields stay on the record, so a
        different rule can be applied downstream without reparsing.
        """
        return self.answer if self.answer is not None else self.adversarial_answer

    @property
    def is_adversarial(self) -> bool:
        return self.category is LocomoCategory.ADVERSARIAL

    @property
    def category_label(self) -> str:
        return CATEGORY_LABELS[self.category]


@dataclass(frozen=True, slots=True)
class LocomoSample:
    """One conversation plus its QA set — the unit of caching and of ingestion."""

    sample_id: str
    speaker_a: str
    speaker_b: str
    sessions: tuple[LocomoSession, ...]
    questions: tuple[LocomoQuestion, ...]

    @property
    def turns(self) -> tuple[LocomoTurn, ...]:
        return tuple(turn for session in self.sessions for turn in session.turns)

    @property
    def session_count(self) -> int:
        return len(self.sessions)

    @property
    def turn_count(self) -> int:
        return sum(len(session.turns) for session in self.sessions)

    def questions_by_category(self) -> Mapping[LocomoCategory, tuple[LocomoQuestion, ...]]:
        """Group questions for per-category reporting (MAGMA harvest §2.4)."""
        grouped: dict[LocomoCategory, list[LocomoQuestion]] = {}
        for question in self.questions:
            grouped.setdefault(question.category, []).append(question)
        return {category: tuple(items) for category, items in grouped.items()}

    def content_hash(self) -> str:
        """Stable digest of everything ingestion depends on.

        Folded into the cache manifest so that editing or re-downloading the
        corpus invalidates the built store instead of silently reusing one built
        from different turns — the failure mode that quietly poisons a results
        table.
        """
        digest = hashlib.sha256()
        digest.update(self.sample_id.encode("utf-8"))
        for turn in self.turns:
            digest.update(b"\x00")
            digest.update(turn.dia_id.encode("utf-8"))
            digest.update(b"\x01")
            digest.update(turn.render().encode("utf-8"))
        for question in self.questions:
            digest.update(b"\x02")
            digest.update(question.question.encode("utf-8"))
            digest.update(b"\x03")
            digest.update((question.gold or "").encode("utf-8"))
            digest.update(b"\x04")
            digest.update(str(int(question.category)).encode("utf-8"))
        return digest.hexdigest()

    def to_benchmark_sample(self) -> BenchmarkSample:
        """Project onto the shared :class:`~evals.datasets.base.BenchmarkSample`.

        Lossless where it matters: the rendered turn text is exactly what
        :func:`ingest_sample` writes, ``dia_id`` rides along as
        :attr:`~evals.datasets.base.ChatTurn.turn_id` so retrieved evidence can be
        matched against LoCoMo's turn-level gold pointers, and the numeric category is
        preserved both as the verbatim ``question_type`` label and in ``meta``.

        Both speakers map to ``user``: they are humans in a peer conversation, and the
        :class:`IngestOptions` role override is an ingestion-time concern, not a
        property of the corpus. Roles here are for readers of the neutral record;
        :func:`ingest_sample` remains the authority on what is actually written.
        """
        units = tuple(
            IngestUnit(
                unit_id=f"{self.sample_id}:session:{session.index}",
                index=position,
                timestamp_raw=session.date_time or "",
                timestamp=None,
                turns=tuple(
                    ChatTurn(
                        role=DEFAULT_SPEAKER_ROLE,
                        content=turn.render(),
                        turn_id=turn.dia_id,
                    )
                    for turn in session.turns
                ),
            )
            for position, session in enumerate(self.sessions)
        )
        evidence_units = {unit.unit_id for unit in units}
        queries = tuple(
            BenchmarkQuery(
                query_id=f"{self.sample_id}:{question.question_id}",
                question=question.question,
                gold_answer=question.gold,
                ability=ABILITY_BY_CATEGORY.get(question.category, Ability.OTHER),
                question_type=question.category_label,
                expects_abstention=question.is_adversarial,
                evidence_turn_ids=question.evidence,
                evidence_unit_ids=tuple(
                    sorted(
                        unit
                        for unit in evidence_units
                        if any(
                            turn.turn_id in question.evidence
                            for u in units
                            if u.unit_id == unit
                            for turn in u.turns
                        )
                    )
                ),
                meta={
                    "raw_category": question.raw_category,
                    "category_code": int(question.category),
                    "answer": question.answer,
                    "adversarial_answer": question.adversarial_answer,
                    "evidence": list(question.evidence),
                },
            )
            for question in self.questions
        )
        return BenchmarkSample(
            dataset=ADAPTER_NAME,
            sample_id=self.sample_id,
            namespace=_namespace_for(ADAPTER_NAME, self.sample_id),
            units=units,
            queries=queries,
            content_hash=content_digest(ADAPTER_NAME, ADAPTER_VERSION, self.sample_id, units),
        )


def category_counts(sample: LocomoSample) -> Mapping[str, int]:
    """``{"multi-hop": 42, ...}`` — a report-ready census of one sample's QA set."""
    counts: dict[str, int] = {}
    for question in sample.questions:
        label = question.category_label
        counts[label] = counts.get(label, 0) + 1
    return counts


# ── parsing ──────────────────────────────────────────────────────────────────


def _as_mapping(value: object, where: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise LocomoFormatError(f"{where}: expected an object, got {type(value).__name__}")
    for key in value:
        if not isinstance(key, str):
            raise LocomoFormatError(f"{where}: expected string keys, got {type(key).__name__}")
    mapping: Mapping[str, object] = {str(k): v for k, v in value.items()}
    return mapping


def _as_sequence(value: object, where: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise LocomoFormatError(f"{where}: expected an array, got {type(value).__name__}")
    items: Sequence[object] = value
    return items


def _as_str(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise LocomoFormatError(f"{where}: expected a string, got {type(value).__name__}")
    return value


def _opt_str(value: object, where: str) -> str | None:
    if value is None:
        return None
    return _as_str(value, where)


def _as_answer(value: object, where: str) -> str | None:
    """Gold answers are usually strings but the corpus also stores bare years."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise LocomoFormatError(f"{where}: boolean is not a valid answer")
    if isinstance(value, int | float):
        return str(value)
    return _as_str(value, where)


def _as_category(value: object, where: str) -> tuple[LocomoCategory, int]:
    if isinstance(value, bool):
        raise LocomoFormatError(f"{where}: boolean is not a valid category")
    if isinstance(value, int):
        raw = value
    elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
        raw = int(value.strip())
    else:
        raise LocomoFormatError(f"{where}: expected an integer category, got {value!r}")
    return LocomoCategory.from_code(raw), raw


def _parse_turn(
    raw: object, *, session_index: int, session_date: str | None, turn_index: int, where: str
) -> LocomoTurn:
    turn = _as_mapping(raw, where)
    urls_raw = turn.get("img_url")
    urls: tuple[str, ...] = ()
    if urls_raw is not None:
        urls = tuple(
            _as_str(url, f"{where}.img_url[{i}]")
            for i, url in enumerate(_as_sequence(urls_raw, f"{where}.img_url"))
        )
    return LocomoTurn(
        dia_id=_as_str(turn.get("dia_id"), f"{where}.dia_id"),
        speaker=_as_str(turn.get("speaker"), f"{where}.speaker"),
        text=_as_str(turn.get("text"), f"{where}.text"),
        session_index=session_index,
        session_date=session_date,
        turn_index=turn_index,
        image_urls=urls,
        image_caption=_opt_str(turn.get("blip_caption"), f"{where}.blip_caption"),
        image_query=_opt_str(turn.get("query"), f"{where}.query"),
    )


def _parse_sessions(conversation: Mapping[str, object], where: str) -> tuple[LocomoSession, ...]:
    """Collect ``session_N`` arrays in numeric order.

    The corpus ships ``session_N_date_time`` keys for sessions that carry no turn
    list (a sample with 19 sessions still declares dates up to 35), so the turn
    arrays — not the date keys — decide what exists.
    """
    sessions: list[LocomoSession] = []
    for key, value in conversation.items():
        match = _SESSION_KEY.match(key)
        if match is None or not isinstance(value, list):
            continue
        index = int(match.group(1))
        date_time = _opt_str(
            conversation.get(f"{key}{_DATE_KEY_SUFFIX}"), f"{where}.{key}{_DATE_KEY_SUFFIX}"
        )
        turns = tuple(
            _parse_turn(
                raw,
                session_index=index,
                session_date=date_time,
                turn_index=turn_index,
                where=f"{where}.{key}[{turn_index}]",
            )
            for turn_index, raw in enumerate(value)
        )
        sessions.append(LocomoSession(index=index, date_time=date_time, turns=turns))
    sessions.sort(key=lambda session: session.index)
    return tuple(sessions)


def _parse_questions(raw_qa: object, *, sample_id: str, where: str) -> tuple[LocomoQuestion, ...]:
    questions: list[LocomoQuestion] = []
    for i, raw in enumerate(_as_sequence(raw_qa, where)):
        item_where = f"{where}[{i}]"
        item = _as_mapping(raw, item_where)
        category, raw_category = _as_category(item.get("category"), f"{item_where}.category")
        evidence_raw = item.get("evidence")
        evidence: tuple[str, ...] = ()
        if evidence_raw is not None:
            evidence = tuple(
                _as_str(e, f"{item_where}.evidence[{j}]")
                for j, e in enumerate(_as_sequence(evidence_raw, f"{item_where}.evidence"))
            )
        questions.append(
            LocomoQuestion(
                question_id=f"{sample_id}:q{i}",
                question=_as_str(item.get("question"), f"{item_where}.question"),
                category=category,
                raw_category=raw_category,
                answer=_as_answer(item.get("answer"), f"{item_where}.answer"),
                adversarial_answer=_opt_str(
                    item.get("adversarial_answer"), f"{item_where}.adversarial_answer"
                ),
                evidence=evidence,
            )
        )
    return tuple(questions)


def _parse_sample(raw: object, *, index: int) -> LocomoSample:
    where = f"sample[{index}]"
    sample = _as_mapping(raw, where)
    sample_id = _as_str(sample.get("sample_id"), f"{where}.sample_id")
    conversation = _as_mapping(sample.get("conversation"), f"{where}.conversation")
    return LocomoSample(
        sample_id=sample_id,
        speaker_a=_as_str(conversation.get("speaker_a"), f"{where}.conversation.speaker_a"),
        speaker_b=_as_str(conversation.get("speaker_b"), f"{where}.conversation.speaker_b"),
        sessions=_parse_sessions(conversation, f"{where}.conversation"),
        questions=_parse_questions(sample.get("qa"), sample_id=sample_id, where=f"{where}.qa"),
    )


def parse_locomo(payload: object) -> tuple[LocomoSample, ...]:
    """Parse an already-decoded LoCoMo payload. Pure — no I/O, no engine."""
    return tuple(
        _parse_sample(raw, index=i) for i, raw in enumerate(_as_sequence(payload, "dataset"))
    )


def resolve_dataset_path(path: str | Path | None = None) -> Path:
    """Explicit path wins, then ``MEMSPINE_LOCOMO_PATH``; never a bundled default.

    Raises ``FileNotFoundError`` with the licence position spelled out, because
    "where is the data" is the first question every new operator asks.
    """
    candidate = path if path is not None else os.environ.get(DATASET_PATH_ENV)
    if not candidate:
        raise FileNotFoundError(
            "No LoCoMo path supplied. memspine does not vendor the corpus (Snap Inc. "
            f"releases it under its own terms): pass an explicit path or set {DATASET_PATH_ENV}."
        )
    resolved = Path(candidate).expanduser()
    if not resolved.is_file():
        raise FileNotFoundError(f"LoCoMo dataset not found at {resolved}")
    return resolved


def load_locomo(
    path: str | Path | None = None,
    *,
    sample_ids: Sequence[str] | None = None,
    limit: int | None = None,
) -> tuple[LocomoSample, ...]:
    """Read and parse a LoCoMo JSON file supplied by the operator.

    ``sample_ids`` filters by ``sample_id`` (order follows the file, not the
    argument); ``limit`` truncates after filtering.
    """
    resolved = resolve_dataset_path(path)
    with resolved.open(encoding="utf-8") as handle:
        payload: object = json.load(handle)
    samples = parse_locomo(payload)
    if sample_ids is not None:
        wanted = set(sample_ids)
        missing = wanted.difference({sample.sample_id for sample in samples})
        if missing:
            raise LocomoFormatError(f"sample_ids not present in {resolved}: {sorted(missing)}")
        samples = tuple(sample for sample in samples if sample.sample_id in wanted)
    if limit is not None:
        samples = samples[:limit]
    return samples


def iter_locomo(
    path: str | Path | None = None,
    *,
    sample_ids: Sequence[str] | None = None,
    limit: int | None = None,
) -> Iterator[LocomoSample]:
    """Typed iterator over samples — the harness's normal entry point."""
    yield from load_locomo(path, sample_ids=sample_ids, limit=limit)


# ── ingestion ────────────────────────────────────────────────────────────────


@runtime_checkable
class SupportsEngineWrite(Protocol):
    """The slice of :class:`memspine.Engine` the ingestion driver needs.

    Structural, not nominal, so the driver is testable without booting a real
    engine (and so the harness can wrap one). ``Engine`` satisfies it — see the
    ``TYPE_CHECKING`` assertion at the bottom of this module.
    """

    async def write_messages(
        self,
        messages: Sequence[Mapping[str, str]],
        namespace: str = ...,
        actor: str = ...,
        session_id: str | None = ...,
        channel: str = ...,
        group_id: str | None = ...,
        tags: list[str] | None = ...,
    ) -> Sequence[object]: ...

    async def sleep(self) -> Mapping[str, Mapping[str, object]]: ...


def sample_namespace(sample: LocomoSample, prefix: str = ADAPTER_NAME) -> str:
    """One namespace per conversation — LoCoMo's sample *is* the user.

    Delegates to :func:`evals.datasets.base.sample_namespace` so both adapters
    namespace the same way (``"locomo/conv-26"``, ``"longmemeval/gpt4_2655b836"``);
    a per-adapter rule was one more place two benchmarks could stop being comparable.
    """
    return _namespace_for(prefix, sample.sample_id)


@dataclass(frozen=True, slots=True)
class IngestOptions:
    """Write-side toggles, bundled so one flag stays one toggle.

    Passing these as an object rather than loose keyword arguments means the
    ablation matrix can construct, log and diff a configuration as a value — and
    means :func:`build_or_load_memory` forwards them without a typing escape.

    Every field except ``on_session`` changes the *constructed memory*, so
    :meth:`digest` folds them into the build cache key. Without that, flipping
    ``sleep_every_sessions`` — pricing the consolidation layer, which is the whole
    reason this harness exists — returns a store that never consolidated, replays the
    lean row's statistics, and puts a full-profile point on the frontier that was
    plotted from a lean build. Nothing fails; the table is just wrong.
    """

    channel: str = DEFAULT_CHANNEL
    role_for_speaker: Callable[[str], str] | None = None
    role_map_id: str = "default"
    """Names the ``role_for_speaker`` mapping for the cache key. A callable has no
    stable identity across processes, so the *operator* labels it: change the mapping,
    change this string. Left at ``"default"`` while ``role_for_speaker`` is ``None``."""

    with_timestamp: bool = True
    sleep_every_sessions: int | None = None
    sleep_at_end: bool = False
    on_session: Callable[[int, int], None] | None = None
    """Progress callback. Deliberately absent from :meth:`digest`: it observes the
    build, it does not change it."""

    def as_dict(self) -> dict[str, object]:
        """The build-affecting fields, as plain data — loggable and diffable."""
        return {
            "channel": self.channel,
            "role_map_id": self.role_map_id if self.role_for_speaker is not None else "default",
            "with_timestamp": self.with_timestamp,
            "sleep_every_sessions": self.sleep_every_sessions,
            "sleep_at_end": self.sleep_at_end,
        }

    def digest(self) -> str:
        """Stable 64-hex digest over :meth:`as_dict` — the cache key's options half."""
        return options_digest(self.as_dict())


async def ingest_sample(
    engine: SupportsEngineWrite,
    sample: LocomoSample,
    *,
    namespace: str | None = None,
    options: IngestOptions | None = None,
) -> IngestionStats:
    """Feed one sample's turns into ``engine`` through the normal write path.

    One session becomes one call to :meth:`Engine.write_messages`, stamped with a
    shared ``session_id`` and ``group_id`` so the M13.2 session detector sees a
    single episode at consolidation time — the same shape :meth:`write_episode`
    produces, but with LoCoMo's own session boundary instead of a content hash.

    Nothing here bypasses the write door. Be precise about what that bills, though:
    these are ``episodic`` writes, so every turn pays firewall assessment, the event
    append, all four projectors, corroboration and link evolution exactly as production
    writes do — but **not** MinHash-LSH dedup and **not** the M4 conflict ladder, which
    ``Engine._write_locked`` gates on ``memory_type == "semantic"``. Reading a dedup
    price out of the LoCoMo write number would therefore be wrong.

    ``IngestOptions.sleep_every_sessions`` runs the sleep cycle mid-ingest
    (consolidate, reorganize, decay, compress); leave it ``None`` for the lean
    profile, where sleep is priced as a separate layer rather than folded into
    the write cost.
    """
    opts = options or IngestOptions()
    resolved_ns = namespace or sample_namespace(sample)
    to_role = opts.role_for_speaker or (lambda _speaker: DEFAULT_SPEAKER_ROLE)
    started = time.perf_counter()
    records = 0
    characters = 0
    sleep_cycles = 0
    total_sessions = len(sample.sessions)

    for position, session in enumerate(sample.sessions, start=1):
        if not session.turns:
            continue
        session_id = f"{sample.sample_id}:session:{session.index}"
        messages: list[Mapping[str, str]] = []
        for turn in session.turns:
            content = turn.render(with_timestamp=opts.with_timestamp)
            characters += len(content)
            messages.append({"role": to_role(turn.speaker), "content": content})
        written = await engine.write_messages(
            messages,
            namespace=resolved_ns,
            channel=opts.channel,
            session_id=session_id,
            group_id=session_id,
            tags=[f"locomo:{sample.sample_id}", f"session:{session.index}"],
        )
        records += len(written)
        every = opts.sleep_every_sessions
        if every is not None and every > 0 and position % every == 0:
            await engine.sleep()
            sleep_cycles += 1
        if opts.on_session is not None:
            opts.on_session(position, total_sessions)

    if opts.sleep_at_end:
        await engine.sleep()
        sleep_cycles += 1

    return IngestionStats(
        sample_id=sample.sample_id,
        namespace=resolved_ns,
        sessions=total_sessions,
        turns=sample.turn_count,
        records=records,
        characters=characters,
        sleep_cycles=sleep_cycles,
        wall_seconds=time.perf_counter() - started,
    )


# ── per-sample memory cache ──────────────────────────────────────────────────


#: Opens an engine bound to the given storage directory. The harness owns config
#: layering (defaults → template → user YAML → env → kwargs), so the factory —
#: not this module — decides the profile and the toggles being priced.
EngineFactory = Callable[[Path], AbstractAsyncContextManager[SupportsEngineWrite]]


def build_content_hash(sample: LocomoSample) -> str:
    """The content half of the cache key, computed on the shared digest.

    Both adapters hash the same way over the same neutral shape, so a stale entry is
    detected identically on both benchmarks.
    """
    return content_digest(
        ADAPTER_NAME, ADAPTER_VERSION, sample.sample_id, sample.to_benchmark_sample().units
    )


@dataclass(frozen=True, slots=True)
class BuildOutcome:
    """Result of :func:`build_or_load_memory`.

    ``stats`` is present either way — freshly measured on a build, replayed from
    the manifest on a hit — so a cached run still reports its write cost instead
    of a blank cell. On a hit ``stats.wall_seconds`` is the *original* build's, which
    ``reused`` is there to disambiguate.
    """

    slot: BuildSlot
    namespace: str
    reused: bool
    stats: IngestionStats
    manifest: BuildManifest | None = field(default=None)


async def build_or_load_memory(
    sample: LocomoSample,
    *,
    cache: BuildCache,
    engine_factory: EngineFactory,
    rebuild: bool = False,
    namespace: str | None = None,
    options: IngestOptions | None = None,
) -> BuildOutcome:
    """Ensure a built store exists for ``sample``, building it only when needed.

    On a hit the engine is never opened: the caller opens its own read-side
    engine against ``outcome.slot.storage_path``. On a miss the slot is wiped,
    the factory opens a write-side engine there, and the manifest is committed
    last so an interrupted build reads as a miss next time.

    The slot is keyed on the digest of the options *this call* will use, not on
    whatever the cache was constructed with, so a run that changes the sleep cadence
    or the provenance channel cannot silently reuse a store built without them.
    """
    opts = options or IngestOptions()
    slot = cache.slot(sample.sample_id, options_hash=opts.digest())
    resolved_ns = namespace or sample_namespace(sample)
    content_hash = build_content_hash(sample)

    if not rebuild and slot.matches(content_hash):
        manifest = slot.read_manifest()
        assert manifest is not None  # matches() already read it
        return BuildOutcome(
            slot=slot,
            namespace=manifest.namespace or resolved_ns,
            reused=True,
            stats=manifest.stats,
            manifest=manifest,
        )

    slot.reset()
    async with engine_factory(slot.storage_path) as engine:
        stats = await ingest_sample(engine, sample, namespace=resolved_ns, options=opts)
    manifest = slot.commit(
        content_hash=content_hash,
        namespace=resolved_ns,
        stats=stats,
        extra={"ingest_options": json.dumps(opts.as_dict(), sort_keys=True)},
    )
    return BuildOutcome(
        slot=slot, namespace=resolved_ns, reused=False, stats=stats, manifest=manifest
    )


@dataclass(frozen=True, slots=True)
class LocomoAdapter:
    """:class:`~evals.datasets.base.DatasetAdapter` over the LoCoMo corpus.

    Yields the neutral :class:`~evals.datasets.base.BenchmarkSample`, so the harness
    drives LoCoMo and LongMemEval through one code path. The richer LoCoMo records stay
    available through :func:`load_locomo` for anything that needs them.
    """

    limit: int | None = None
    sample_ids: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return ADAPTER_NAME

    def load(self, path: str | Path) -> Iterator[BenchmarkSample]:
        for sample in load_locomo(path, sample_ids=self.sample_ids, limit=self.limit):
            yield sample.to_benchmark_sample()


def adapter() -> LocomoAdapter:
    """Zero-argument factory, the shape :data:`evals.datasets.base.LOADERS` resolves."""
    return LocomoAdapter()


def load_samples(
    path: str | Path,
    *,
    limit: int | None = None,
    sample_ids: Sequence[str] = (),
) -> Iterator[BenchmarkSample]:
    """Stream neutral samples. The mirror of ``longmemeval.load_samples``."""
    yield from LocomoAdapter(limit=limit, sample_ids=tuple(sample_ids)).load(path)


if TYPE_CHECKING:  # pragma: no cover — a compile-time conformance proof, not a runtime import.
    from memspine import Engine

    def _engine_conforms(engine: Engine) -> SupportsEngineWrite:
        """Fails type-checking the moment ``Engine``'s write door drifts from
        :class:`SupportsEngineWrite`, which is the point."""
        return engine
