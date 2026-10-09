"""The contract every evals dataset adapter shares.

Two adapters landed in parallel (``locomo.py``, ``longmemeval.py``) and each grew its
own sample model, its own ingestion driver and its own build cache. Two benchmarks
cannot report one comparable frontier through two incompatible cost types, so this
module owns the parts that must be identical and the adapters own only what is
genuinely dataset-specific (parsing, taxonomy, ordering).

What lives here
---------------
**A neutral sample model.** :class:`BenchmarkSample` is what the harness consumes:
an ordered history of :class:`IngestUnit` plus the :class:`BenchmarkQuery` objects asked
against it. LongMemEval gives each question its own haystack (one query per sample);
LoCoMo asks many questions of one conversation. The tuple carries both.

**One build cache.** Construction is the expensive half of a benchmark run and read-side
ablations are the cheap half, so a built store is cached per sample and reused
(MAGMA_HARVEST §2.3). The key is

    (dataset, sample_id, profile, config_hash, options_hash, adapter_version)

and the *content* hash is checked separately, inside the manifest. Every one of those
six components is load-bearing, and the two that were missing from the original adapters
are the two that matter most for this project:

* ``config_hash`` — the engine configuration. Without it a run under one governance
  configuration silently reuses a memory built under another. That is not a slow run,
  it is a wrong row on the frontier.
* ``options_hash`` — the *ingestion* options (channel, timestamping, sleep cadence,
  speaker-role mapping). Sleep cadence is a governance layer this project exists to
  price; if it is absent from the key, pricing it measures nothing and reports a number.

:func:`BuildCache.slot` takes an explicit ``options_hash`` override so a driver derives
it from the options it is about to use rather than trusting the caller to have matched
them — the mismatch cannot be constructed.

**Ingestion accounting.** :class:`IngestionStats` is the write-side half of the
(accuracy, tokens, latency) triplet, per sample, and is replayed out of the manifest on
a cache hit so a reused build still reports its cost instead of a blank cell.

What does *not* live here
-------------------------
Parsing, per-dataset taxonomies, session ordering, and anything that needs a benchmark's
own vocabulary. Scoring, judging, CLI wiring and engine configuration live elsewhere in
``evals/``; adapters import none of it, and neither does this module.

**No corpus is vendored anywhere under ``evals/``.** Benchmark data carries its own
licence and is never committed; every loader reads from a path the operator supplies.

Outside the wheel (D-19/D-35): nothing in ``src/memspine`` may import this.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shutil
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Final, Protocol, Self, runtime_checkable

__all__ = [
    "LOADERS",
    "MANIFEST_VERSION",
    "Ability",
    "AnswerOutcome",
    "BenchmarkQuery",
    "BenchmarkSample",
    "BuildCache",
    "BuildKey",
    "BuildManifest",
    "BuildSlot",
    "CacheContractError",
    "ChatTurn",
    "DatasetAdapter",
    "IngestUnit",
    "IngestionDriver",
    "IngestionStats",
    "config_hash",
    "content_digest",
    "drive_ingestion",
    "is_false_abstention",
    "known_datasets",
    "load_dataset",
    "options_digest",
    "sample_namespace",
    "scored_correct",
]

#: Bumped when the on-disk manifest layout changes. An older manifest reads as a miss.
MANIFEST_VERSION: Final = 2


class CacheContractError(RuntimeError):
    """A build cache was used in a way that could return a memory built differently."""


# ─────────────────────────────────────────────────────────────────────────────
# Outcome vocabulary
# ─────────────────────────────────────────────────────────────────────────────


class Ability(StrEnum):
    """Coarse capability label for a question.

    The five names are LongMemEval's abilities; other datasets map their own taxonomy
    onto them, and every adapter also keeps its dataset-native label verbatim
    (:attr:`BenchmarkQuery.question_type`) so per-type statistics survive the grouping.

    ``OTHER`` exists so a non-strict load of a drifted file still yields usable records;
    a strict load raises instead (D-10: hard-fail clearly).
    """

    INFORMATION_EXTRACTION = "information_extraction"
    MULTI_SESSION_REASONING = "multi_session_reasoning"
    TEMPORAL_REASONING = "temporal_reasoning"
    KNOWLEDGE_UPDATE = "knowledge_update"
    PREFERENCE = "preference"
    """Personalised-response questions. Kept separate from information extraction
    because upstream evaluates it separately, and folding it in makes the IE column
    non-comparable with every other paper reporting the benchmark."""

    ABSTENTION = "abstention"
    OTHER = "other"


class AnswerOutcome(StrEnum):
    """What the system *did* with a query — deliberately orthogonal to correctness.

    A single ``correct: bool`` cannot express these benchmarks: declining to answer is
    the right move on an abstention question and the wrong move on an answerable one,
    and neither case is "answered incorrectly". Pair this with the judge's gold-match
    verdict and derive correctness via :func:`scored_correct`.
    """

    ANSWERED = "answered"
    ABSTAINED = "abstained"
    ERROR = "error"


def scored_correct(
    query: BenchmarkQuery,
    outcome: AnswerOutcome,
    *,
    answer_matches_gold: bool | None = None,
) -> bool:
    """Correctness under the benchmark's own rule.

    An abstention question is correct exactly when the system abstained. An answerable
    one is correct when the system answered *and* the judge matched the gold answer.

    ``answer_matches_gold`` is **required** whenever the system actually answered an
    answerable question: defaulting an unjudged item to ``False`` would report an
    unscored run as a run that scored zero, which is the exact confusion this harness
    refuses everywhere else.
    """
    if outcome is AnswerOutcome.ERROR:
        return False
    if query.expects_abstention:
        return outcome is AnswerOutcome.ABSTAINED
    if outcome is AnswerOutcome.ABSTAINED:
        return False
    if answer_matches_gold is None:
        raise ValueError(
            f"answer_matches_gold is required to score answered query {query.query_id!r}: "
            "a judge verdict has not been supplied, and an unjudged item is not a failed one"
        )
    return answer_matches_gold


def is_false_abstention(query: BenchmarkQuery, outcome: AnswerOutcome) -> bool:
    """Declined a question that had an answer — the over-refusal failure mode, which a
    pooled accuracy figure hides."""
    return outcome is AnswerOutcome.ABSTAINED and not query.expects_abstention


# ─────────────────────────────────────────────────────────────────────────────
# The neutral sample model
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ChatTurn:
    """One turn of a history session."""

    role: str
    content: str
    has_answer: bool = False
    """Upstream annotation, where the benchmark provides one: this turn carries evidence
    for the sample's question."""

    turn_id: str = ""
    """The dataset's own turn identifier where it has one (LoCoMo's ``dia_id``), so
    retrieved evidence can be linked back to gold evidence pointers."""

    def as_message(self) -> dict[str, str]:
        """OpenAI-style mapping, the shape ``Engine.write_episode`` consumes."""
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True, slots=True)
class IngestUnit:
    """One unit of history handed to the memory system — one session.

    The unit, not the turn, is the ingestion granularity: memspine's write door stamps a
    shared session id and ``group_id`` across a transcript, which is what makes
    session-boundary detection at consolidation time work.
    """

    unit_id: str
    index: int
    """Position in the ordered history (0-based), *after* any reordering is applied."""

    timestamp_raw: str
    """The dataset's own date string, kept verbatim — never normalised away."""

    timestamp: datetime | None
    turns: tuple[ChatTurn, ...]
    has_evidence: bool = False
    """This session is named in the gold evidence, or contains a ``has_answer`` turn."""

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    @property
    def char_count(self) -> int:
        return sum(len(turn.content) for turn in self.turns)

    def messages(self) -> list[dict[str, str]]:
        return [turn.as_message() for turn in self.turns]


@dataclass(frozen=True, slots=True)
class BenchmarkQuery:
    """One question asked against a sample's history."""

    query_id: str
    question: str
    gold_answer: str | None
    ability: Ability
    question_type: str
    """The dataset's own label, verbatim — per-type stats must not be lost to the
    coarser :class:`Ability` grouping (MAGMA_HARVEST §2.4)."""

    expects_abstention: bool = False
    asked_at_raw: str = ""
    asked_at: datetime | None = None
    evidence_unit_ids: tuple[str, ...] = ()
    """Sessions containing the evidence — ground truth for retrieval-recall metrics."""

    evidence_turn_ids: tuple[str, ...] = ()
    """Turn-level evidence pointers where the benchmark publishes them (LoCoMo does)."""

    meta: Mapping[str, Any] = field(default_factory=dict)
    """Dataset-specific fields a judge may need, carried into ``ItemResult.question_meta``
    so re-judging never has to reopen the corpus."""


@dataclass(frozen=True, slots=True)
class BenchmarkSample:
    """One history plus the queries asked against it — the unit of caching and of work."""

    dataset: str
    sample_id: str
    namespace: str
    units: tuple[IngestUnit, ...]
    queries: tuple[BenchmarkQuery, ...]
    content_hash: str
    """Fingerprint of the *ingestible* content only (see :func:`content_digest`)."""

    @property
    def unit_count(self) -> int:
        return len(self.units)

    @property
    def turn_count(self) -> int:
        return sum(unit.turn_count for unit in self.units)

    @property
    def char_count(self) -> int:
        return sum(unit.char_count for unit in self.units)

    def category_counts(self) -> Mapping[str, int]:
        """Report-ready census of this sample's questions, by dataset-native type."""
        counts: dict[str, int] = {}
        for query in self.queries:
            counts[query.question_type] = counts.get(query.question_type, 0) + 1
        return counts


@runtime_checkable
class DatasetAdapter(Protocol):
    """What the harness needs of a dataset: a name and a stream of samples.

    Per-dataset options (split, ordering, filters) are constructor arguments of the
    concrete adapter, so this stays a two-member protocol every dataset can satisfy.
    """

    @property
    def name(self) -> str: ...

    def load(self, path: str | Path) -> Iterator[BenchmarkSample]: ...


@runtime_checkable
class IngestionDriver(Protocol):
    """The harness's side of ingestion: how a sample's history enters a memory system.

    A memspine driver maps this onto the engine's write door per unit and
    ``Engine.sleep`` in :meth:`end_sample`. Keeping ``begin``/``end`` explicit is what
    lets a driver open one namespace-scoped engine per sample and run the sleep cycle
    exactly once, after the whole history has landed.
    """

    async def begin_sample(self, sample: BenchmarkSample) -> None: ...

    async def ingest_unit(self, sample: BenchmarkSample, unit: IngestUnit) -> None: ...

    async def end_sample(self, sample: BenchmarkSample) -> None: ...


async def drive_ingestion(sample: BenchmarkSample, driver: IngestionDriver) -> int:
    """Push one sample's history through ``driver`` in order; returns units ingested."""
    await driver.begin_sample(sample)
    for unit in sample.units:
        await driver.ingest_unit(sample, unit)
    await driver.end_sample(sample)
    return len(sample.units)


# ─────────────────────────────────────────────────────────────────────────────
# Namespacing
# ─────────────────────────────────────────────────────────────────────────────

_NS_INVALID: Final = re.compile(r"[^a-z0-9_-]+")


def sample_namespace(dataset: str, sample_id: str) -> str:
    """``"{dataset}/{slug}"``, accepted by memspine's ``validate_namespace`` grammar.

    One namespace per sample: the sample *is* the user, and cross-sample leakage would
    be indistinguishable from a retrieval win. When slugification is lossy — an id that
    differed only in case or punctuation — a short digest is appended, because two
    samples sharing a namespace is cross-contamination reported as a score. Real
    LoCoMo (``conv-26``) and LongMemEval (``gpt4_2655b836``) ids never trigger it.
    """
    lowered = sample_id.lower()
    slug = _NS_INVALID.sub("-", lowered).strip("-_")
    if not slug:
        slug = "sample"
    elif not slug[0].isalnum():
        slug = f"s{slug}"
    if slug != lowered or lowered != sample_id:
        suffix = hashlib.sha256(sample_id.encode("utf-8")).hexdigest()[:8]
        slug = f"{slug}-{suffix}"
    prefix = _NS_INVALID.sub("-", dataset.lower()).strip("-_") or "dataset"
    return f"{prefix}/{slug}"


# ─────────────────────────────────────────────────────────────────────────────
# Digests
# ─────────────────────────────────────────────────────────────────────────────


def _canonical(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def config_hash(config: Mapping[str, object]) -> str:
    """Stable 64-hex digest of an engine configuration.

    Feed it ``Engine.describe()`` or a dumped ``MemspineConfig``. Deliberately
    ``hashlib`` and canonical JSON rather than :func:`hash`, which is salted per process
    and would silently miss the cache on every run.
    """
    return hashlib.sha256(_canonical(config).encode("utf-8")).hexdigest()


def options_digest(options: Mapping[str, object]) -> str:
    """Stable 64-hex digest of the *ingestion* options a build was made with.

    Separate from :func:`config_hash` because they come from different places: the
    engine config is memspine's, the ingest options are the adapter's (channel,
    timestamping, sleep cadence, role mapping). Both change the constructed memory, so
    both are in the key.
    """
    return hashlib.sha256(_canonical(options).encode("utf-8")).hexdigest()


def content_digest(
    dataset: str,
    adapter_version: str,
    sample_id: str,
    units: Sequence[IngestUnit],
) -> str:
    """Stable digest over everything ingestion depends on, in its final order.

    Queries are excluded on purpose: the cache holds a *built memory*, which depends on
    the history and not on what is later asked of it. Ordering is included, because
    presenting a knowledge-update pair in the wrong direction is a different memory.
    """
    digest = hashlib.sha256()
    digest.update(f"{dataset}\x00{adapter_version}\x00{sample_id}".encode())
    for unit in units:
        digest.update(b"\x01")
        digest.update(unit.unit_id.encode("utf-8"))
        digest.update(b"\x02")
        digest.update(unit.timestamp_raw.encode("utf-8"))
        for turn in unit.turns:
            digest.update(b"\x03")
            digest.update(turn.role.encode("utf-8"))
            digest.update(b"\x04")
            digest.update(turn.content.encode("utf-8"))
    return digest.hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# Ingestion accounting
# ─────────────────────────────────────────────────────────────────────────────


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value)


@dataclass(frozen=True, slots=True)
class IngestionStats:
    """The write-side half of the cost/capability frontier, per sample.

    Persisted in the build manifest and replayed on a cache hit, so a reused build still
    reports what constructing it cost. ``wall_seconds`` on a hit is therefore the
    *original* build's wall time, not this run's — an aggregator that sums it across
    runs double-counts; the ``reused`` flag on the outcome disambiguates.
    """

    sample_id: str
    namespace: str
    sessions: int
    """Ingest units actually written (LoCoMo sessions, LongMemEval haystack sessions)."""

    turns: int
    records: int
    characters: int
    sleep_cycles: int
    wall_seconds: float

    def as_dict(self) -> dict[str, object]:
        return {
            "sample_id": self.sample_id,
            "namespace": self.namespace,
            "sessions": self.sessions,
            "turns": self.turns,
            "records": self.records,
            "characters": self.characters,
            "sleep_cycles": self.sleep_cycles,
            "wall_seconds": self.wall_seconds,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Self:
        return cls(
            sample_id=str(data.get("sample_id", "")),
            namespace=str(data.get("namespace", "")),
            sessions=int(_number(data.get("sessions"))),
            turns=int(_number(data.get("turns"))),
            records=int(_number(data.get("records"))),
            characters=int(_number(data.get("characters"))),
            sleep_cycles=int(_number(data.get("sleep_cycles"))),
            wall_seconds=float(_number(data.get("wall_seconds"))),
        )


# ─────────────────────────────────────────────────────────────────────────────
# The build cache
# ─────────────────────────────────────────────────────────────────────────────

_SLUG_UNSAFE: Final = re.compile(r"[^A-Za-z0-9_.-]")


@dataclass(frozen=True, slots=True)
class BuildKey:
    """Everything that determines *which* memory a cached build is.

    Six components, every one load-bearing. Drop any and some pair of runs shares a
    slot while differing in what was built — which does not fail, it reports.
    """

    dataset: str
    sample_id: str
    profile: str
    config_hash: str
    options_hash: str = ""
    adapter_version: str = "1"

    def slug(self) -> str:
        """Filesystem-safe directory name. Digests are truncated for readability; the
        manifest holds the full values and is what :meth:`BuildSlot.matches` compares."""
        sample = _SLUG_UNSAFE.sub("_", self.sample_id)
        profile = _SLUG_UNSAFE.sub("_", self.profile)
        version = _SLUG_UNSAFE.sub("_", self.adapter_version)
        return f"{sample}__{profile}__{self.config_hash[:16]}__{self.options_hash[:8]}__v{version}"


@dataclass(frozen=True, slots=True)
class BuildManifest:
    """Bookkeeping written *after* a successful build — the completion marker."""

    version: int
    key: BuildKey
    content_hash: str
    namespace: str
    built_at: str
    stats: IngestionStats
    extra: Mapping[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "dataset": self.key.dataset,
            "sample_id": self.key.sample_id,
            "profile": self.key.profile,
            "config_hash": self.key.config_hash,
            "options_hash": self.key.options_hash,
            "adapter_version": self.key.adapter_version,
            "content_hash": self.content_hash,
            "namespace": self.namespace,
            "built_at": self.built_at,
            "stats": self.stats.as_dict(),
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Self:
        raw_stats = data.get("stats")
        stats = raw_stats if isinstance(raw_stats, Mapping) else {}
        raw_extra = data.get("extra")
        extra = (
            {str(k): str(v) for k, v in raw_extra.items()} if isinstance(raw_extra, Mapping) else {}
        )
        return cls(
            version=int(_number(data.get("version"))),
            key=BuildKey(
                dataset=str(data.get("dataset", "")),
                sample_id=str(data.get("sample_id", "")),
                profile=str(data.get("profile", "")),
                config_hash=str(data.get("config_hash", "")),
                options_hash=str(data.get("options_hash", "")),
                adapter_version=str(data.get("adapter_version", "1")),
            ),
            content_hash=str(data.get("content_hash", "")),
            namespace=str(data.get("namespace", "")),
            built_at=str(data.get("built_at", "")),
            stats=IngestionStats.from_dict(stats),
            extra=extra,
        )


@dataclass(frozen=True, slots=True)
class BuildSlot:
    """One cached build: a directory holding the engine store plus a manifest."""

    key: BuildKey
    path: Path

    MANIFEST_NAME: ClassVar[str] = "manifest.json"

    @property
    def manifest_path(self) -> Path:
        return self.path / self.MANIFEST_NAME

    @property
    def storage_path(self) -> Path:
        """Where the engine's on-disk store lives. Hand this to ``storage.path``."""
        return self.path / "store"

    def read_manifest(self) -> BuildManifest | None:
        """The manifest, or ``None`` when absent, unreadable or malformed.

        A corrupt manifest is a cache miss, not an error: the recovery is to rebuild,
        and making a truncated file fatal would strand a long ablation run behind a
        manual ``rm``.
        """
        if not self.manifest_path.is_file():
            return None
        try:
            with self.manifest_path.open(encoding="utf-8") as handle:
                data: object = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, Mapping):
            return None
        try:
            return BuildManifest.from_dict(data)
        except (TypeError, ValueError):
            return None

    def matches(self, content_hash: str) -> bool:
        """True only for a complete build of *this* content under *this* key."""
        manifest = self.read_manifest()
        if manifest is None:
            return False
        return (
            manifest.version == MANIFEST_VERSION
            and manifest.key == self.key
            and manifest.content_hash == content_hash
            and self.storage_path.exists()
        )

    def prepare(self, content_hash: str, *, rebuild: bool = False) -> bool:
        """Ready the directory and report whether a build is needed.

        Returns ``False`` when a fresh entry can be reused. Otherwise wipes any stale
        contents (this is what ``--rebuild`` means), recreates the directory, and
        returns ``True``.
        """
        if not rebuild and self.matches(content_hash):
            return False
        self.reset()
        return True

    def reset(self) -> None:
        """Wipe the slot. Removes the manifest with it, so a crashed rebuild can never
        leave a completion marker standing over a half-built store."""
        if self.path.exists():
            shutil.rmtree(self.path)
        self.storage_path.mkdir(parents=True, exist_ok=True)

    def commit(
        self,
        *,
        content_hash: str,
        namespace: str,
        stats: IngestionStats,
        extra: Mapping[str, str] | None = None,
    ) -> BuildManifest:
        """Write the completion marker atomically (temp file + ``os.replace``)."""
        manifest = BuildManifest(
            version=MANIFEST_VERSION,
            key=self.key,
            content_hash=content_hash,
            namespace=namespace,
            built_at=datetime.now(UTC).isoformat(),
            stats=stats,
            extra=dict(extra or {}),
        )
        self.path.mkdir(parents=True, exist_ok=True)
        tmp = self.manifest_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(manifest.as_dict(), indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.manifest_path)
        return manifest


@dataclass(frozen=True, slots=True)
class BuildCache:
    """Per-sample store cache, one tree per dataset.

    Layout: ``root/<dataset>/<key.slug()>/{manifest.json,store/}``.

    ``options_hash`` is the cache-level default; :meth:`slot` takes an override so a
    driver derives it from the options it is *about* to use. That is deliberate — a
    driver that computes the key from its own options cannot construct the mismatch
    where a run with the sleep cadence changed reuses a store that never consolidated.
    """

    root: Path
    dataset: str
    profile: str
    config_hash: str
    options_hash: str = ""
    adapter_version: str = "1"

    def key_for(self, sample_id: str, *, options_hash: str | None = None) -> BuildKey:
        return BuildKey(
            dataset=self.dataset,
            sample_id=sample_id,
            profile=self.profile,
            config_hash=self.config_hash,
            options_hash=self.options_hash if options_hash is None else options_hash,
            adapter_version=self.adapter_version,
        )

    def slot(self, sample_id: str, *, options_hash: str | None = None) -> BuildSlot:
        key = self.key_for(sample_id, options_hash=options_hash)
        return BuildSlot(key=key, path=self.root / self.dataset / key.slug())


# ─────────────────────────────────────────────────────────────────────────────
# Adapter registry
# ─────────────────────────────────────────────────────────────────────────────

#: Dataset name -> ``module:attribute`` naming a zero-argument adapter factory.
#:
#: Resolved lazily so loading LoCoMo never imports the LongMemEval adapter (or its
#: dependencies) and vice versa, and so this module has no import cycle with either.
#: Names match :class:`evals.harness.config.Dataset` values; the harness maps its enum
#: onto these strings rather than the other way round, keeping ``datasets/`` free of
#: any dependency on ``harness/``.
LOADERS: Final[Mapping[str, str]] = {
    "locomo": "evals.legacy_datasets.locomo:adapter",
    "locomo_plus": "evals.legacy_datasets.locomo:adapter",
    "longmemeval_s": "evals.legacy_datasets.longmemeval:adapter",
    "longmemeval_m": "evals.legacy_datasets.longmemeval:adapter",
    "longmemeval_oracle": "evals.legacy_datasets.longmemeval:adapter",
}


def known_datasets() -> tuple[str, ...]:
    return tuple(sorted(LOADERS))


def _resolve(spec: str) -> Callable[[], DatasetAdapter]:
    module_name, _, attribute = spec.partition(":")
    module = importlib.import_module(module_name)
    factory = getattr(module, attribute)
    if not callable(factory):
        raise CacheContractError(f"dataset loader {spec!r} is not callable")
    return factory  # type: ignore[no-any-return]


def load_dataset(
    name: str,
    path: str | Path,
    *,
    sample_ids: Sequence[str] = (),
    limit: int | None = None,
) -> Iterator[BenchmarkSample]:
    """Stream :class:`BenchmarkSample` for any registered dataset.

    The one entry point the harness needs: it never has to know which adapter parses
    which layout, only that both yield the same record type with the same cost fields.
    """
    spec = LOADERS.get(name)
    if spec is None:
        raise CacheContractError(f"unknown dataset {name!r} — known: {list(known_datasets())}")
    adapter = _resolve(spec)()
    wanted = set(sample_ids)
    emitted = 0
    for sample in adapter.load(path):
        if wanted and sample.sample_id not in wanted:
            continue
        yield sample
        emitted += 1
        if limit is not None and emitted >= limit:
            return
