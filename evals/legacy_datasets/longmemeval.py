"""LongMemEval dataset adapter for the evals harness (D-35: outside the wheel).

LongMemEval (Wu et al., ICLR 2025) is 500 curated questions, each embedded in its **own**
chat history. ``longmemeval_s`` is ~115K tokens over ~40 sessions per question;
``longmemeval_m`` scales the same questions to ~500 sessions. It probes five long-term
memory abilities: information extraction, multi-session reasoning, temporal reasoning,
knowledge updates, and **abstention**.

**No data is vendored.** The loader reads a user-supplied path to a file downloaded from
``xiaowu0162/longmemeval-cleaned`` (``longmemeval_s_cleaned.json``,
``longmemeval_m_cleaned.json`` or ``longmemeval_oracle.json`` — all three share one schema).

On-disk schema (a top-level JSON array; field names verified against the upstream
GitHub README's field table and the ``longmemeval_oracle.json`` header — the Hugging
Face dataset card itself carries only licence front-matter)::

    {
      "question_id":          "gpt4_2655b836",       # "_abs" suffix ⇒ abstention instance
      "question_type":        "temporal-reasoning",
      "question":             "...",
      "answer":               "...",
      "question_date":        "2023/04/10 (Mon) 23:07",
      "haystack_dates":       ["2023/04/10 (Mon) 17:50", ...],   # parallel to the two below
      "haystack_session_ids": ["answer_4be1b6b4_2", ...],
      "haystack_sessions":    [[{"role": ..., "content": ..., "has_answer": bool}, ...], ...],
      "answer_session_ids":   ["answer_4be1b6b4_1", ...]         # optional
    }

Two things worth knowing before reading the code:

* **Session order is per-file, and only ``oracle`` is unsorted.** Upstream states that
  ``haystack_session_ids`` is "sorted by timestamp for ``longmemeval_s.json`` and
  ``longmemeval_m.json``; not sorted for ``longmemeval_oracle.json``", and the released
  ``oracle`` file bears that out — its first instance carries dates ``17:50, 14:47,
  17:15`` under ids ``_2, _3, _1``. Ingesting *that* file in array order would present a
  knowledge-update pair in the wrong direction, so :data:`SessionOrder` defaults to
  ``CHRONOLOGICAL``, which is a **no-op on ``_s`` and ``_m``** and a correction on
  ``oracle``. ``SessionOrder.FILE`` preserves the raw order. Note the cost of the
  default: with ``strict=True`` a single unparseable date now hard-fails a file that
  upstream had already sorted correctly.
* **Abstention is an outcome, not a wrong answer.** See :class:`AnswerOutcome` and
  :func:`scored_correct`: *what the system did* (answered / abstained / errored) is kept
  orthogonal to *whether that was right*, because one whole LongMemEval ability is
  knowing when to decline.

Cost discipline (MAGMA_HARVEST §3): this module reports history size
(:attr:`BenchmarkSample.turn_count`, :attr:`BenchmarkSample.char_count`) so a run can
publish the (accuracy, tokens, latency) triplet rather than accuracy alone.

The dataset-agnostic half of this adapter — the sample model, the ingestion driver
protocol and the build cache — now lives in :mod:`evals.legacy_datasets.base` and is shared
with the LoCoMo adapter. Names re-exported here are aliases, not copies.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

import orjson

from .base import (
    Ability,
    AnswerOutcome,
    BenchmarkQuery,
    BenchmarkSample,
    BuildCache,
    ChatTurn,
    DatasetAdapter,
    IngestionDriver,
    IngestUnit,
    content_digest,
    drive_ingestion,
    is_false_abstention,
    sample_namespace,
    scored_correct,
)

__all__ = [
    "ABILITY_BY_QUESTION_TYPE",
    "ABSTENTION_SUFFIX",
    "ADAPTER_NAME",
    "ADAPTER_VERSION",
    "Ability",
    "AnswerOutcome",
    "BenchmarkQuery",
    "BenchmarkSample",
    "BuildCache",
    "ChatTurn",
    "DatasetAdapter",
    "IngestUnit",
    "IngestionDriver",
    "LongMemEvalAdapter",
    "LongMemEvalFormatError",
    "SessionOrder",
    "adapter",
    "build_sample",
    "drive_ingestion",
    "is_false_abstention",
    "load_samples",
    "scored_correct",
]

ADAPTER_NAME = "longmemeval"
"""Dataset name: the first namespace segment and the cache-tree root directory."""

ADAPTER_VERSION = "1"
"""Bumped whenever a change here alters what gets ingested; folded into every cache key."""


# ---------------------------------------------------------------------------
# Shared contract - now in evals/legacy_datasets/base.py
#
# The sample model (ChatTurn / IngestUnit / BenchmarkQuery / BenchmarkSample), the
# outcome vocabulary (Ability / AnswerOutcome / scored_correct), the ingestion-driver
# protocol and the build cache used to live here, with a note asking whoever landed
# the second adapter to hoist them. They now live in `base.py` and BOTH adapters
# import them from there; the names are re-exported at the top of this module so its
# public surface is unchanged.
#
# `SampleCache` is gone. Use `base.BuildCache`, whose key also covers the engine
# config and the ingest options - without those two a run under one governance
# configuration silently reuses a memory built under another, which does not fail,
# it reports.
# ---------------------------------------------------------------------------
# ─────────────────────────────────────────────────────────────────────────────
# LongMemEval specifics
# ─────────────────────────────────────────────────────────────────────────────


class LongMemEvalFormatError(ValueError):
    """The supplied file does not match the documented LongMemEval schema."""


ABSTENTION_SUFFIX = "_abs"
"""Upstream marker: "If ``question_id`` ends with ``_abs``, then the question is an
``abstention`` question." (LongMemEval README.)"""

ABILITY_BY_QUESTION_TYPE: Mapping[str, Ability] = {
    "single-session-user": Ability.INFORMATION_EXTRACTION,
    "single-session-assistant": Ability.INFORMATION_EXTRACTION,
    "single-session-preference": Ability.PREFERENCE,
    "multi-session": Ability.MULTI_SESSION_REASONING,
    "knowledge-update": Ability.KNOWLEDGE_UPDATE,
    "temporal-reasoning": Ability.TEMPORAL_REASONING,
}
"""Question type → ability. **This grouping is ours, not upstream's**: the LongMemEval
README publishes only a ``name in data`` → ``official name`` table, no ability column.

The one judgement call is ``single-session-preference``, which the ICLR paper describes
as testing "whether the model can utilize the user information to generate a personalized
response" and evaluates separately from single-session recall (its own human-eval table
uses *single-session-user* alone for the information-extraction column). Folding those 30
questions into information extraction would make our IE number non-comparable with every
other paper reporting this benchmark, so they get :attr:`Ability.PREFERENCE`.

Abstention is *not* a question type: it is signalled by the id suffix and overrides the
mapping. Per-``question_type`` statistics are kept verbatim regardless, so any consumer
that prefers a different grouping can rebuild it from the stored labels."""


class SessionOrder(StrEnum):
    """Order in which history sessions are handed to the memory system.

    ``CHRONOLOGICAL`` sorts by parsed ``haystack_dates`` (ties broken by file position).
    ``FILE`` preserves the array order, which the released data shows is **not**
    chronological — see the module docstring.
    """

    CHRONOLOGICAL = "chronological"
    FILE = "file"


_DATE = re.compile(
    r"^\s*(\d{4})/(\d{1,2})/(\d{1,2})(?:\s*\([A-Za-z]{3}\))?\s+(\d{1,2}):(\d{2})\s*$"
)


def _parse_date(raw: str) -> datetime | None:
    """Parse ``2023/04/10 (Mon) 23:07``; ``None`` when the string does not match.

    Hand-rolled rather than ``strptime`` because ``%a`` is locale-dependent and the
    weekday is redundant with the date anyway.
    """
    match = _DATE.match(raw)
    if match is None:
        return None
    year, month, day, hour, minute = (int(group) for group in match.groups())
    try:
        # Naive on purpose: LongMemEval carries no zone, and inventing one would make
        # temporal-reasoning comparisons wrong in a way that is hard to see.
        return datetime(year, month, day, hour, minute)
    except ValueError:
        return None


def _as_list(value: object, where: str) -> list[object]:
    if not isinstance(value, list):
        raise LongMemEvalFormatError(f"{where}: expected a JSON array, got {type(value).__name__}")
    return value


def _as_mapping(value: object, where: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise LongMemEvalFormatError(f"{where}: expected a JSON object, got {type(value).__name__}")
    return value


def _req_str(data: Mapping[str, object], key: str, where: str) -> str:
    if key not in data:
        raise LongMemEvalFormatError(f"{where}: missing required field {key!r}")
    value = data[key]
    if not isinstance(value, str):
        raise LongMemEvalFormatError(
            f"{where}: field {key!r} must be a string, got {type(value).__name__}"
        )
    return value


def _req_str_list(data: Mapping[str, object], key: str, where: str) -> list[str]:
    if key not in data:
        raise LongMemEvalFormatError(f"{where}: missing required field {key!r}")
    items = _as_list(data[key], f"{where}.{key}")
    out: list[str] = []
    for i, item in enumerate(items):
        if not isinstance(item, str):
            raise LongMemEvalFormatError(
                f"{where}.{key}[{i}]: must be a string, got {type(item).__name__}"
            )
        out.append(item)
    return out


def _opt_str_list(data: Mapping[str, object], key: str, where: str) -> list[str]:
    """``answer_session_ids`` is absent from some releases; treat it as empty."""
    if key not in data:
        return []
    return _req_str_list(data, key, where)


def _turns(raw_session: object, where: str) -> tuple[ChatTurn, ...]:
    turns: list[ChatTurn] = []
    for i, raw_turn in enumerate(_as_list(raw_session, where)):
        turn_where = f"{where}[{i}]"
        data = _as_mapping(raw_turn, turn_where)
        has_answer = data.get("has_answer", False)
        turns.append(
            ChatTurn(
                role=_req_str(data, "role", turn_where),
                content=_req_str(data, "content", turn_where),
                has_answer=bool(has_answer),
            )
        )
    return tuple(turns)


def _ability_for(question_id: str, question_type: str, *, strict: bool) -> tuple[Ability, bool]:
    """Resolve (ability, expects_abstention).

    The id suffix is authoritative for abstention. Some releases also carry the suffix
    on ``question_type``; it is stripped before the table lookup so both shapes resolve.
    """
    expects_abstention = question_id.endswith(ABSTENTION_SUFFIX)
    base_type = question_type.removesuffix(ABSTENTION_SUFFIX)
    if expects_abstention:
        return Ability.ABSTENTION, True
    ability = ABILITY_BY_QUESTION_TYPE.get(base_type)
    if ability is None:
        if strict:
            raise LongMemEvalFormatError(
                f"{question_id}: unknown question_type {question_type!r} "
                f"(known: {sorted(ABILITY_BY_QUESTION_TYPE)}); "
                "pass strict=False to label it Ability.OTHER instead"
            )
        return Ability.OTHER, False
    return ability, False


def build_sample(
    raw: object,
    *,
    order: SessionOrder = SessionOrder.CHRONOLOGICAL,
    strict: bool = True,
) -> BenchmarkSample:
    """Turn one raw LongMemEval instance into a :class:`BenchmarkSample`.

    Separated from :func:`load_samples` so a fixture — or a single instance pulled from
    a larger file — can be built without touching the filesystem.
    """
    data = _as_mapping(raw, "instance")
    question_id = _req_str(data, "question_id", "instance")
    where = f"instance {question_id!r}"

    question_type = _req_str(data, "question_type", where)
    ability, expects_abstention = _ability_for(question_id, question_type, strict=strict)

    session_ids = _req_str_list(data, "haystack_session_ids", where)
    dates = _req_str_list(data, "haystack_dates", where)
    if "haystack_sessions" not in data:
        raise LongMemEvalFormatError(f"{where}: missing required field 'haystack_sessions'")
    sessions = _as_list(data["haystack_sessions"], f"{where}.haystack_sessions")
    if not (len(session_ids) == len(dates) == len(sessions)):
        raise LongMemEvalFormatError(
            f"{where}: haystack_session_ids ({len(session_ids)}), haystack_dates "
            f"({len(dates)}) and haystack_sessions ({len(sessions)}) must be parallel arrays"
        )
    evidence_ids = frozenset(_opt_str_list(data, "answer_session_ids", where))

    positions = list(range(len(sessions)))
    applied_order = order
    if order is SessionOrder.CHRONOLOGICAL:
        parsed = [_parse_date(date) for date in dates]
        unparsed = [dates[i] for i, ts in enumerate(parsed) if ts is None]
        if unparsed and strict:
            raise LongMemEvalFormatError(
                f"{where}: unparseable haystack_dates {unparsed[:3]!r} block chronological "
                "ordering; pass order=SessionOrder.FILE or strict=False"
            )
        if unparsed:
            applied_order = SessionOrder.FILE
        else:
            positions.sort(key=lambda i: (parsed[i] or datetime.min, i))

    units: list[IngestUnit] = []
    for index, source_index in enumerate(positions):
        turns = _turns(sessions[source_index], f"{where}.haystack_sessions[{source_index}]")
        unit_id = session_ids[source_index]
        timestamp_raw = dates[source_index]
        units.append(
            IngestUnit(
                unit_id=unit_id,
                index=index,
                timestamp_raw=timestamp_raw,
                timestamp=_parse_date(timestamp_raw),
                turns=turns,
                has_evidence=unit_id in evidence_ids or any(turn.has_answer for turn in turns),
            )
        )

    question_date = _req_str(data, "question_date", where)
    gold = data.get("answer")
    query = BenchmarkQuery(
        query_id=question_id,
        question=_req_str(data, "question", where),
        # Kept verbatim even for abstention instances, where upstream still ships an
        # ``answer`` string: expects_abstention — not a null gold — drives scoring.
        gold_answer=gold if isinstance(gold, str) else None,
        ability=ability,
        question_type=question_type,
        expects_abstention=expects_abstention,
        asked_at_raw=question_date,
        asked_at=_parse_date(question_date),
        evidence_unit_ids=tuple(unit.unit_id for unit in units if unit.has_evidence),
        # The order actually applied, which is not always the order requested: with
        # strict=False an unparseable date silently falls back to file order, and a
        # sample that claims CHRONOLOGICAL while delivering FILE is a protocol
        # difference nobody would otherwise see.
        meta={"session_order": applied_order.value, "requested_session_order": order.value},
    )

    ordered_units = tuple(units)
    return BenchmarkSample(
        dataset=ADAPTER_NAME,
        sample_id=question_id,
        namespace=sample_namespace(ADAPTER_NAME, question_id),
        units=ordered_units,
        queries=(query,),
        content_hash=content_digest(ADAPTER_NAME, ADAPTER_VERSION, question_id, ordered_units),
    )


def load_samples(
    path: str | Path,
    *,
    limit: int | None = None,
    question_ids: Collection[str] | None = None,
    abilities: Collection[Ability] | None = None,
    order: SessionOrder = SessionOrder.CHRONOLOGICAL,
    strict: bool = True,
) -> Iterator[BenchmarkSample]:
    """Stream samples from a user-supplied LongMemEval JSON file.

    ``path`` points at ``longmemeval_s_cleaned.json`` (or the ``_m`` / ``oracle``
    siblings) downloaded by the user; nothing here fetches or vendors data.

    Filters compose: ``question_ids`` and ``abilities`` are applied first, ``limit`` caps
    what survives. The file is parsed eagerly (a single JSON array leaves no cheaper
    option without a streaming parser) and samples are *built* lazily.

    **Lazy building does not bound peak memory.** ``read_bytes`` plus ``orjson.loads``
    materialises the whole file as bytes and then as a Python object graph before the
    first sample exists; laziness saves only the dataclass layer on top. On the ~2.7 GB
    ``_m`` split expect this path to need well over 10 GB of RAM, and quite possibly to
    fail. ``_s`` and ``oracle`` are fine. A streaming parser (ijson) is the fix if ``_m``
    is ever needed.
    """
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"LongMemEval data file not found: {source}")
    payload = orjson.loads(source.read_bytes())
    instances = _as_list(payload, str(source))
    wanted_ids = frozenset(question_ids) if question_ids is not None else None
    wanted_abilities = frozenset(abilities) if abilities is not None else None

    def _stream() -> Iterator[BenchmarkSample]:
        emitted = 0
        for raw in instances:
            if limit is not None and emitted >= limit:
                return
            if wanted_ids is not None:
                data = _as_mapping(raw, str(source))
                if _req_str(data, "question_id", str(source)) not in wanted_ids:
                    continue
            sample = build_sample(raw, order=order, strict=strict)
            if wanted_abilities is not None and not any(
                query.ability in wanted_abilities for query in sample.queries
            ):
                continue
            emitted += 1
            yield sample

    return _stream()


@dataclass(frozen=True, slots=True)
class LongMemEvalAdapter:
    """:class:`DatasetAdapter` implementation — options bound once, path passed at load."""

    limit: int | None = None
    question_ids: frozenset[str] | None = None
    abilities: frozenset[Ability] | None = None
    order: SessionOrder = SessionOrder.CHRONOLOGICAL
    strict: bool = True

    @property
    def name(self) -> str:
        return ADAPTER_NAME

    def load(self, path: str | Path) -> Iterator[BenchmarkSample]:
        return load_samples(
            path,
            limit=self.limit,
            question_ids=self.question_ids,
            abilities=self.abilities,
            order=self.order,
            strict=self.strict,
        )


def adapter() -> LongMemEvalAdapter:
    """Zero-argument factory, the shape :data:`evals.legacy_datasets.base.LOADERS` resolves.

    Defaults only: a run that needs filters or FILE ordering constructs
    :class:`LongMemEvalAdapter` directly rather than going through the registry.
    """
    return LongMemEvalAdapter()
