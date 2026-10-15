"""LIB-13: role policy, echo guard and routing for the insert path (W-06, W-09, I-8).

Pure and unwired (W2-03 wires it): no I/O, no clock, no model call. Raw keeps every role for audit
(W-06 B); this module only decides what *derivation* may use, whether a turn is an echo of recalled
memory, and which stages a turn is routed to.

Echo guard (I-8 A): (a) a recall marker wraps memory text injected into the assistant's context, so a
turn that carries it is an echo ("marked"); (b) a turn whose normalised token shingles overlap a memory
the session's last read rendered is an echo too ("overlap"), which catches echoes that dropped the
marker. Overlap is max(Jaccard, containment of the memory in the turn), so a short echoed span inside a
long turn is caught. An echo never creates new facts or corroboration (ER-17) and inherits the source
family of what it echoes (S4-2): the verdict names the echoed ids and families. A user restatement is
assent, not echo (I-8): a user turn that overlaps is flagged only as an ``assent_candidate``, never as
support.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from memspine.schemas.steps.shared import DeriveUse

__all__ = [
    "DEFAULT_ROLE_RULES",
    "EchoConfig",
    "EchoVerdict",
    "RecallMarker",
    "RoleName",
    "RolePolicy",
    "RoleRule",
    "RoleTurn",
    "Routing",
    "detect_echo",
    "normalise_tokens",
    "route",
    "shingles",
    "wrap_recall",
]

RoleName = Literal["user", "assistant", "tool", "system", "operator", "document", "external"]
SrcClassName = Literal["user_stated", "assistant_proposed", "tool", "document", "external"]

_ROLES: tuple[str, ...] = (
    "user",
    "assistant",
    "tool",
    "system",
    "operator",
    "document",
    "external",
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------------------------- policy


class RoleRule(_Frozen):
    """What one role may do. Raw is always kept; this governs derivation only."""

    derive_use: DeriveUse
    src_class: SrcClassName
    base_trust: float = Field(ge=0.0, le=1.0)
    extract: bool = True  # may the turn feed extraction at all
    firewall_first: bool = True  # gate 1 runs before routing onward
    may_corroborate: bool = True  # may it count toward independent support (S4)

    @model_validator(mode="after")
    def _excluded_never_extracts(self) -> RoleRule:
        if self.derive_use == "excluded" and (self.extract or self.may_corroborate):
            raise ValueError("an excluded role cannot extract or corroborate")
        return self


# Trust defaults follow core/policies/trust.py (user 0.7, assistant 0.5, tool 0.4, operator and system
# 0.9); document and external sit at or below tool, since they originate outside the trust boundary.
DEFAULT_ROLE_RULES: dict[str, RoleRule] = {
    "user": RoleRule(derive_use="stated", src_class="user_stated", base_trust=0.7),
    "assistant": RoleRule(
        derive_use="proposal", src_class="assistant_proposed", base_trust=0.5, may_corroborate=False
    ),
    "tool": RoleRule(
        derive_use="evidence", src_class="tool", base_trust=0.4, may_corroborate=False
    ),
    "system": RoleRule(
        derive_use="excluded",
        src_class="tool",
        base_trust=0.9,
        extract=False,
        may_corroborate=False,
    ),
    "operator": RoleRule(derive_use="stated", src_class="user_stated", base_trust=0.9),
    "document": RoleRule(
        derive_use="evidence", src_class="document", base_trust=0.4, may_corroborate=False
    ),
    "external": RoleRule(
        derive_use="evidence", src_class="external", base_trust=0.3, may_corroborate=False
    ),
}


class EchoConfig(_Frozen):
    enabled: bool = True
    threshold: float = Field(default=0.6, gt=0.0, le=1.0)
    shingle: int = Field(default=3, ge=1, le=8)
    min_memory_shingles: int = Field(default=3, ge=1)  # containment needs at least this many
    marker_open: str = "⟦mem:"  # "<open>ID<close>text<end>"
    marker_close_id: str = "⟧"
    marker_end: str = "⟦/mem⟧"


class RolePolicy(_Frozen):
    """Per-role rules plus echo settings. Every role must have a rule."""

    rules: dict[str, RoleRule] = Field(default_factory=lambda: dict(DEFAULT_ROLE_RULES))
    echo: EchoConfig = Field(default_factory=EchoConfig)

    @model_validator(mode="after")
    def _all_roles(self) -> RolePolicy:
        missing = [r for r in _ROLES if r not in self.rules]
        unknown = [r for r in self.rules if r not in _ROLES]
        if missing or unknown:
            raise ValueError(f"role rules: missing {missing}, unknown {unknown}")
        return self

    def rule(self, role: str) -> RoleRule:
        try:
            return self.rules[role]
        except KeyError:
            raise ValueError(f"unknown role {role!r}") from None


# ------------------------------------------------------------------------------------------ echo guard


class RecallMarker(_Frozen):
    """A memory the assistant was shown in the session's last read."""

    id: str = Field(min_length=1)
    text: str
    family: str | None = None  # source family of the memory (S4-2), if the caller knows it


class EchoVerdict(_Frozen):
    kind: Literal["none", "marked", "overlap"] = "none"
    matched_ids: tuple[str, ...] = ()
    scores: dict[str, float] = Field(default_factory=dict)
    inherited_families: tuple[str, ...] = ()  # families of the echoed memories, sorted

    @property
    def is_echo(self) -> bool:
        return self.kind != "none"


def wrap_recall(memory_id: str, text: str, config: EchoConfig | None = None) -> str:
    """Wrap injected memory text with its id so the guard can recognise it later."""
    c = config or EchoConfig()
    return f"{c.marker_open}{memory_id}{c.marker_close_id}{text}{c.marker_end}"


def _is_token_char(ch: str) -> bool:
    return unicodedata.category(ch)[0] in ("L", "N", "M")


def _is_unspaced_script(ch: str) -> bool:
    o = ord(ch)
    return (
        0x3040 <= o <= 0x30FF
        or 0x3400 <= o <= 0x9FFF
        or 0xF900 <= o <= 0xFAFF
        or 0x0E00 <= o <= 0x0E7F
    )


def _bigrams(chars: list[str]) -> list[str]:
    if len(chars) == 1:
        return ["".join(chars)]
    return ["".join(chars[i : i + 2]) for i in range(len(chars) - 1)]


def normalise_tokens(text: str) -> list[str]:
    """NFKC, casefold, split on anything but letters/numbers/marks; unspaced scripts -> bigrams."""
    s = unicodedata.normalize("NFKC", text).casefold()
    words: list[str] = []
    cur: list[str] = []
    for ch in s:
        if _is_token_char(ch):
            cur.append(ch)
        elif cur:
            words.append("".join(cur))
            cur = []
    if cur:
        words.append("".join(cur))
    out: list[str] = []
    for w in words:
        run: list[str] = []
        spaced: list[str] = []
        for ch in w:
            if _is_unspaced_script(ch):
                if spaced:
                    out.append("".join(spaced))
                    spaced = []
                run.append(ch)
            else:
                if run:
                    out.extend(_bigrams(run))
                    run = []
                spaced.append(ch)
        if run:
            out.extend(_bigrams(run))
        if spaced:
            out.append("".join(spaced))
    return out


def shingles(text: str, k: int = 3) -> frozenset[tuple[str, ...]]:
    toks = normalise_tokens(text)
    if not toks:
        return frozenset()
    if len(toks) < k:
        return frozenset({tuple(toks)})
    return frozenset(tuple(toks[i : i + k]) for i in range(len(toks) - k + 1))


def _overlap_score(
    turn: frozenset[tuple[str, ...]], mem: frozenset[tuple[str, ...]], min_mem: int
) -> float:
    if not turn or not mem:
        return 0.0
    inter = len(turn & mem)
    if inter == 0:
        return 0.0
    jac = inter / len(turn | mem)
    cont = inter / len(mem) if len(mem) >= min_mem else 0.0
    return max(jac, cont)


def detect_echo(
    content: str,
    recalled: list[RecallMarker] | tuple[RecallMarker, ...],
    config: EchoConfig | None = None,
) -> EchoVerdict:
    """Marker first, then last-read overlap. Deterministic; ids sorted."""
    c = config or EchoConfig()
    if not c.enabled or not content:
        return EchoVerdict()
    by_id = {m.id: m for m in recalled}
    pat = re.compile(re.escape(c.marker_open) + r"(.+?)" + re.escape(c.marker_close_id))
    marked = set(pat.findall(content))
    scores: dict[str, float] = {}
    if recalled:
        t = shingles(content, c.shingle)
        for m in recalled:
            sc = _overlap_score(t, shingles(m.text, c.shingle), c.min_memory_shingles)
            if sc >= c.threshold:
                scores[m.id] = max(scores.get(m.id, 0.0), round(sc, 6))
    for mid in marked:
        scores[mid] = 1.0
    if not scores:
        return EchoVerdict()
    ids = tuple(sorted(scores))
    fams = tuple(sorted({by_id[i].family for i in ids if i in by_id and by_id[i].family}))
    return EchoVerdict(
        kind="marked" if marked else "overlap",
        matched_ids=ids,
        scores={i: scores[i] for i in ids},
        inherited_families=fams,
    )


# ----------------------------------------------------------------------------------------------- routing


class RoleTurn(_Frozen):
    role: RoleName
    content: str = ""


class Routing(_Frozen):
    """Where a turn goes. ``raw`` is always true (W-06 B)."""

    raw: bool = True
    firewall_first: bool
    extraction: bool
    derive_use: DeriveUse
    src_class: SrcClassName
    base_trust: float
    count_as_support: bool  # may it raise support or corroboration
    assent_candidate: bool = False  # user turn that restates a shown memory (assent, not echo)
    echo: bool = False
    inherited_families: tuple[str, ...] = ()
    inherits_family_of: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()


def route(turn: RoleTurn, policy: RolePolicy, echo_verdict: EchoVerdict | None = None) -> Routing:
    rule = policy.rule(turn.role)
    v = echo_verdict or EchoVerdict()
    reasons = [f"role:{turn.role}", f"derive_use:{rule.derive_use}"]
    extraction = rule.extract
    support = rule.may_corroborate
    derive_use: DeriveUse = rule.derive_use
    echo = False
    assent = False
    if not rule.extract:
        reasons.append("role excluded from extraction")
    if v.is_echo:
        extraction = False
        support = False
        if turn.role == "user":
            # I-8: a user restatement counts as assent, not echo; never as independent support.
            assent = True
            reasons.append(f"user restates shown memory ({v.kind}): assent candidate, no support")
        else:
            echo = True
            derive_use = "excluded"
            reasons.append(f"echo ({v.kind}) of {','.join(v.matched_ids)}: raw only, no new facts")
            if v.inherited_families:
                reasons.append("inherits family " + ",".join(v.inherited_families))
    if rule.firewall_first:
        reasons.append("firewall gate 1 first")
    return Routing(
        firewall_first=rule.firewall_first,
        extraction=extraction,
        derive_use=derive_use,
        src_class=rule.src_class,
        base_trust=rule.base_trust,
        count_as_support=support,
        assent_candidate=assent,
        echo=echo,
        inherited_families=v.inherited_families if (echo or assent) else (),
        inherits_family_of=v.matched_ids if (echo or assent) else (),
        reasons=tuple(reasons),
    )
