from __future__ import annotations

from typing import Any

from memspine.exceptions import MemspineError
from memspine.models.providers.llm import structured as _structured
from memspine.models.providers.llm.base import LLMService


def asset_stats(eng: Any) -> dict[str, int]:
    """E04 cost counters of this engine (fetches, vision calls, failures, cache hits)."""
    if eng._assets is None:
        return {}
    s = eng._assets.stats
    return {
        "fetches": s.fetches,
        "fetch_failures": s.fetch_failures,
        "vision_calls": s.vision_calls,
        "vision_failures": s.vision_failures,
        "cache_hits": s.cache_hits,
    }


def external_stats(eng: Any) -> dict[str, int]:
    """E03 counters: network calls made, cache hits and misses, refusals, errors."""
    if eng._external is None:
        return {}
    s = eng._external.stats
    return {
        "network_calls": s.network_calls,
        "cache_hits": s.cache_hits,
        "cache_misses": s.cache_misses,
        "refused": s.refused,
        "errors": s.errors,
    }


def model_calls(eng: Any) -> dict[str, int]:
    """LLM calls this engine has made since ``start()``, per role (read and
    write path alike). Empty when no LLM is configured."""
    return eng._llm.call_counts() if eng._llm is not None else {}


def rerank_stats(eng: Any) -> dict[str, Any]:
    """E8 reranker use since construction: ``{"mode", "calls", "failures",
    "unavailable"}``. ``calls`` counts rerank attempts, ``failures`` those that
    raised (retrieval fell back to vector order), ``unavailable`` that the adapter
    could not be built and the stage disabled itself. Read-only."""
    mode = eng._resolved.config.read.rerank if eng._resolved is not None else None
    return {
        "mode": mode,
        "calls": eng._rerank_calls,
        "failures": eng._rerank_failures,
        "unavailable": eng._rerank_unavailable,
        "gated": eng._rerank_gated,
    }


def model_usage(eng: Any) -> dict[str, dict[str, Any]]:
    """Per-role LLM spend since ``start()``: ``{"model", "calls", "prompt", "completion"}``.

    Token counts are the provider's own usage report where it gives one (LiteLLM),
    else a four-characters-per-token estimate. Roles never called are omitted.
    """
    if eng._llm is None:
        return {}
    calls = eng._llm.call_counts()
    tokens = eng._llm.token_counts()
    models = eng._llm.models()
    out: dict[str, dict[str, Any]] = {}
    for role in sorted(set(calls) | set(tokens)):
        used = tokens.get(role, {})
        out[role] = {
            "model": models.get(role, ""),
            "calls": calls.get(role, 0),
            "prompt": used.get("prompt", 0),
            "completion": used.get("completion", 0),
        }
    return out


def usage(eng: Any, *, reset: bool = False) -> dict[str, dict[str, Any]]:
    """#33: LLM calls and tokens per named prompt since ``start()`` or the last reset.

    Keyed by ``prompt_version`` (``<id>@<version>``; ``"<unnamed>"`` for a caller's
    own messages sent through :meth:`llm`). Each entry holds ``prompt_id``,
    ``prompt_version``, ``roles``, ``calls``, ``input_tokens``, ``output_tokens``,
    ``estimated_calls`` and ``estimated``. Tokens are the provider's own usage
    report when it gives one, else a characters/4 estimate, and such calls are
    counted in ``estimated_calls``. ``reset=True`` clears the counters after the
    snapshot is taken. In-process only; empty when no LLM role is bound.
    """
    return eng._llm.prompt_usage(reset=reset) if eng._llm is not None else {}


def structured_stats(eng: Any, *, reset: bool = False) -> dict[str, dict[str, Any]]:
    """I69: structured-output outcomes per prompt version: ``calls``, ``clean``,
    ``repaired`` (strict parse failed, the repair net rescued it), ``validation_failed``,
    ``llm_errors``, ``retried`` / ``retry_ok`` / ``retry_failed`` (``llm.structured``),
    ``constrained_retries``, plus ``repair_rate`` and ``failure_rate``. Process-wide
    (shared by engines in one process), in memory only."""
    snapshot = _structured.structured_stats()
    if reset:
        _structured.reset_structured_stats()
    return snapshot


def llm(eng: Any, role: str) -> LLMService:
    """The provider bound to a role (D-07/D-22): extract / judge / chat.

    Returns a counting wrapper, not the bare provider: every ``chat`` call made
    through it is added to :meth:`model_calls` under ``role``. Other attributes are
    delegated to the bound provider.
    """
    if eng._llm is None:
        raise MemspineError("Engine not started — call start() first")
    return eng._llm.for_role(role)  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt


def describe(eng: Any) -> dict[str, Any]:
    """The effective world (§4 step 7). Only meaningful on a started engine."""
    storage = eng._require_started()
    assert eng._resolved is not None
    config = eng._resolved.config
    return {
        "profile": config.profile,
        "memories": {
            "enabled": sorted(eng._enabled),
            "auto_enabled": list(eng._auto_enabled),
        },
        "event_log": {
            "mode": config.event_log.mode.value,
            "compress": config.event_log.compress,
            "retention_days": config.event_log.retention_days,
            "rebuildable": storage.can_rebuild,
        },
        "storage": {"provider": config.storage.provider},
        "embedding": eng._embedder.embedder_id if eng._embedder else None,
        "vector": type(eng._vector).__name__ if eng._vector else None,
        "cache": config.cache.provider,
        "graph": type(eng._graph).__name__ if eng._graph else None,
        "llm_roles": eng._llm.roles if eng._llm else [],
        "prompts": ({p.id: p.prompt_version for p in eng._prompts.list()} if eng._prompts else {}),
        "semantic_pipeline": eng._semantic is not None,
        "firewall": "deterministic (trust-matrix + instruction-flag + anomaly)",
        "episodic": eng._episodic is not None,
        "resource_ingest": eng._resource is not None,
        "procedural": eng._procedural is not None,
        "reflective": eng._reflective is not None,
        "associative": eng._associative is not None,
        "prospective": eng._prospective is not None,
        "shared": eng._shared is not None,
        "consolidation_summarizer": "llm" if eng._summarize is not None else "extractive",
        "projectors": [projector.name for projector in eng._projectors],
        "runner": config.workers.runner,
        # D1: whether the autonomous sleep loop is active this run.
        "scheduler": eng._scheduler.running if eng._scheduler is not None else False,
        "strict_services": config.strict_services,
        # MD-01: the model plan (tiers, devices, VRAM, fallbacks) when models.tiers_v2 is on.
        **(
            {"models": eng._llm.model_plan}
            if eng._llm is not None and getattr(eng._llm, "model_plan", None)
            else {}
        ),
        **({"write_timers": eng.write_timers()} if eng._write_timers else {}),
        **({"window_write": eng.window_stats()} if eng._windows is not None else {}),
    }


def window_stats(eng: Any, *, reset: bool = False) -> dict[str, Any]:
    """G-W13 write-quality counters of the window write (``{}`` when it is off): windows and
    why they closed, calls per layer, cache hits, parse failures, sections dropped, items
    dropped as ungrounded, deposits, cancellations, windows per session, calls per turn."""
    return eng._windows.stats(reset=reset) if eng._windows is not None else {}
