"""Model tiers, placement, no-model fallbacks and thinking policy (MD-01 library, LIB-42).

Pure planning, no I/O and no model calls: ``plan_models`` takes what is installed
(``Inventory``), the machine (``Hardware``) and preferences (``PlanPrefs``) and returns
a ``ModelPlan`` that says which model serves each role, on which device, with which
thinking setting, and which capabilities are unavailable because a model is missing.
Unwired: nothing imports this yet.

Decisions encoded: P-01 (worker and reader tiers, roles pinnable, one model may serve
both), P-02 (roles that read raw turns stay local; only reader-side roles may be hosted,
and only when the preferences allow it), P-03 (explicit per-role fallback; a role the
caller marks required that cannot be served is an error entry, not a silent skip),
P-15 (thinking off everywhere by default; capped thinking only for background roles and
only on opt-in), P-19 (one device plan: LLM, then reranker, then embedder, with a
memory budget per device), Q5/Q6 (reranker order Jina v3.5, Qwen3-Reranker-0.6B, MiniLM
CPU, none; an unscored result is never a zero score), layer policy (L0 always; L1 and L3
only when a worker model is bound).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import IntEnum
from typing import Any, Literal

__all__ = [
    "RERANKER_ORDER",
    "ROLE_CATALOGUE",
    "Capability",
    "GpuInfo",
    "Hardware",
    "Inventory",
    "ModelInfo",
    "ModelPlan",
    "PlanPrefs",
    "RoleBinding",
    "RoleSpec",
    "ThinkingPolicy",
    "Tier",
    "explain",
    "plan_models",
]


class Tier(IntEnum):
    """Capability class of a model (ER-39 cost classes map onto these)."""

    NONE = 0
    SMALL = 1  # small local model
    MEDIUM = 2  # medium local model (today's single 9B)
    LARGE = 3  # large local or cloud model


ModelKind = Literal["llm", "reranker", "embedder"]
Group = Literal["worker", "reader"]

#: Reranker preference order by ``ModelInfo.family`` (Q5/Q6). Absent -> unscored.
RERANKER_ORDER: tuple[str, ...] = ("jina-v3.5", "qwen3-reranker-0.6b", "minilm")
#: Families that run only on CPU by design.
_CPU_ONLY_FAMILIES = frozenset({"minilm"})

_CPU = "cpu"
_REMOTE = "remote"


@dataclass(frozen=True)
class RoleSpec:
    """One model role: tier, thinking allowance, token and time defaults, fallback."""

    name: str
    group: Group
    min_tier: Tier
    #: Capped thinking may be opted into (P-15). False means never, whatever the prefs say.
    thinking_allowed: bool = False
    thinking_cap_tokens: int = 0
    max_tokens: int = 1024
    timeout_s: float = 60.0
    #: Reads raw turns or records (stays local); False means a hosted model is acceptable (P-02).
    hosted_ok: bool = False
    hot_path: bool = False
    #: Capabilities that are unavailable when this role has no model.
    capabilities: tuple[str, ...] = ()
    #: Explicit no-model fallback key (P-03) and a one-line description.
    fallback: str = "skip"
    fallback_text: str = ""


Capability = str

ROLE_CATALOGUE: tuple[RoleSpec, ...] = (
    RoleSpec(
        "extract",
        "worker",
        Tier.SMALL,
        max_tokens=2048,
        timeout_s=90.0,
        capabilities=("layer.L1",),
        fallback="rules",
        fallback_text="no worker model: L1 facts off, L0 raw only (rules pre-pass if installed)",
    ),
    RoleSpec(
        "extract_edges",
        "worker",
        Tier.SMALL,
        max_tokens=2048,
        timeout_s=90.0,
        capabilities=("layer.L3",),
        fallback="skip",
        fallback_text="no worker model: L3 graph off",
    ),
    RoleSpec(
        "summarize",
        "worker",
        Tier.SMALL,
        max_tokens=1536,
        timeout_s=90.0,
        capabilities=("layer.L2",),
        fallback="skip",
        fallback_text="no worker model: summaries and episodes off",
    ),
    RoleSpec(
        "call2",
        "worker",
        Tier.SMALL,
        max_tokens=1536,
        timeout_s=90.0,
        capabilities=("write.call2",),
        fallback="skip",
        fallback_text="no worker model: second write call off",
    ),
    RoleSpec(
        "resolve",
        "worker",
        Tier.SMALL,
        max_tokens=512,
        timeout_s=45.0,
        capabilities=("write.resolve_confirm",),
        fallback="rules",
        fallback_text="no worker model: entity resolution by rules only",
    ),
    RoleSpec(
        "dedupe",
        "worker",
        Tier.SMALL,
        max_tokens=512,
        timeout_s=45.0,
        capabilities=("write.dedupe_confirm",),
        fallback="rules",
        fallback_text="no worker model: dedupe by hash and similarity only",
    ),
    RoleSpec(
        "sufficiency",
        "worker",
        Tier.SMALL,
        max_tokens=128,
        timeout_s=15.0,
        hosted_ok=True,
        hot_path=True,
        capabilities=("read.sufficiency",),
        fallback="skip",
        fallback_text="no worker model: sufficiency check off, single pass read",
    ),
    RoleSpec(
        "query_rewrite",
        "worker",
        Tier.SMALL,
        max_tokens=128,
        timeout_s=15.0,
        hosted_ok=True,
        hot_path=True,
        capabilities=("read.query_rewrite",),
        fallback="skip",
        fallback_text="no worker model: query rewrite off, query used as given",
    ),
    RoleSpec(
        "relevance",
        "worker",
        Tier.SMALL,
        max_tokens=64,
        timeout_s=15.0,
        hot_path=True,
        capabilities=("read.relevance_model_gate",),
        fallback="calibrated",
        fallback_text="no worker model: relevance gate uses the store-calibrated threshold only",
    ),
    RoleSpec(
        "chat",
        "reader",
        Tier.MEDIUM,
        max_tokens=1024,
        timeout_s=120.0,
        hosted_ok=True,
        hot_path=True,
        capabilities=("answer",),
        fallback="skip",
        fallback_text="no reader model: evidence is returned, no generated answer",
    ),
    RoleSpec(
        "verify",
        "reader",
        Tier.MEDIUM,
        max_tokens=256,
        timeout_s=60.0,
        hosted_ok=True,
        capabilities=("answer.verify",),
        fallback="skip",
        fallback_text="no reader model: answer verification off",
    ),
    RoleSpec(
        "judge",
        "reader",
        Tier.MEDIUM,
        thinking_allowed=True,
        thinking_cap_tokens=1024,
        max_tokens=512,
        timeout_s=120.0,
        capabilities=("derive.conflict_judge",),
        fallback="skip",
        fallback_text="no reader model: conflicts kept as contested, not adjudicated",
    ),
    RoleSpec(
        "reflect",
        "reader",
        Tier.MEDIUM,
        thinking_allowed=True,
        thinking_cap_tokens=2048,
        max_tokens=1536,
        timeout_s=180.0,
        capabilities=("layer.reflect",),
        fallback="skip",
        fallback_text="no reader model: reflection off",
    ),
    RoleSpec(
        "plan",
        "reader",
        Tier.MEDIUM,
        max_tokens=512,
        timeout_s=60.0,
        hosted_ok=True,
        capabilities=("read.plan",),
        fallback="skip",
        fallback_text="no reader model: query planning off, fixed retrieval legs",
    ),
)

_SPEC_BY_NAME = {r.name: r for r in ROLE_CATALOGUE}


@dataclass(frozen=True)
class GpuInfo:
    name: str
    vram_mb: int


@dataclass(frozen=True)
class Hardware:
    """The machine. ``ram_mb`` of 0 means unknown (RAM is then not checked)."""

    gpus: tuple[GpuInfo, ...] = ()
    ram_mb: int = 0
    cpu_threads: int = 0


@dataclass(frozen=True)
class ModelInfo:
    """One installed or configured model with caller-supplied memory estimates."""

    id: str
    kind: ModelKind
    provider: str = "local"
    tier: Tier = Tier.NONE  # llm only
    family: str = ""  # reranker family (see RERANKER_ORDER)
    hosted: bool = False
    vram_mb: int = 0
    ram_mb: int = 0  # CPU footprint; 0 falls back to vram_mb
    context_vram_mb: int = 0  # llm: reserved context memory, added to whichever device hosts it
    cpu_ok: bool = True
    gpu_ok: bool = True
    supports_thinking: bool = False
    profile: str = ""  # embedder: "cpu" (default multilingual) or "gpu" (instruction embedder)

    @property
    def cpu_mb(self) -> int:
        return (self.ram_mb or self.vram_mb) + self.context_vram_mb

    @property
    def gpu_mb(self) -> int:
        return self.vram_mb + self.context_vram_mb


@dataclass(frozen=True)
class Inventory:
    models: tuple[ModelInfo, ...] = ()

    def of_kind(self, kind: str) -> tuple[ModelInfo, ...]:
        return tuple(sorted((m for m in self.models if m.kind == kind), key=lambda m: m.id))


@dataclass(frozen=True)
class PlanPrefs:
    #: role -> model id; wins over selection (still checked against inventory and privacy).
    pins: Mapping[str, str] = field(default_factory=dict)
    #: Roles to leave off even when a model could serve them.
    disabled_roles: frozenset[str] = frozenset()
    #: Roles a feature needs; a required role that cannot be served is an error entry (P-03).
    required_roles: frozenset[str] = frozenset()
    #: Hosted models may serve roles marked ``hosted_ok`` only when this is True (P-02).
    allow_hosted: bool = False
    #: Roles for which capped thinking is opted into; honoured only where the role allows it.
    thinking_optin: frozenset[str] = frozenset()
    embedder_profile: Literal["auto", "cpu", "gpu"] = "auto"
    #: Embedder id fixed by the store (MD-02 hook); wins over the profile choice.
    pinned_embedder: str | None = None
    #: Reranker off by request.
    rerank: bool = True
    #: VRAM kept free on every GPU.
    gpu_headroom_mb: int = 512


@dataclass(frozen=True)
class ThinkingPolicy:
    mode: Literal["off", "capped"] = "off"
    cap_tokens: int = 0
    #: Provider-level switch to apply, e.g. ``{"think": False}`` for Ollama.
    provider_options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RoleBinding:
    role: str
    group: str
    status: Literal["bound", "fallback", "disabled", "error"]
    model_id: str = ""
    provider: str = ""
    tier: Tier = Tier.NONE
    device: str = ""
    thinking: ThinkingPolicy = field(default_factory=ThinkingPolicy)
    max_tokens: int = 0
    timeout_s: float = 0.0
    shared_with: tuple[str, ...] = ()
    fallback: str = ""
    note: str = ""


@dataclass(frozen=True)
class _Placed:
    model_id: str
    kind: str
    device: str
    mb: int


@dataclass(frozen=True)
class ModelPlan:
    roles: tuple[RoleBinding, ...]
    placements: tuple[_Placed, ...]
    #: device -> (budget_mb, used_mb); budget 0 means unchecked.
    memory: tuple[tuple[str, int, int], ...]
    reranker: str = ""
    reranker_device: str = ""
    #: "always" on GPU, "gated" on CPU, "" when none.
    rerank_gate: str = ""
    #: True when no reranker is bound: results stay unscored (None), never 0.
    rerank_unscored: bool = True
    embedder: str = ""
    embedder_device: str = ""
    available: tuple[str, ...] = ()
    unavailable: tuple[tuple[str, str], ...] = ()
    errors: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def binding(self, role: str) -> RoleBinding:
        for b in self.roles:
            if b.role == role:
                return b
        raise KeyError(role)

    @property
    def layers(self) -> tuple[str, ...]:
        """Layers the plan can populate: L0 always; L1 and L3 only with a worker model."""
        out = ["L0"]
        for layer, cap in (("L1", "layer.L1"), ("L3", "layer.L3")):
            if cap in self.available:
                out.append(layer)
        return tuple(out)

    def to_dict(self) -> dict[str, Any]:
        return {
            "roles": [
                {
                    "role": b.role,
                    "status": b.status,
                    "model": b.model_id,
                    "device": b.device,
                    "thinking": b.thinking.mode,
                    "thinking_cap": b.thinking.cap_tokens,
                    "fallback": b.fallback,
                }
                for b in self.roles
            ],
            "placements": [
                {"model": p.model_id, "kind": p.kind, "device": p.device, "mb": p.mb}
                for p in self.placements
            ],
            "reranker": self.reranker,
            "rerank_gate": self.rerank_gate,
            "embedder": self.embedder,
            "layers": list(self.layers),
            "unavailable": [list(u) for u in self.unavailable],
            "errors": list(self.errors),
        }


# --------------------------------------------------------------------------------------
# Memory ledger
# --------------------------------------------------------------------------------------


class _Ledger:
    def __init__(self, hw: Hardware, headroom: int) -> None:
        self.gpu_free: dict[str, int] = {}
        self.gpu_budget: dict[str, int] = {}
        for i, g in enumerate(hw.gpus):
            dev = f"cuda:{i}"
            budget = max(0, g.vram_mb - headroom)
            self.gpu_budget[dev] = budget
            self.gpu_free[dev] = budget
        self.ram_budget = hw.ram_mb
        self.ram_free = hw.ram_mb
        self.placed: list[_Placed] = []

    def try_place(self, m: ModelInfo, *, prefer_gpu: bool = True) -> str | None:
        if m.hosted:
            self.placed.append(_Placed(m.id, m.kind, _REMOTE, 0))
            return _REMOTE
        if prefer_gpu and m.gpu_ok:
            for dev in sorted(self.gpu_free):
                if self.gpu_free[dev] >= m.gpu_mb:
                    self.gpu_free[dev] -= m.gpu_mb
                    self.placed.append(_Placed(m.id, m.kind, dev, m.gpu_mb))
                    return dev
        if m.cpu_ok and (self.ram_budget == 0 or self.ram_free >= m.cpu_mb):
            if self.ram_budget:
                self.ram_free -= m.cpu_mb
            self.placed.append(_Placed(m.id, m.kind, _CPU, m.cpu_mb))
            return _CPU
        return None

    def usage(self) -> tuple[tuple[str, int, int], ...]:
        rows = [
            (d, self.gpu_budget[d], self.gpu_budget[d] - self.gpu_free[d])
            for d in sorted(self.gpu_budget)
        ]
        rows.append(
            (_CPU, self.ram_budget, self.ram_budget - self.ram_free if self.ram_budget else 0)
        )
        return tuple(rows)


# --------------------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------------------


def _eligible(spec: RoleSpec, pool: tuple[ModelInfo, ...], prefs: PlanPrefs) -> list[ModelInfo]:
    out = []
    for m in pool:
        if m.tier < spec.min_tier:
            continue
        if m.hosted and not (prefs.allow_hosted and spec.hosted_ok):
            continue
        out.append(m)
    return out


def _choose(spec: RoleSpec, pool: tuple[ModelInfo, ...], prefs: PlanPrefs) -> ModelInfo | None:
    cands = _eligible(spec, pool, prefs)
    if not cands:
        return None
    if spec.group == "worker":
        # cheapest adequate: local before hosted, lowest tier, least memory, then id
        return min(cands, key=lambda m: (m.hosted, m.tier, m.gpu_mb, m.id))
    # reader: strongest; local before hosted only on equal tier
    return min(cands, key=lambda m: (-int(m.tier), m.hosted, m.gpu_mb, m.id))


def _thinking(
    spec: RoleSpec, model: ModelInfo | None, prefs: PlanPrefs
) -> tuple[ThinkingPolicy, str]:
    opts: dict[str, Any] = {}
    if model is not None:
        opts = {"think": False} if model.provider == "ollama" else {"no_think": True}
    if spec.thinking_allowed and spec.name in prefs.thinking_optin and model is not None:
        if model.supports_thinking:
            return ThinkingPolicy("capped", spec.thinking_cap_tokens, {}), ""
        return ThinkingPolicy(
            "off", 0, opts
        ), "thinking requested but the model does not support it"
    note = ""
    if spec.name in prefs.thinking_optin and not spec.thinking_allowed:
        note = "thinking not allowed for this role; kept off"
    return ThinkingPolicy("off", 0, opts), note


def _plan_llms(
    inv: Inventory, hw: Hardware, prefs: PlanPrefs
) -> tuple[dict[str, ModelInfo], dict[str, str], _Ledger, list[str], list[str]]:
    """Resolve roles to models and place them; drop models that fit nowhere and retry."""
    pool = inv.of_kind("llm")
    dropped: set[str] = set()
    drop_notes: list[str] = []
    errors: list[str] = []
    while True:
        live = tuple(m for m in pool if m.id not in dropped)
        chosen: dict[str, ModelInfo] = {}
        errors = []
        for spec in ROLE_CATALOGUE:
            if spec.name in prefs.disabled_roles:
                continue
            pin = prefs.pins.get(spec.name)
            if pin:
                m = next((x for x in live if x.id == pin), None)
                if m is None:
                    errors.append(f"role {spec.name}: pinned model {pin} is not available")
                elif m.tier < spec.min_tier:
                    errors.append(
                        f"role {spec.name}: pin {pin} is below tier {spec.min_tier.name.lower()}"
                    )
                elif m.hosted and not (prefs.allow_hosted and spec.hosted_ok):
                    errors.append(
                        f"role {spec.name}: pin {pin} is hosted and this role must stay local"
                    )
                else:
                    chosen[spec.name] = m
                continue
            m = _choose(spec, live, prefs)
            if m is not None:
                chosen[spec.name] = m
        # place distinct models: reader-side (strongest) first, then by id
        distinct: dict[str, ModelInfo] = {}
        for m in chosen.values():
            distinct[m.id] = m
        order = sorted(distinct.values(), key=lambda m: (-int(m.tier), m.id))
        ledger = _Ledger(hw, prefs.gpu_headroom_mb)
        device: dict[str, str] = {}
        failed = None
        for m in order:
            d = ledger.try_place(m)
            if d is None:
                failed = m
                break
            device[m.id] = d
        if failed is None:
            return chosen, device, ledger, errors, drop_notes
        dropped.add(failed.id)
        drop_notes.append(f"model {failed.id} fits no device and was dropped")


def _plan_reranker(
    inv: Inventory, ledger: _Ledger, prefs: PlanPrefs
) -> tuple[ModelInfo | None, str, list[str]]:
    notes: list[str] = []
    if not prefs.rerank:
        return None, "", ["reranker turned off by preference"]
    rerankers = inv.of_kind("reranker")
    for family in RERANKER_ORDER:
        for m in rerankers:
            if m.family != family:
                continue
            gpu_ok = m.gpu_ok and family not in _CPU_ONLY_FAMILIES
            probe = m if gpu_ok else replace(m, gpu_ok=False)
            dev = ledger.try_place(probe)
            if dev is not None:
                return m, dev, notes
            notes.append(f"reranker {m.id} fits no device; trying the next in the chain")
    return None, "", notes


def _plan_embedder(
    inv: Inventory, ledger: _Ledger, prefs: PlanPrefs
) -> tuple[ModelInfo | None, str, list[str], list[str]]:
    notes: list[str] = []
    errors: list[str] = []
    embedders = inv.of_kind("embedder")
    if prefs.pinned_embedder:
        m = next((e for e in embedders if e.id == prefs.pinned_embedder), None)
        if m is None:
            errors.append(
                f"embedder {prefs.pinned_embedder} is pinned by the store but not available"
            )
            return None, "", notes, errors
        dev = ledger.try_place(m)
        if dev is None:
            errors.append(f"pinned embedder {m.id} fits no device")
            return None, "", notes, errors
        return m, dev, notes, errors
    want = prefs.embedder_profile
    gpu_first = [e for e in embedders if e.profile == "gpu"]
    cpu_first = [e for e in embedders if e.profile != "gpu"]
    if want == "gpu":
        order = gpu_first + cpu_first
    elif want == "cpu":
        order = cpu_first
    else:
        order = gpu_first + cpu_first
    for m in order:
        if m.profile == "gpu":
            # the GPU profile is chosen only when it really lands on a GPU
            probe = replace(m, cpu_ok=False)
            dev = ledger.try_place(probe)
            if dev is not None:
                return m, dev, notes, errors
            notes.append(f"gpu embedder {m.id} does not fit a GPU; using the CPU default")
            continue
        dev = ledger.try_place(replace(m, gpu_ok=False))
        if dev is not None:
            return m, dev, notes, errors
    return None, "", notes, errors


def plan_models(
    available: Inventory, hardware: Hardware, prefs: PlanPrefs | None = None
) -> ModelPlan:
    """Build the model plan. Pure and deterministic for equal arguments."""
    prefs = prefs or PlanPrefs()
    chosen, device, ledger, errors, notes = _plan_llms(available, hardware, prefs)
    rr, rr_dev, rr_notes = _plan_reranker(available, ledger, prefs)
    emb, emb_dev, emb_notes, emb_err = _plan_embedder(available, ledger, prefs)
    notes += rr_notes + emb_notes
    errors += emb_err

    users: dict[str, list[str]] = {}
    for role, m in chosen.items():
        users.setdefault(m.id, []).append(role)

    bindings: list[RoleBinding] = []
    lost: list[tuple[str, str]] = []
    avail: list[str] = ["layer.L0"]
    for spec in ROLE_CATALOGUE:
        m = chosen.get(spec.name)
        if spec.name in prefs.disabled_roles:
            bindings.append(
                RoleBinding(
                    spec.name,
                    spec.group,
                    "disabled",
                    fallback="skip",
                    note="disabled by preference",
                )
            )
            for c in spec.capabilities:
                lost.append((c, f"role {spec.name} disabled by preference"))
            continue
        if m is None:
            required = spec.name in prefs.required_roles
            status = "error" if required else "fallback"
            reason = (
                f"no model for role {spec.name} (needs tier {spec.min_tier.name.lower()} or above)"
            )
            if required:
                errors.append(reason)
            bindings.append(
                RoleBinding(
                    spec.name, spec.group, status, fallback=spec.fallback, note=spec.fallback_text
                )
            )
            for c in spec.capabilities:
                lost.append((c, spec.fallback_text or reason))
            continue
        think, tnote = _thinking(spec, m, prefs)
        others = tuple(sorted(r for r in users[m.id] if r != spec.name))
        bindings.append(
            RoleBinding(
                spec.name,
                spec.group,
                "bound",
                m.id,
                m.provider,
                m.tier,
                device[m.id],
                think,
                spec.max_tokens,
                spec.timeout_s,
                others,
                note=tnote,
            )
        )
        avail.extend(spec.capabilities)

    if rr is not None:
        avail.append("rerank")
    else:
        lost.append(
            ("rerank", "no reranker installed or placeable: results stay unscored (never 0)")
        )
    if emb is not None:
        avail.append("vector_search")
    else:
        lost.append(("vector_search", "no embedder available: lexical search only"))

    avail_t = tuple(dict.fromkeys(avail))
    lost_t = tuple(sorted(dict.fromkeys(lost)))
    return ModelPlan(
        roles=tuple(bindings),
        placements=tuple(ledger.placed),
        memory=ledger.usage(),
        reranker=rr.id if rr else "",
        reranker_device=rr_dev,
        rerank_gate=("" if rr is None else ("always" if rr_dev.startswith("cuda") else "gated")),
        rerank_unscored=rr is None,
        embedder=emb.id if emb else "",
        embedder_device=emb_dev,
        available=avail_t,
        unavailable=lost_t,
        errors=tuple(dict.fromkeys(errors)),
        notes=tuple(dict.fromkeys(notes)),
    )


def explain(plan: ModelPlan) -> list[str]:
    """Human-readable lines for ``describe()``, receipts and the run manifest."""
    lines: list[str] = [f"layers: {', '.join(plan.layers)}"]
    for b in plan.roles:
        if b.status == "bound":
            share = f" (shared with {', '.join(b.shared_with)})" if b.shared_with else ""
            think = (
                "thinking off"
                if b.thinking.mode == "off"
                else f"thinking capped at {b.thinking.cap_tokens}"
            )
            extra = f"; {b.note}" if b.note else ""
            lines.append(f"role {b.role}: {b.model_id} on {b.device}, {think}{share}{extra}")
        elif b.status == "disabled":
            lines.append(f"role {b.role}: disabled ({b.note})")
        else:
            lines.append(f"role {b.role}: {b.status}, fallback {b.fallback}: {b.note}")
    if plan.reranker:
        lines.append(
            f"reranker: {plan.reranker} on {plan.reranker_device}, gate {plan.rerank_gate}"
        )
    else:
        lines.append("reranker: none, results unscored")
    lines.append(
        f"embedder: {plan.embedder} on {plan.embedder_device}"
        if plan.embedder
        else "embedder: none"
    )
    for dev, budget, used in plan.memory:
        if budget:
            lines.append(f"memory {dev}: {used} of {budget} MB")
    for cap, why in plan.unavailable:
        lines.append(f"unavailable {cap}: {why}")
    lines.extend(f"error: {e}" for e in plan.errors)
    lines.extend(f"note: {n}" for n in plan.notes)
    return lines
