"""MD-01: resolve engine roles through tier, placement and fallback (``models.tiers_v2``).

Wiring for the LIB-42 library (:mod:`memspine.models.tiers`). It turns the ``models`` config into a
:class:`~memspine.models.tiers.ModelPlan` (pure: declared sizes in, plan out, no GPU needed), maps
the engine's role names onto the catalogue (:mod:`memspine.prompts.roles`), and says per role which
model serves it, on which device, with which thinking setting, or why it has no model and what the
fallback is. Only CUDA devices are planned (MD-03: DirectML on the AMD iGPU is 5-8x slower and
resets the driver). Taps: ``models.resolve`` per role, ``models.placement`` once.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field, replace
from typing import Any

from memspine.exceptions import ConfigError
from memspine.models.tiers import (
    RERANKER_ORDER,
    ROLE_CATALOGUE,
    GpuInfo,
    Hardware,
    Inventory,
    ModelInfo,
    ModelPlan,
    PlanPrefs,
    RoleBinding,
    Tier,
    plan_models,
)
from memspine.observability import tap
from memspine.prompts.roles import CATALOGUE_ROLE

__all__ = [
    "DEFAULT_RERANKERS",
    "RERANK_MODE",
    "TIER_BY_NAME",
    "Resolution",
    "ResolvedModels",
    "detect_cuda_gpus",
    "emit_taps",
    "report",
    "rerank_chain",
    "resolve_models",
    "resolve_role",
    "role_binding",
    "role_config",
]

#: Config tier names -> library tiers (``worker`` is today's 9B class).
TIER_BY_NAME: dict[str, Tier] = {
    "none": Tier.NONE,
    "small-local": Tier.SMALL,
    "worker": Tier.MEDIUM,
    "frontier": Tier.LARGE,
}
_NAME_BY_TIER = {v: k for k, v in TIER_BY_NAME.items()}

#: Reranker family -> ``read.rerank`` factory mode.
RERANK_MODE: dict[str, str] = {
    "jina-v3.5": "jina",
    "qwen3-reranker-0.6b": "qwen3",
    "minilm": "fastembed",
}

#: Estimated sizes of the owner-chosen reranker chain (Jina v3.5, Qwen3-Reranker-0.6B, MiniLM CPU).
#: The two torch models are GPU-only here, so a CPU-only machine falls straight to MiniLM.
#: Estimates only: replace with measured values by declaring the model in ``models.catalogue``.
DEFAULT_RERANKERS: tuple[ModelInfo, ...] = (
    ModelInfo(
        "jina-reranker-v3.5",
        "reranker",
        family="jina-v3.5",
        vram_mb=1300,
        ram_mb=1600,
        cpu_ok=False,
    ),
    ModelInfo(
        "qwen3-reranker-0.6b",
        "reranker",
        family="qwen3-reranker-0.6b",
        vram_mb=1400,
        ram_mb=1800,
        cpu_ok=False,
    ),
    ModelInfo("minilm-reranker", "reranker", family="minilm", ram_mb=200, gpu_ok=False),
)

_NOT_CUDA = re.compile(r"amd|radeon|intel|directml|basic render|microsoft", re.IGNORECASE)


def detect_cuda_gpus() -> tuple[GpuInfo, ...]:
    """CUDA devices from ``nvidia-smi``; empty when it is absent or fails. Never raises."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ()
    gpus: list[GpuInfo] = []
    for line in out.splitlines():
        name, _, mem = line.rpartition(",")
        if name and mem.strip().isdigit():
            gpus.append(GpuInfo(name.strip(), int(mem.strip())))
    return tuple(gpus)


def _provider_of(model_id: str) -> str:
    return model_id.partition("/")[0] if "/" in model_id else "local"


def _info(decl: Any) -> ModelInfo:
    return ModelInfo(
        id=decl.id,
        kind=decl.kind,
        provider=decl.provider or _provider_of(decl.id),
        tier=TIER_BY_NAME[decl.tier] if decl.kind == "llm" else Tier.NONE,
        family=decl.family,
        hosted=decl.hosted,
        vram_mb=decl.vram_mb,
        ram_mb=decl.ram_mb,
        context_vram_mb=decl.context_vram_mb,
        cpu_ok=decl.placement != "gpu",
        gpu_ok=decl.placement != "cpu",
        supports_thinking=decl.supports_thinking,
        profile=decl.profile,
    )


@dataclass(frozen=True)
class Resolution:
    """What ``models.resolve`` reports for one engine role."""

    role: str  # engine role (``window_extract``)
    catalogue_role: str  # tier-catalogue role (``extract``)
    status: str  # bound | fallback | disabled | error | explicit | unmapped
    tier: str = ""  # the model's tier name, "" when no model
    model_id: str = ""
    device: str = ""
    thinking: str = "off"  # off | capped
    thinking_cap: int = 0
    fallback: str = ""  # skip | rules | calibrated, set when no model serves the role
    reason: str = ""  # the fallback reason / note (prose)
    reason_code: str = ""  # identifier form of the reason, for taps


@dataclass(frozen=True)
class ResolvedModels:
    """The plan plus what the engine needs to build providers from it."""

    plan: ModelPlan
    inventory: Inventory
    decls: dict[str, Any]
    hardware: Hardware
    prefs: PlanPrefs
    thinking_caps: dict[str, int] = field(default_factory=dict)
    #: Reasons collected before planning (ignored non-CUDA devices, unknown role names, tier gaps).
    notes: tuple[str, ...] = ()
    #: Hard errors (unknown catalogue role names); the plan's own errors are in ``plan.errors``.
    errors: tuple[str, ...] = ()

    def check(self) -> None:
        """Fail fast (P-03 default): raise ``ConfigError`` listing every error."""
        problems = [*self.errors, *self.plan.errors]
        if problems:
            raise ConfigError("models.tiers_v2: " + "; ".join(dict.fromkeys(problems)))


def resolve_models(
    cfg: Any, *, extra_required: tuple[str, ...] = (), gpus: tuple[GpuInfo, ...] | None = None
) -> ResolvedModels:
    """Build the plan from a ``ModelsConfig``. Pure for a given config (``gpus`` overrides probing).

    ``extra_required`` are catalogue roles a configured feature needs (they join
    ``models.required_roles`` when ``on_missing_model`` is ``error``).
    """
    notes: list[str] = []
    errors: list[str] = []
    known = {r.name for r in ROLE_CATALOGUE}

    decls: dict[str, Any] = {d.id: d for d in cfg.catalogue}
    models = [_info(d) for d in cfg.catalogue]
    if cfg.default_rerankers == "shipped" and not any(m.kind == "reranker" for m in models):
        models.extend(DEFAULT_RERANKERS)
    inv = Inventory(tuple(models))

    declared = [GpuInfo(g.name, g.vram_mb) for g in cfg.gpus]
    if gpus is not None:
        declared = list(gpus)
    elif cfg.detect_gpus and not declared:
        declared = list(detect_cuda_gpus())
    cuda: list[GpuInfo] = []
    for g in declared:
        if _NOT_CUDA.search(g.name):
            notes.append(f"device {g.name} ignored: only CUDA is planned (MD-03)")
        else:
            cuda.append(g)
    hw = Hardware(gpus=tuple(cuda), ram_mb=cfg.ram_mb)

    pins: dict[str, str] = {}
    disabled: set[str] = set()
    for role, model_id in cfg.pins.items():
        if role not in known:
            errors.append(f"models.pins: unknown role {role!r} (valid: {', '.join(sorted(known))})")
        else:
            pins[role] = model_id
    llms = [m for m in models if m.kind == "llm"]
    for role, tier_name in sorted(cfg.role_tier.items()):
        if role not in known:
            errors.append(
                f"models.role_tier: unknown role {role!r} (valid: {', '.join(sorted(known))})"
            )
            continue
        want = TIER_BY_NAME[tier_name]
        if want == Tier.NONE:
            disabled.add(role)
            continue
        if role in pins:
            continue
        fits = sorted(
            (m for m in llms if m.tier >= want and (cfg.allow_hosted or not m.hosted)),
            key=lambda m: (m.tier, m.hosted, m.gpu_mb, m.id),
        )
        if fits:
            pins[role] = fits[0].id
        else:
            disabled.add(role)
            notes.append(f"role {role}: no declared model at tier {tier_name} or above; role off")

    required: set[str] = set()
    if cfg.on_missing_model == "error":
        for role in (*cfg.required_roles, *extra_required):
            if role in known:
                required.add(role)
            else:
                errors.append(f"models.required_roles: unknown role {role!r}")

    caps: dict[str, int] = {}
    for role, cap in cfg.thinking.items():
        if role not in known:
            errors.append(f"models.thinking: unknown role {role!r}")
        elif cap > 0:
            caps[role] = cap

    prefs = PlanPrefs(
        pins=pins,
        disabled_roles=frozenset(disabled),
        required_roles=frozenset(required),
        allow_hosted=cfg.allow_hosted,
        thinking_optin=frozenset(caps),
        embedder_profile=cfg.embedder_profile,
        rerank=cfg.rerank == "chain",
        gpu_headroom_mb=cfg.gpu_headroom_mb,
    )
    plan = plan_models(inv, hw, prefs)
    # An explicit token budget replaces the library default cap, only where thinking is on.
    if caps:
        roles = tuple(
            replace(b, thinking=replace(b.thinking, cap_tokens=caps[b.role]))
            if b.status == "bound" and b.thinking.mode == "capped" and b.role in caps
            else b
            for b in plan.roles
        )
        plan = replace(plan, roles=roles)
    return ResolvedModels(plan, inv, decls, hw, prefs, caps, tuple(notes), tuple(errors))


def resolve_role(resolved: ResolvedModels, role: str) -> Resolution:
    """The resolution of an engine ``role`` (see :data:`memspine.prompts.roles.CATALOGUE_ROLE`)."""
    cat = CATALOGUE_ROLE.get(role)
    if cat is None:
        return Resolution(role, "", "unmapped", reason="role has no tier mapping")
    b = resolved.plan.binding(cat)
    if b.status == "bound":
        return Resolution(
            role,
            cat,
            "bound",
            tier=_NAME_BY_TIER.get(b.tier, ""),
            model_id=b.model_id,
            device=b.device,
            thinking=b.thinking.mode,
            thinking_cap=b.thinking.cap_tokens,
            reason=b.note,
            reason_code="thinking_not_applied" if b.note else "",
        )
    reason = b.note or f"no model for role {cat}"
    code = {"disabled": "disabled", "error": "required_missing"}.get(b.status, "no_model_bound")
    return Resolution(role, cat, b.status, fallback=b.fallback, reason=reason, reason_code=code)


def role_config(resolved: ResolvedModels, role: str) -> Any | None:
    """The ``LLMRoleConfig`` for a bound engine ``role`` (None when no model serves it).

    Thinking (P-15): off -> ``no_think`` True for a model that can think, else the provider default;
    capped -> ``no_think`` False (the adapter wrapper caps the tokens)."""
    from memspine.config.schema.llm import LLMRoleConfig

    b = role_binding(resolved, role)
    if b is None or b.status != "bound":
        return None
    d = resolved.decls.get(b.model_id)
    info = next((m for m in resolved.inventory.models if m.id == b.model_id), None)
    if d is None:  # only declared llms are bound, but stay safe
        return None
    no_think: bool | None
    if b.thinking.mode == "capped":
        no_think = False
    else:
        no_think = True if (info is not None and info.supports_thinking) else None
    return LLMRoleConfig(
        model=d.id,
        api_base=d.api_base,
        api_key=d.api_key,
        aws_region=d.aws_region,
        timeout_seconds=max(d.timeout_seconds, b.timeout_s),
        no_think=no_think,
    )


def role_binding(resolved: ResolvedModels, role: str) -> RoleBinding | None:
    cat = CATALOGUE_ROLE.get(role)
    return resolved.plan.binding(cat) if cat else None


def rerank_chain(resolved: ResolvedModels) -> list[tuple[str, str]]:
    """The reranker fallback chain as ``(factory mode, device)``: Jina v3.5, Qwen3-Reranker-0.6B,
    MiniLM on CPU, each only if declared and placeable; an empty list means results stay unscored.
    Each link is planned with the higher links removed, so its device is the one it would get
    alone."""
    chain: list[tuple[str, str]] = []
    inv = resolved.inventory
    family_of = {m.id: m.family for m in inv.models if m.kind == "reranker"}
    for _ in range(len(RERANKER_ORDER) * 4):
        plan = plan_models(inv, resolved.hardware, resolved.prefs)
        if not plan.reranker:
            break
        mode = RERANK_MODE.get(family_of.get(plan.reranker, ""))
        if mode and all(mode != m for m, _ in chain):
            chain.append((mode, plan.reranker_device))
        inv = Inventory(tuple(m for m in inv.models if m.id != plan.reranker))
    return chain


def emit_taps(resolved: ResolvedModels, roles: tuple[str, ...] | None = None) -> None:
    """``models.placement`` (the VRAM plan) and one ``models.resolve`` per engine role."""
    if not tap.enabled():
        return
    plan = resolved.plan
    tap.event(
        "models.placement",
        devices=[
            {"device": d, "budget_mb": budget, "used_mb": used} for d, budget, used in plan.memory
        ],
        placed=[
            {"model": p.model_id, "kind": p.kind, "device": p.device, "mb": p.mb}
            for p in plan.placements
        ],
        reranker=plan.reranker,
        reranker_device=plan.reranker_device,
        rerank_gate=plan.rerank_gate,
        embedder=plan.embedder,
        embedder_device=plan.embedder_device,
        n_errors=len(plan.errors),
        n_notes=len(resolved.notes) + len(plan.notes),
    )
    for role in roles if roles is not None else tuple(sorted(CATALOGUE_ROLE)):
        r = resolve_role(resolved, role)
        tap.event(
            "models.resolve",
            role=r.role,
            catalogue_role=r.catalogue_role,
            status=r.status,
            tier=r.tier,
            model=r.model_id,
            device=r.device,
            thinking=r.thinking,
            thinking_cap=r.thinking_cap,
            fallback=r.fallback,
            fallback_reason=r.reason_code,
        )


def report(resolved: ResolvedModels) -> dict[str, Any]:
    """The plan for ``describe()`` and the run manifest."""
    return {
        **resolved.plan.to_dict(),
        "notes": [*resolved.notes, *resolved.plan.notes],
        "rerank_chain": [list(x) for x in rerank_chain(resolved)],
    }
