"""memspine evals — the run driver.

Lives at the repository root, **outside the wheel** (D-19/D-35): nothing here ships,
and nothing in ``src/memspine`` may import it.

What this file is
-----------------
The command line over :mod:`evals.harness.config` and :mod:`evals.harness.results`.
It turns flags into a :class:`~evals.harness.config.RunConfig`, drives the two passes,
and writes a :class:`~evals.harness.results.RunResults` file. Its output is the
**cost-versus-capability frontier**: not one score, but a row per governance layer,
each carrying its own price.

Every run writes its complete resolved configuration into the result file — that is
``RunConfig``'s job, and this driver's job is to make sure nothing that can move a
number reaches the engine without passing through it first. A number without its
protocol is worthless, which is this project's own central finding about the field.

Two passes, never fused
-----------------------
Generation and scoring are separate passes over a results file (MAGMA_HARVEST §2.1).
``--score-only --input-results FILE`` re-judges a stored run: it loads the file, takes
the protocol from the file's own ``RunConfig``, swaps in the new judge, and **never
constructs an Engine** — the generation seam is imported lazily inside the generation
path, so the guarantee is structural rather than a promise.

Ablations are policy-option overrides, not branches
---------------------------------------------------
``AblationConfig`` gives one typed boolean per mechanism (one flag = one toggle).
This module owns the missing half: the **bridge from each toggle to the memspine
config key that actually implements it** (:data:`TOGGLE_SEAMS`). memspine's policies
are bindable through the D-14 override channel (``read.*``,
``memories.<type>.policies.*``), so an ablation is a config override, never an ``if``
at a decision site.

Every binding below was checked against a real config key in ``config/schema.py`` or a
``PolicyOptions`` model. Where memspine has **no** seam for a toggle, the binding says
so and the driver refuses the run naming the seam that is missing — rather than
accepting the flag and quietly changing nothing, which would put an unearned row on
the frontier. Where a binding is an approximation, it carries a caveat that is printed
and recorded in the run's notes, so the published number says what was actually
switched off.

Seams
-----
Two callables, resolved lazily by dotted ``module:attribute`` and overridable from the
CLI so the modules behind them can be named and shaped freely:

``--generation-entrypoint`` (default ``evals.harness.generation:generate``)
    ``generate(config: RunConfig)`` -> ``RunResults`` | ``(items, builds)`` |
    ``[ItemResult, ...]``; sync or async.

``--judge-entrypoint`` (default ``evals.harness.judge:score``)
    ``score(results: RunResults)`` -> ``RunResults`` | ``[ItemResult, ...]``; sync or
    async. The judge takes its instrument from ``results.summary.config.judge``.

Aggregation is not a seam: ``RunResults.refresh_summary()`` recomputes every
breakdown, including the per-category and per-memory-type views.

Usage
-----
::

    python -m evals.run --dataset locomo --data-path data/locomo10.json \\
        --model openai/gpt-4o-mini --judge-model openai/gpt-4o-mini

    python -m evals.run --dataset locomo --data-path data/locomo10.json \\
        --model openai/gpt-4o-mini --judge-model openai/gpt-4o-mini \\
        --ablation no_hybrid_retrieval --out evals/runs/no_hybrid.json

    python -m evals.run --score-only --input-results evals/runs/run.json \\
        --judge-model openai/gpt-4o --out evals/runs/run.rejudged.json

    python -m evals.run --dataset longmemeval --data-path data/lme_s.json \\
        --model openai/gpt-4o-mini --judge-model openai/gpt-4o-mini \\
        --frontier --out evals/runs/frontier.json

    python -m evals.run --dataset locomo --data-path data/locomo10.json \\
        --model openai/gpt-4o-mini --judge-model openai/gpt-4o-mini --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import inspect
import json
import random
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

from evals.harness.config import (
    WRITE_SIDE_ABLATIONS,
    AblationConfig,
    CacheConfig,
    Dataset,
    DatasetConfig,
    GenerationConfig,
    HarnessConfigError,
    HarnessError,
    JudgeConfig,
    ModelConfig,
    RunConfig,
    RunMode,
    SystemConfig,
)
from evals.harness.results import (
    BuildRecord,
    Environment,
    ItemResult,
    RunResults,
    build_results,
    utcnow,
)
from evals.legacy_datasets.base import LOADERS as DATASET_LOADERS

__all__ = [
    "FRONTIER_LADDER",
    "TOGGLE_SEAMS",
    "FrontierRung",
    "OverrideResolution",
    "SeamUnavailableError",
    "ToggleSeam",
    "build_parser",
    "main",
    "memspine_overrides",
    "resolve_ladder",
]

EVALS_DIR: Final = Path(__file__).resolve().parent
REPO_ROOT: Final = EVALS_DIR.parent

DEFAULT_TEMPLATE: Final = "benchmark"
DEFAULT_GENERATION_ENTRYPOINT: Final = "evals.harness.generation:generate"
DEFAULT_JUDGE_ENTRYPOINT: Final = "evals.harness.judge:score"

#: ``--dataset`` accepts every :class:`Dataset` member plus this friendly alias.
DATASET_ALIASES: Final[Mapping[str, Dataset]] = {"longmemeval": Dataset.LONGMEMEVAL_S}


def _check_dataset_coverage() -> None:
    """Every ``Dataset`` the CLI accepts must have a loader behind it.

    ``--dataset locomo_plus`` that parses fine and then dies at load time, after the
    engine is up and the first sample has been ingested, is the expensive way to find
    this out. Fail at import, like the toggle-seam and ablation-partition checks.
    """
    missing = sorted(item.value for item in Dataset if item.value not in DATASET_LOADERS)
    if missing:
        raise HarnessConfigError(
            "these Dataset members have no adapter in evals.legacy_datasets.base.LOADERS: "
            f"{missing} — every dataset the CLI offers must be loadable"
        )


class SeamUnavailableError(HarnessError):
    """A module or capability this driver needs is missing."""


#: What an operator has to build when a default seam is not there yet. These two
#: modules are the harness's remaining stubs: everything either side of them —
#: configuration, ablation bridge, dataset adapters, cost accounting, result schema,
#: frontier ladder — is complete, and nothing here fabricates a number in their place.
_SEAM_CONTRACTS: Final[Mapping[str, str]] = {
    DEFAULT_GENERATION_ENTRYPOINT: (
        "\n\nThis is the ENGINE ADAPTER seam and it is not implemented yet. It must be "
        "an (optionally async) `generate(config: RunConfig)` returning RunResults, "
        "(items, builds), or a list of ItemResult. Its job: load the corpus via "
        "evals.legacy_datasets.base.load_dataset(config.dataset.name.value, config.dataset.path), "
        "build or reuse each sample's memory through evals.legacy_datasets.base.BuildCache "
        "keyed on config.build_digest(), retrieve + assemble per question, call the "
        "backbone, and record per-stage cost with evals.harness.accounting.CostLedger "
        "(bridge it with evals.harness.results.stage_costs_from_report). "
        "Point --generation-entrypoint at your own module:attr in the meantime."
    ),
    DEFAULT_JUDGE_ENTRYPOINT: (
        "\n\nThis is the JUDGE seam and it is not implemented yet. It must be an "
        "(optionally async) `score(results: RunResults)` returning RunResults or a list "
        "of ItemResult, taking its instrument from results.summary.config.judge and "
        "filling ItemResult.judge for every item in results.unjudged(). "
        "Point --judge-entrypoint at your own module:attr in the meantime."
    ),
}


# ── toggle -> memspine config seam ───────────────────────────────────────────


@dataclass(frozen=True)
class ToggleSeam:
    """How one :class:`AblationConfig` field is realised in memspine's config.

    ``on``/``off`` are the dotted-path overrides for that state; an empty mapping
    means "this is memspine's built-in behaviour, nothing to say". ``on_unavailable``
    / ``off_unavailable`` name the seam memspine would need for a state it cannot
    express — selecting such a state is an error, never a silent no-op.
    """

    field_name: str
    on: Mapping[str, Any] = field(default_factory=dict)
    off: Mapping[str, Any] = field(default_factory=dict)
    on_unavailable: str | None = None
    off_unavailable: str | None = None
    on_caveat: str | None = None
    off_caveat: str | None = None
    #: Dotted config key that a *template* layer must not set for the ``off`` state to
    #: be usable (an override layer cannot unset a key a template set).
    off_forbids_template_key: str | None = None
    #: This toggle shares its only config seam with another one, so it may only be
    #: turned off together with it.
    off_requires_also_off: str | None = None
    #: The mirror of the above: turning *this* one off also disables the named toggle,
    #: so leaving that one on would record a protocol that did not run.
    off_also_disables: str | None = None


_SEAM_LIST: Final[tuple[ToggleSeam, ...]] = (
    # ── write path ───────────────────────────────────────────────────────────
    ToggleSeam(
        field_name="firewall",
        off={"memories.semantic.policies.trust.quarantine_below": 0.0},
        off_caveat=(
            "firewall=False is APPROXIMATE, and narrower than it looks. memspine has no "
            "switch that bypasses Firewall.assess (engine.py constructs it "
            "unconditionally), so this only sets trust.quarantine_below=0. "
            "TrustPolicy.should_quarantine has THREE independent limbs and that "
            "neutralises exactly one: content still quarantines on the anomaly check "
            "and on the instruction-shape flag for tool/assistant/external origins. "
            "So a row labelled no_firewall still ran a firewall, and the measured delta "
            "is the price of the TRUST-THRESHOLD limb alone — not of the gate, and not "
            "of the defence. A true bypass needs a `firewall.enabled` field on "
            "MemspineConfig (one decision = one ADR)."
        ),
    ),
    ToggleSeam(
        field_name="dedup",
        off_unavailable=(
            "DedupPolicy exposes thresholds (minhash_num_perm / lsh_threshold / "
            "cosine_threshold) but no enable flag, and defeating it by pushing "
            "thresholds out of range would misreport what was measured. Needs a "
            "`memories.semantic.policies.dedup.enabled` option."
        ),
    ),
    ToggleSeam(
        field_name="conflict_resolution",
        off_unavailable=(
            "ConflictPolicy exposes bias and trust_margin but no enable flag; the M4 "
            "ladder is called unconditionally by SemanticMemory.write. Needs a "
            "`memories.semantic.policies.conflict.enabled` option."
        ),
    ),
    ToggleSeam(
        field_name="entity_extraction",
        on={"memories.semantic.policies.entity_extraction": "gliner"},
        off={"memories.semantic.policies.entity_extraction": "off"},
        on_caveat=(
            "entity_extraction=True binds the local CPU extractor (D-28), which needs "
            "memspine[ner]; the `llm` provider is the expensive path and must be "
            "selected explicitly through --config."
        ),
    ),
    ToggleSeam(
        field_name="corroboration",
        off_unavailable=(
            "Engine._corroborate is gated only by role and by constants "
            "(TRUST_DEFAULT / CORROBORATIONS_TO_PROMOTE), not by config. Needs a "
            "`memories.semantic.policies.trust.corroboration_enabled` option."
        ),
    ),
    ToggleSeam(
        field_name="evolve_links",
        off_requires_also_off="graph_retrieval",
        off_unavailable=(
            "Engine._evolve_links is gated only by whether associative memory is "
            "enabled — the same switch that governs graph retrieval — so write-side "
            "link evolution cannot be priced independently of read-side traversal. "
            "Turn both off together, or add a "
            "`memories.associative.policies.evolve_links` option."
        ),
    ),
    ToggleSeam(
        field_name="consolidation",
        on={"memories.episodic.policies.consolidation.triggers": ["session_end", "sleep_cycle"]},
        off={"memories.episodic.policies.consolidation.triggers": []},
    ),
    ToggleSeam(
        field_name="graph_extraction",
        on={"memories.semantic.policies.extract_graph": {"max_rounds": 1}},
        off={"memories.semantic.policies.extract_graph": False},
        on_caveat=(
            "graph_extraction=True only does work when an `extract_edges` LLM role is "
            "bound; with llm.roles empty the stage self-skips, so the row would show "
            "the layer on and its cost at zero."
        ),
    ),
    ToggleSeam(
        field_name="reorganize",
        on_caveat=(
            "reorganize=True has NO config switch: the Leiden stage self-skips unless "
            "memspine[community] is installed, so this flag records intent only and "
            "the environment decides. Needs a "
            "`memories.associative.policies.community.enabled` option to be priceable."
        ),
    ),
    ToggleSeam(
        field_name="decay",
        off_unavailable=(
            "DecayPolicy exposes tier-transition day thresholds but no enable flag, "
            "and pushing them to infinity would misreport what was measured. Needs a "
            "`memories.<type>.policies.decay.enabled` option."
        ),
    ),
    ToggleSeam(
        field_name="cold_compression",
        on={
            "memories.semantic.policies.compression.compress_tiers": ["dormant"],
            "memories.episodic.policies.compression.compress_tiers": ["dormant"],
        },
        off={
            "memories.semantic.policies.compression.compress_tiers": [],
            "memories.episodic.policies.compression.compress_tiers": [],
        },
    ),
    ToggleSeam(
        field_name="typed_relations",
        on_unavailable=(
            "typed relation views are not built: PLAN_typed_graph_and_lightweight "
            "Part A steps T1-T4 (RelType taxonomy, typed emission, per-family index, "
            "the `typed` strategy). Until they land there is nothing to ablate."
        ),
    ),
    # ── read path ────────────────────────────────────────────────────────────
    ToggleSeam(
        field_name="hybrid_retrieval",
        on={"read.hybrid": True},
        off={"read.hybrid": False},
    ),
    ToggleSeam(
        field_name="rerank",
        on={"read.rerank": "fastembed"},
        off={"read.rerank": "off"},
        on_caveat=(
            "rerank=True selects the fastembed ONNX cross-encoder; the flashrank "
            "alternative (memspine[rerank]) must be selected through --config."
        ),
    ),
    ToggleSeam(
        field_name="static_prefilter",
        on={"read.static_prefilter": True},
        off={"read.static_prefilter": False},
    ),
    ToggleSeam(
        field_name="mmr",
        off={"read.assembly.mmr_lambda": 1.0},
        off_caveat=(
            "mmr=False is APPROXIMATE: AssemblyPolicy has no MMR switch, so this sets "
            "mmr_lambda=1.0, which zeroes the redundancy term and degenerates the "
            "selection to greedy relevance order. The selection loop itself still "
            "runs, so this prices the diversity term, not the loop."
        ),
    ),
    ToggleSeam(
        field_name="cache_aware_assembly",
        on={"read.assembly.cache_aware_placement": True},
        off={"read.assembly.cache_aware_placement": False},
    ),
    ToggleSeam(
        field_name="assembly_compression",
        on={"read.compression.assembly": True},
        off={"read.compression.assembly": False},
        on_caveat="assembly_compression=True needs memspine[compress] (llmlingua).",
    ),
    ToggleSeam(
        field_name="graph_retrieval",
        on={"memories.associative.enabled": True},
        off={"memories.associative.enabled": False},
        off_forbids_template_key="graph.provider",
        off_also_disables="evolve_links",
        off_caveat=(
            "graph_retrieval=False disables associative memory, which also disables "
            "write-side link evolution (they share one switch) — evolve_links must be "
            "False too so the recorded protocol matches what ran."
        ),
    ),
    ToggleSeam(
        field_name="typed_adaptive_routing",
        on_unavailable=(
            "query-conditioned routing is not built: PLAN Part A step T6 "
            "(`typed_adaptive` rule table). memspine has no query conditioning at all."
        ),
    ),
    ToggleSeam(
        field_name="validity_aware_traversal",
        on_unavailable=(
            "validity-aware traversal is not built: PLAN Part A step T7. The "
            "bi-temporal record exists; the traversal that honours valid_to does not."
        ),
    ),
    ToggleSeam(
        field_name="trust_bounded_traversal",
        on_unavailable="trust-bounded traversal is not built: PLAN Part A step T7.",
    ),
)

TOGGLE_SEAMS: Final[Mapping[str, ToggleSeam]] = {seam.field_name: seam for seam in _SEAM_LIST}


def _check_seam_coverage() -> None:
    """Every ablation toggle must have a declared config seam.

    A toggle with no entry here would be accepted on the command line and change
    nothing — an unearned row on the frontier. Fail at import, not at analysis time.
    """
    declared = set(AblationConfig.model_fields)
    bound = set(TOGGLE_SEAMS)
    if declared != bound:
        missing = sorted(declared - bound)
        stale = sorted(bound - declared)
        raise HarnessConfigError(
            "toggle seam table is out of sync with AblationConfig — "
            f"unbound toggles: {missing}; unknown seams: {stale}"
        )


_check_seam_coverage()
_check_dataset_coverage()


@dataclass(frozen=True)
class OverrideResolution:
    """The memspine overrides an ablation implies, plus what they do not cover."""

    overrides: dict[str, Any]
    caveats: tuple[str, ...]


def memspine_overrides(ablation: AblationConfig) -> OverrideResolution:
    """Translate every toggle into dotted memspine config overrides.

    Both states of every *available* toggle are emitted, not only the deviations, so
    the resulting config states each priced layer explicitly and does not depend on a
    template default that may change later.
    """
    overrides: dict[str, Any] = {}
    caveats: list[str] = []
    unsupported: list[str] = []
    for name, seam in TOGGLE_SEAMS.items():
        if getattr(ablation, name):
            if seam.on_unavailable is not None:
                unsupported.append(f"{name}=True: {seam.on_unavailable}")
                continue
            overrides.update(seam.on)
            if seam.on_caveat:
                caveats.append(seam.on_caveat)
            continue
        coupled = seam.off_requires_also_off
        if coupled is not None and getattr(ablation, coupled):
            unsupported.append(f"{name}=False requires {coupled}=False too: {seam.off_unavailable}")
            continue
        # The mirror check. Without it, `--ablation no_graph_retrieval` alone is
        # accepted and records evolve_links=True while Engine._evolve_links returns
        # immediately (it is gated on associative memory being enabled) — a protocol
        # record that is wrong, which is worse than a flag that does nothing.
        disabled = seam.off_also_disables
        if disabled is not None and getattr(ablation, disabled):
            unsupported.append(
                f"{name}=False also disables {disabled}, so {disabled}=False is required "
                "for the recorded protocol to match what ran (they share one switch)"
            )
            continue
        if seam.off_unavailable is not None and coupled is None:
            unsupported.append(f"{name}=False: {seam.off_unavailable}")
            continue
        overrides.update(seam.off)
        if seam.off_caveat:
            caveats.append(seam.off_caveat)
    if unsupported:
        detail = "\n  - ".join(unsupported)
        raise SeamUnavailableError(
            "this ablation asks for something memspine cannot express:\n  - " + detail
        )
    return OverrideResolution(overrides=overrides, caveats=tuple(caveats))


# ── the frontier ladder ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class FrontierRung:
    """One rung, expressed as a delta on the rung below it.

    A rung's diff is a single readable change rather than a re-declared toggle set,
    which is what makes a published frontier auditable.
    """

    name: str
    turn_on: tuple[str, ...] = ()
    note: str = ""


#: The bottom rung: every priceable layer off. dedup, conflict resolution,
#: corroboration and decay stay ON at every rung — memspine has no config seam to
#: disable them (see :data:`TOGGLE_SEAMS`), so they are a constant of the frontier,
#: not a step on it. That is a limitation of the engine, recorded here rather than
#: papered over.
#:
#: ``firewall=False`` here is a **deliberate deviation** from PLAN Part B §2.2/§2.4 and
#: from benchmark.yaml's own comment, both of which say to keep the firewall on and
#: price it. A frontier needs a bottom row with the layer off, or the "+firewall" rung
#: has nothing to be a delta *from*; the plan's instruction is honoured by the rung
#: immediately above, which turns it back on and prices it. Worth an ADR because it
#: reverses a written plan line, and because what the off-row actually disables is
#: narrower than the label suggests (see the firewall seam's caveat).
LEAN_ABLATION: Final[AblationConfig] = AblationConfig(
    firewall=False,
    entity_extraction=False,
    consolidation=False,
    graph_extraction=False,
    reorganize=False,
    cold_compression=False,
    evolve_links=False,
    graph_retrieval=False,
    hybrid_retrieval=True,
    rerank=False,
    static_prefilter=False,
    mmr=True,
    cache_aware_assembly=True,
    assembly_compression=False,
)

FRONTIER_LADDER: Final[tuple[FrontierRung, ...]] = (
    FrontierRung(
        name="lean",
        note=(
            "Deterministic, LLM-free write path. The operating point comparable with "
            "lightweight research systems."
        ),
    ),
    FrontierRung(
        name="firewall",
        turn_on=("firewall",),
        note="Adds the Memory Firewall quarantine verdict (E1).",
    ),
    FrontierRung(
        name="consolidation",
        turn_on=("consolidation",),
        note="Adds episodic->semantic consolidation (deterministic summariser, N6).",
    ),
    FrontierRung(
        name="graph",
        turn_on=("graph_retrieval", "evolve_links"),
        note=(
            "Adds associative memory: write-side link evolution and read-side graph "
            "traversal, which memspine cannot separate."
        ),
    ),
    FrontierRung(
        name="full",
        turn_on=(
            "entity_extraction",
            "graph_extraction",
            "reorganize",
            "cold_compression",
            "rerank",
            "assembly_compression",
        ),
        note=(
            "Everything priceable on. Unlike the rungs below it this is not a "
            "single-toggle step, so its delta is a bundle, not an attribution."
        ),
    ),
)


def resolve_ladder(
    selected: Sequence[str] | None = None,
) -> list[tuple[FrontierRung, AblationConfig]]:
    """Accumulate the ladder into (rung, ablation) pairs, in order.

    ``selected`` subsets which rungs are emitted; accumulation always walks the whole
    ladder, so a subset row still carries everything it inherits.
    """
    wanted = None if selected is None else set(selected)
    if wanted is not None:
        unknown = sorted(wanted - {rung.name for rung in FRONTIER_LADDER})
        if unknown:
            names = ", ".join(rung.name for rung in FRONTIER_LADDER)
            raise HarnessError(f"unknown frontier rung(s): {unknown} — valid: {names}")
    current = LEAN_ABLATION
    out: list[tuple[FrontierRung, AblationConfig]] = []
    for rung in FRONTIER_LADDER:
        unknown_toggles = sorted(set(rung.turn_on) - set(AblationConfig.model_fields))
        if unknown_toggles:
            raise HarnessConfigError(
                f"frontier rung {rung.name!r} names unknown toggle(s): {unknown_toggles}"
            )
        if rung.turn_on:
            current = current.model_copy(update=dict.fromkeys(rung.turn_on, True))
        if wanted is None or rung.name in wanted:
            out.append((rung, current))
    return out


# ── small helpers ────────────────────────────────────────────────────────────


def _nest(dotted: Mapping[str, Any]) -> dict[str, Any]:
    """``{"read.hybrid": False}`` -> ``{"read": {"hybrid": False}}``.

    The config loader merges nested mappings; ``SystemConfig.overrides`` records the
    flat dotted form because that is what a human writes in a YAML run config.
    """
    nested: dict[str, Any] = {}
    for key, value in dotted.items():
        node = nested
        parts = key.split(".")
        for part in parts[:-1]:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise HarnessConfigError(f"override key {key!r} conflicts with a scalar override")
            node = child
        node[parts[-1]] = value
    return nested


def _git_commit(repo: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _resolve_entrypoint(spec: str) -> Callable[..., Any]:
    module_name, _, attribute = spec.partition(":")
    if not module_name or not attribute:
        raise HarnessError(f"entrypoint {spec!r} must be of the form 'module:attribute'")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        hint = _SEAM_CONTRACTS.get(spec, "")
        raise SeamUnavailableError(
            f"cannot import {module_name!r} for entrypoint {spec!r}: {exc}{hint}"
        ) from exc
    target = getattr(module, attribute, None)
    if target is None:
        raise SeamUnavailableError(f"module {module_name!r} has no attribute {attribute!r}")
    if not callable(target):
        raise SeamUnavailableError(f"{spec!r} is not callable")
    return cast("Callable[..., Any]", target)


async def _acall(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _parse_model(spec: str, api_base: str | None, seed: int) -> ModelConfig:
    """``provider/model`` -> :class:`ModelConfig` (the LiteLLM id shape)."""
    provider, sep, model = spec.partition("/")
    if not sep or not provider or not model:
        raise HarnessError(
            f"model {spec!r} must be 'provider/model' (e.g. openai/gpt-4o-mini, "
            "ollama/llama3, bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0)"
        )
    return ModelConfig(provider=provider, model=model, api_base=api_base, seed=seed)


def _short(text: str, limit: int = 48) -> str:
    if len(text) <= limit:
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:6]
    return f"{text[: limit - 7]}-{digest}"


def _make_run_id(dataset: Dataset, template: str | None, ablation: AblationConfig) -> str:
    """Corpus, profile, ablation, timestamp — readable, sortable, path-safe.

    Always generated, even when a ``--config`` file names a ``run_id``: the same run
    config is meant to be run repeatedly (and once per rung in frontier mode), and two
    runs sharing an id would overwrite each other's results file.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{dataset.value}-{template or 'none'}-{_short(ablation.label())}-{stamp}"


def _dataset_from(name: str) -> Dataset:
    alias = DATASET_ALIASES.get(name)
    return alias if alias is not None else Dataset(name)


# ── run-config construction ──────────────────────────────────────────────────


def _ablation_from_keys(base: AblationConfig, keys: Sequence[str]) -> AblationConfig:
    """``no_rerank`` -> ``rerank=False``; ``rerank`` -> ``rerank=True``.

    The same spelling :meth:`AblationConfig.label` produces, so a run's label round-trips
    back into the flags that made it.
    """
    updates: dict[str, bool] = {}
    fields = set(AblationConfig.model_fields)
    for key in keys:
        if key in fields:
            updates[key] = True
        elif key.startswith("no_") and key[3:] in fields:
            updates[key[3:]] = False
        else:
            valid = ", ".join(sorted(fields))
            raise HarnessError(
                f"unknown ablation {key!r} — use a toggle name or 'no_<name>'. Valid: {valid}"
            )
    return base.model_copy(update=updates) if updates else base


def _system_config(
    args: argparse.Namespace, ablation: AblationConfig, base: SystemConfig | None
) -> tuple[SystemConfig, tuple[str, ...]]:
    resolution = memspine_overrides(ablation)
    start = base or SystemConfig()
    extra_overrides: dict[str, Any] = {}
    embedding = start.embedding
    if args.embedding_model:
        embedding = embedding.model_copy(update={"model": args.embedding_model})
        # The flag must actually reach the engine. Recording a different embedder in
        # the protocol from the one that ran is the worst of both worlds: the embedder
        # is the single largest determinant of retrieval quality, and the result file
        # would name the wrong one.
        extra_overrides["embedding.model"] = args.embedding_model
    template = args.profile or start.template
    config_sha = _file_sha256(start.config_path)
    system = start.model_copy(
        update={
            "template": template,
            # NOT `template`: SystemConfig.profile is documented as "whatever the
            # template declares" and the harness must not silently override a
            # template's own profile. --profile selects the *template*.
            "profile": start.profile,
            "config_sha256": config_sha,
            "embedding": embedding,
            # The toggle bridge wins over anything a --config file said: the flags on
            # the command line are what the operator asked for.
            #
            # The whole override map goes here, read-side keys included, because this
            # is the only field the runner can write them to. RunConfig.build_payload
            # narrows it back out (see _READ_ONLY_OVERRIDE_PREFIXES), so a read-side
            # ablation keeps the same build_digest and reuses the cached memory —
            # MAGMA_HARVEST §2.3's economy. The narrowing is an allow-list of
            # read-only subtrees, so an unrecognised key over-invalidates the cache
            # rather than under-invalidating it.
            "overrides": {**start.overrides, **resolution.overrides, **extra_overrides},
        }
    )
    return system, resolution.caveats


def _file_sha256(path: Path | None) -> str | None:
    """Content digest of a file the protocol points at, or ``None`` when absent.

    A path is not a protocol: the file behind it can be edited between two runs that
    then share a digest. This is what closes that gap for ``system.config_path`` and
    for the corpus.
    """
    if path is None:
        return None
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _dataset_config(args: argparse.Namespace, base: DatasetConfig | None) -> DatasetConfig:
    if base is None:
        if not args.dataset:
            raise HarnessError("--dataset is required (or supply it through --config)")
        base = DatasetConfig(name=_dataset_from(args.dataset))
    updates: dict[str, Any] = {}
    if args.dataset:
        updates["name"] = _dataset_from(args.dataset)
    if args.data_path:
        path = Path(args.data_path)
        if not path.exists():
            raise HarnessError(f"--data-path {path} does not exist")
        updates["path"] = path
        # DatasetConfig.corpus_sha256 is documented as "stamped by the loader" and is
        # the only defence against silently comparing two runs over two different
        # copies of "LoCoMo". Stamped here because this is where the path is resolved.
        digest = _file_sha256(path) if path.is_file() else None
        if digest is not None:
            updates["corpus_sha256"] = digest
    if args.sample:
        updates["sample_ids"] = tuple(args.sample)
    if args.max_questions is not None:
        updates["max_questions_per_sample"] = args.max_questions
    if args.limit is not None:
        updates["limit"] = args.limit
    return base.model_copy(update=updates) if updates else base


def _run_config(
    args: argparse.Namespace,
    *,
    mode: RunMode,
    ablation: AblationConfig | None = None,
    run_id_suffix: str = "",
) -> tuple[RunConfig, tuple[str, ...]]:
    """Flags (+ optional ``--config`` file) -> the one object that defines the run.

    A flag only overrides the file when it was actually passed: every override-capable
    option defaults to ``None`` so an argparse default can never silently replace a
    value the operator wrote in their run config.
    """
    base: RunConfig | None = RunConfig.from_file(args.config) if args.config else None
    seed = args.seed if args.seed is not None else (base.seed if base else 0)

    resolved_ablation = ablation or _ablation_from_keys(
        base.ablation if base else AblationConfig(), args.ablation
    )
    system, caveats = _system_config(args, resolved_ablation, base.system if base else None)
    dataset = _dataset_config(args, base.dataset if base else None)

    generation = base.generation if base else None
    if args.model:
        backbone = _parse_model(args.model, args.model_api_base, seed)
        generation = (
            generation.model_copy(update={"backbone": backbone})
            if generation
            else GenerationConfig(backbone=backbone)
        )
    if generation is None:
        raise HarnessError("--model is required for a generate run (or supply it via --config)")

    judge = base.judge if base else None
    if args.judge_model:
        judge_model = _parse_model(args.judge_model, args.judge_api_base, seed)
        judge = (
            judge.model_copy(update={"model": judge_model})
            if judge
            else JudgeConfig(model=judge_model)
        )
    if judge is None:
        raise HarnessError("--judge-model is required (or supply it via --config)")

    cache = base.cache if base else CacheConfig()
    cache_updates: dict[str, Any] = {}
    if args.cache_dir:
        cache_updates["dir"] = Path(args.cache_dir)
    if args.rebuild:
        cache_updates["rebuild"] = True
    if cache_updates:
        cache = cache.model_copy(update=cache_updates)

    out = Path(args.out) if args.out else None
    output_dir = out.parent if out else (base.output_dir if base else Path("evals/runs"))
    notes = (base.notes if base else "").strip()
    if caveats:
        joined = " | ".join(caveats)
        notes = f"{notes}\nAblation caveats: {joined}".strip() if notes else f"Caveats: {joined}"

    payload: dict[str, Any] = dict(base) if base is not None else {}
    payload.update(
        run_id=_make_run_id(dataset.name, system.template, resolved_ablation) + run_id_suffix,
        dataset=dataset,
        system=system,
        ablation=resolved_ablation,
        generation=generation,
        judge=judge,
        cache=cache,
        seed=seed,
        mode=mode,
        output_dir=output_dir,
        notes=notes,
        require_single_toggle=bool(args.single_toggle),
    )
    try:
        config = RunConfig.model_validate(payload)
    except HarnessError:
        raise
    except Exception as exc:
        raise HarnessConfigError(f"invalid run configuration: {exc}") from exc
    return config, caveats


# ── pre-flight: the overrides must actually bind ─────────────────────────────


def _preflight(config: RunConfig) -> dict[str, Any]:
    """Resolve the memspine config **without constructing an Engine**.

    Catches a mistyped *top-level* override (``MemspineConfig`` and its sub-models are
    ``extra="forbid"``) and the one combination an override layer cannot express, before
    a single token is spent. Returns the effective config for ``--dry-run``.

    What it does **not** catch: anything inside a policy block. ``memories.*.policies``
    and ``read.compression`` are ``dict[str, Any]`` in the schema, so a bogus policy
    option or an out-of-range value passes here and fails later, loudly, at
    ``PolicyOptions.bind`` when the Engine starts. Loud is not the same as early — every
    seam this driver emits is checked against a real key, so this only bites overrides
    supplied through ``--config``.

    This resolution also runs with no secret resolver and no environment layer, so it is
    a *validation* of the run's overrides, not a substitute for the resolved config the
    runner dumps from the started Engine into
    ``RunSummary.resolved_system_config``.
    """
    from memspine.config.loader import load_config

    try:
        resolved = load_config(
            template=config.system.template, overrides=_nest(config.system.overrides)
        )
    except Exception as exc:
        raise HarnessConfigError(
            f"memspine rejected this run's configuration "
            f"(template={config.system.template!r}): {exc}"
        ) from exc

    for name, seam in TOGGLE_SEAMS.items():
        key = seam.off_forbids_template_key
        if key is None or getattr(config.ablation, name):
            continue
        layer = resolved.sources.get(key, "default")
        if layer.startswith("template:"):
            block = key.split(".", 1)[0]
            template_name = layer.split(":", 1)[1]
            raise HarnessConfigError(
                f"{name}=False cannot apply: {layer} sets {key!r}, and an override layer "
                f"cannot unset a key a template set. memspine rejects a `{block}:` block "
                f"when the memory that projects it is disabled. Fix: drop the `{block}:` "
                f"block from the {template_name} template — its value is already the "
                "schema default, so the block is absent unless a run asks for it."
            )
    return {
        "template": config.system.template,
        "effective": resolved.config.model_dump(mode="json"),
        "sources": dict(resolved.sources),
    }


# ── passes ───────────────────────────────────────────────────────────────────


def _coerce_generation(raw: object) -> tuple[list[ItemResult], list[BuildRecord]]:
    """Accept ``RunResults``, ``(items, builds)`` or a bare item list."""
    if isinstance(raw, RunResults):
        return list(raw.items), list(raw.builds)
    if isinstance(raw, tuple) and len(raw) == 2:
        items_obj, builds_obj = raw[0], raw[1]
    else:
        items_obj, builds_obj = raw, []
    if not isinstance(items_obj, Sequence) or isinstance(items_obj, str | bytes):
        raise HarnessError(
            "generation entrypoint must return RunResults, (items, builds), or a list "
            f"of ItemResult — got {type(raw).__name__}"
        )
    items = [ItemResult.model_validate(item) for item in items_obj]
    build_seq: Sequence[Any] = (
        builds_obj if isinstance(builds_obj, Sequence) and not isinstance(builds_obj, str) else []
    )
    return items, [BuildRecord.model_validate(build) for build in build_seq]


async def _generate(
    config: RunConfig, entrypoint: str, effective: Mapping[str, Any] | None = None
) -> RunResults:
    """The only path that may construct an Engine — the seam is imported here, not at
    module scope, so ``--score-only`` cannot reach it.

    The pre-flighted memspine config is stamped onto the summary. ``config.system``
    records what was *asked for* (a template name plus an override map, i.e. a pointer
    to a mutable file); this records what was actually resolved, which is the only way a
    result read a year from now can be trusted to say what it ran under.
    """
    started = utcnow()
    fn = _resolve_entrypoint(entrypoint)
    items, builds = _coerce_generation(await _acall(fn, config))
    results = build_results(
        config=config,
        items=items,
        builds=builds,
        started_at=started,
        finished_at=utcnow(),
        environment=Environment.capture(_git_commit(REPO_ROOT)),
    )
    resolved = None if effective is None else effective.get("effective")
    if isinstance(resolved, dict):
        results.summary.resolved_system_config = resolved
        canonical = json.dumps(resolved, sort_keys=True, separators=(",", ":"), default=str)
        results.summary.resolved_system_config_sha256 = hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest()
    return results


async def _judge(results: RunResults, entrypoint: str) -> RunResults:
    fn = _resolve_entrypoint(entrypoint)
    raw = await _acall(fn, results)
    if isinstance(raw, RunResults):
        judged = raw
    elif isinstance(raw, Sequence) and not isinstance(raw, str | bytes):
        results.items = [ItemResult.model_validate(item) for item in raw]
        judged = results
    else:
        raise HarnessError(
            f"judge entrypoint {entrypoint!r} must return RunResults or a list of "
            f"ItemResult — got {type(raw).__name__}"
        )
    judged.refresh_summary()  # stored aggregates never drift from the items
    return judged


def _report(results: RunResults, path: Path) -> None:
    summary = results.summary
    overall = summary.overall
    print(
        f"{summary.run_id} [{summary.ablation_label}]: "
        f"n={overall.n} judged={overall.n_judged} errors={overall.n_errors} "
        f"judge_score_mean={overall.judge_score_mean} pass_rate={overall.pass_rate} "
        f"tokens_per_question={summary.cost_per_cycle.tokens_per_question} -> {path}"
    )


# ── modes ────────────────────────────────────────────────────────────────────


def _output_path(args: argparse.Namespace, config: RunConfig) -> Path:
    return Path(args.out) if args.out else Path(config.output_dir) / f"{config.run_id}.json"


async def _mode_score_only(args: argparse.Namespace) -> int:
    """Re-judge a stored run. Takes the protocol from the file and swaps the judge, so
    a judge change is visible as a protocol change rather than hidden."""
    source = Path(args.input_results)
    results = RunResults.load(source)
    stored = results.summary.config

    updates: dict[str, Any] = {"mode": RunMode.SCORE, "input_results": source}
    if args.judge_model:
        updates["judge"] = stored.judge.model_copy(
            update={"model": _parse_model(args.judge_model, args.judge_api_base, stored.seed)}
        )
    results.summary.config = stored.model_copy(update=updates)
    results.summary.protocol_digest = results.summary.config.protocol_digest()

    cleared = _clear_stale_verdicts(results, results.summary.config.judge)
    if cleared:
        print(
            f"re-judging {cleared} item(s) scored by a different instrument",
            file=sys.stderr,
        )

    judged = await _judge(results, args.judge_entrypoint)
    out = Path(args.out) if args.out else source.with_name(f"{source.stem}.rejudged.json")
    judged.save(out)
    _report(judged, out)
    return 0


def _clear_stale_verdicts(results: RunResults, judge: JudgeConfig) -> int:
    """Drop every verdict that a *different* instrument produced, and say how many.

    ``RunResults.unjudged()`` is the scorer's work list and it only contains items with
    no verdict at all. Without this, ``--score-only --judge-model <other>`` over an
    already-scored file is a silent no-op: it would rewrite the file with the new judge
    named in its config and the *old* judge's scores in its items — a result that
    misattributes its own numbers, which is worse than one that fails.

    A verdict from the same model, kind, rubric and rubric version is kept, so
    re-running an interrupted scoring pass resumes rather than paying twice.
    """
    instrument = (judge.model.label(), judge.kind.value, judge.rubric_id, judge.rubric_version)
    cleared = 0
    for item in results.items:
        verdict = item.judge
        if verdict is None:
            continue
        if (
            verdict.judge_model,
            verdict.kind,
            verdict.rubric_id,
            verdict.rubric_version,
        ) != instrument:
            item.judge = None
            cleared += 1
    return cleared


def _dump_dry_run(config: RunConfig, caveats: Sequence[str], effective: Mapping[str, Any]) -> None:
    print(
        json.dumps(
            {
                "run_config": config.model_dump(mode="json"),
                "protocol_digest": config.protocol_digest(),
                "build_digest": config.build_digest(),
                "ablation_label": config.ablation.label(),
                "ablation_deviations": config.ablation.deviations(),
                "memspine_overrides": dict(config.system.overrides),
                "memspine_config": effective,
                "caveats": list(caveats),
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )


async def _mode_single(args: argparse.Namespace) -> int:
    mode = RunMode.GENERATE if args.generate_only else RunMode.GENERATE_AND_SCORE
    config, caveats = _run_config(args, mode=mode)
    effective = _preflight(config)
    for caveat in caveats:
        print(f"caveat: {caveat}", file=sys.stderr)
    if args.dry_run:
        _dump_dry_run(config, caveats, effective)
        return 0

    out = _output_path(args, config)
    results = await _generate(config, args.generation_entrypoint, effective)
    results.save(out)  # generation is durable before the judge spends a token
    if not args.generate_only:
        results = await _judge(results, args.judge_entrypoint)
        results.save(out)
    _report(results, out)
    return 0


async def _mode_frontier(args: argparse.Namespace) -> int:
    """One command, one row per rung: the cost-versus-capability frontier."""
    mode = RunMode.GENERATE if args.generate_only else RunMode.GENERATE_AND_SCORE
    prepared: list[tuple[FrontierRung, RunConfig, tuple[str, ...], dict[str, Any]]] = []
    # Every rung is resolved and validated before any of them runs: a ladder that
    # fails on its last rung after paying for the first four is a wasted run.
    for rung, ablation in resolve_ladder(args.rungs or None):
        config, caveats = _run_config(
            args, mode=mode, ablation=ablation, run_id_suffix=f"-{rung.name}"
        )
        prepared.append((rung, config, caveats, _preflight(config)))

    out = Path(args.out) if args.out else Path("evals/runs/frontier.json")
    frontier: dict[str, Any] = {
        "kind": "frontier",
        "created_utc": utcnow().isoformat(),
        "ladder": [
            {
                "rung": rung.name,
                "note": rung.note,
                "turn_on": list(rung.turn_on),
                "ablation": config.ablation.model_dump(mode="json"),
                "ablation_label": config.ablation.label(),
                "caveats": list(caveats),
            }
            for rung, config, caveats, _ in prepared
        ],
        "rows": [],
    }
    if args.dry_run:
        frontier["rows"] = [
            {
                "rung": rung.name,
                "run_config": config.model_dump(mode="json"),
                "protocol_digest": config.protocol_digest(),
                "build_digest": config.build_digest(),
                "memspine_config": effective,
            }
            for rung, config, _, effective in prepared
        ]
        print(json.dumps(frontier, indent=2, ensure_ascii=False, default=str))
        return 0

    rows: list[dict[str, Any]] = []
    for rung, config, caveats, effective in prepared:
        for caveat in caveats:
            print(f"[{rung.name}] caveat: {caveat}", file=sys.stderr)
        rung_out = out.with_name(f"{out.stem}.{rung.name}.json")
        results = await _generate(config, args.generation_entrypoint, effective)
        results.save(rung_out)
        if not args.generate_only:
            results = await _judge(results, args.judge_entrypoint)
            results.save(rung_out)
        rows.append(_frontier_row(rung, results, rung_out, caveats, effective))
        frontier["rows"] = rows
        _write_frontier(out, frontier)  # checkpoint after every rung
        _report(results, rung_out)
    _write_frontier(out, frontier)
    print(f"frontier ({len(rows)} rungs) -> {out}")
    return 0


def _frontier_row(
    rung: FrontierRung,
    results: RunResults,
    results_file: Path,
    caveats: Sequence[str],
    effective: Mapping[str, Any],
) -> dict[str, Any]:
    summary = results.summary
    return {
        "rung": rung.name,
        "note": rung.note,
        "run_id": summary.run_id,
        "results_file": str(results_file),
        "ablation_label": summary.ablation_label,
        "ablation_deviations": summary.ablation_deviations,
        "protocol_digest": summary.protocol_digest,
        "build_digest": summary.build_digest,
        "overall": summary.overall.model_dump(mode="json"),
        "cost_per_cycle": summary.cost_per_cycle.model_dump(mode="json"),
        "per_category": {
            name: breakdown.model_dump(mode="json")
            for name, breakdown in summary.per_category.items()
        },
        # The effective config travels with the row: this file is what a published
        # table is built from, and a row without its protocol is not a result.
        "memspine_config": effective.get("effective"),
        "caveats": list(caveats),
    }


def _write_frontier(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8"
    )


# ── CLI ──────────────────────────────────────────────────────────────────────


def _split_csv(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        out.extend(part.strip() for part in value.split(",") if part.strip())
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals.run",
        description=(
            "memspine benchmark driver — runs LoCoMo / LongMemEval and reports the "
            "cost-versus-capability frontier. Generation and scoring are separate "
            "passes over a results file."
        ),
    )

    data = parser.add_argument_group("dataset")
    data.add_argument(
        "--dataset",
        choices=[*(item.value for item in Dataset), *DATASET_ALIASES],
        help="benchmark corpus",
    )
    data.add_argument("--data-path", help="dataset file or directory")
    data.add_argument(
        "--sample",
        action="append",
        default=[],
        metavar="ID[,ID...]",
        help="restrict to these sample ids (repeatable); default: all",
    )
    data.add_argument("--max-questions", type=int, default=None, help="cap questions per sample")
    data.add_argument("--limit", type=int, default=None, help="cap number of samples")

    config = parser.add_argument_group("configuration")
    config.add_argument(
        "--config", default=None, help="run-config YAML/JSON; CLI flags override it"
    )
    config.add_argument(
        "--profile",
        default=None,
        help=(
            "memspine config template; unset takes it from --config, or "
            f"SystemConfig's default ({DEFAULT_TEMPLATE!r}, the lean one)"
        ),
    )
    config.add_argument(
        "--ablation",
        action="append",
        default=[],
        metavar="KEY[,KEY...]",
        help="toggles, one flag = one toggle: '<name>' enables, 'no_<name>' disables",
    )
    config.add_argument(
        "--single-toggle",
        action="store_true",
        help="refuse to run unless the ablation deviates from the reference in exactly one field",
    )
    config.add_argument(
        "--list-ablations", action="store_true", help="print the toggle seam table and exit"
    )
    config.add_argument(
        "--seed",
        type=int,
        default=None,
        help="run seed; unset takes it from --config, else 0",
    )

    models = parser.add_argument_group("models")
    models.add_argument(
        "--model", default=None, metavar="PROVIDER/MODEL", help="answer backbone (harness-side)"
    )
    models.add_argument("--model-api-base", default=None, help="backbone endpoint override")
    models.add_argument("--judge-model", default=None, metavar="PROVIDER/MODEL")
    models.add_argument("--judge-api-base", default=None, help="judge endpoint override")
    models.add_argument(
        "--embedding-model", default=None, help="override the engine's embedding model"
    )

    cache = parser.add_argument_group("cache")
    cache.add_argument("--cache-dir", default=None, help="per-sample memory build cache")
    cache.add_argument("--rebuild", action="store_true", help="ignore cached builds, reconstruct")

    passes = parser.add_argument_group("passes")
    passes.add_argument(
        "--score-only", action="store_true", help="judge a stored run; never constructs an Engine"
    )
    passes.add_argument("--input-results", default=None, help="results file to re-judge")
    passes.add_argument(
        "--generate-only", action="store_true", help="generate answers, skip the judge pass"
    )

    frontier = parser.add_argument_group("frontier")
    frontier.add_argument(
        "--frontier",
        action="store_true",
        help="run the ladder (lean -> +firewall -> +consolidation -> +graph -> full)",
    )
    frontier.add_argument(
        "--rungs",
        action="append",
        default=[],
        metavar="NAME[,NAME...]",
        help="emit only these rungs (accumulation still walks the whole ladder)",
    )

    seams = parser.add_argument_group("seams")
    seams.add_argument("--generation-entrypoint", default=DEFAULT_GENERATION_ENTRYPOINT)
    seams.add_argument("--judge-entrypoint", default=DEFAULT_JUDGE_ENTRYPOINT)

    parser.add_argument("--out", default=None, help="output file")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve and print the full protocol without running anything",
    )
    return parser


def _print_ablations() -> None:
    reference = AblationConfig()
    print("ablation toggles — '<name>' enables, 'no_<name>' disables:\n")
    for name in AblationConfig.model_fields:
        seam = TOGGLE_SEAMS[name]
        side = "write" if name in WRITE_SIDE_ABLATIONS else "read"
        print(f"  {name}  [{side}-side, reference={bool(getattr(reference, name))}]")
        for state, overrides, unavailable, caveat in (
            ("on ", seam.on, seam.on_unavailable, seam.on_caveat),
            ("off", seam.off, seam.off_unavailable, seam.off_caveat),
        ):
            if unavailable:
                print(f"      {state}: UNAVAILABLE — {unavailable}")
            elif overrides:
                print(f"      {state}: {json.dumps(overrides)}")
            else:
                print(f"      {state}: memspine's built-in behaviour, no override")
            if caveat:
                print(f"      {state}  caveat: {caveat}")
    print("\nfrontier ladder:")
    for rung, ablation in resolve_ladder():
        print(f"  {rung.name}: {ablation.label()}")
        if rung.note:
            print(f"      {rung.note}")


def _validate(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.score_only and args.frontier:
        parser.error("--score-only and --frontier are mutually exclusive")
    if args.score_only and not args.input_results:
        parser.error("--score-only requires --input-results")
    if args.input_results and not args.score_only:
        parser.error("--input-results is only meaningful with --score-only")
    if args.score_only and args.generate_only:
        parser.error("--score-only and --generate-only are mutually exclusive")
    if args.score_only:
        # A re-judge takes its whole protocol from the stored file and swaps the judge.
        # Accepting these silently would let an operator believe an ablation or a
        # dataset selection had been applied to a pass that never looks at them.
        ignored = [
            name
            for name, value in (
                ("--ablation", args.ablation),
                ("--dataset", args.dataset),
                ("--data-path", args.data_path),
                ("--profile", args.profile),
                ("--model", args.model),
                ("--dry-run", args.dry_run),
                ("--single-toggle", args.single_toggle),
                ("--rebuild", args.rebuild),
            )
            if value
        ]
        if ignored:
            parser.error(
                f"--score-only ignores {', '.join(ignored)}: a re-judge takes its "
                "protocol from --input-results and only the judge may change "
                "(--judge-model / --judge-api-base / --judge-entrypoint / --out)"
            )
        return
    if not args.dataset and not args.config:
        parser.error("--dataset is required (or use --config, or --score-only)")
    if args.rungs and not args.frontier:
        parser.error("--rungs is only meaningful with --frontier")
    if args.frontier and args.ablation:
        parser.error("--ablation cannot be combined with --frontier (the ladder owns the toggles)")
    if args.frontier and args.single_toggle:
        parser.error("--single-toggle cannot be combined with --frontier (rungs are cumulative)")


async def _amain(args: argparse.Namespace) -> int:
    if args.score_only:
        return await _mode_score_only(args)
    if args.frontier:
        return await _mode_frontier(args)
    return await _mode_single(args)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_ablations:
        _print_ablations()
        return 0

    args.ablation = _split_csv(args.ablation)
    args.sample = _split_csv(args.sample)
    args.rungs = _split_csv(args.rungs)
    _validate(args, parser)

    # Seeded so anything the driver draws before the seams take over is reproducible;
    # the run's effective seed (which may come from --config) travels in RunConfig for
    # the seams themselves, and RunConfig.sample_seed derives the per-sample seeds.
    random.seed(args.seed if args.seed is not None else 0)

    try:
        return asyncio.run(_amain(args))
    except HarnessError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
