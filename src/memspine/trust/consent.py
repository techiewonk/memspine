"""Privacy tiers, consent and purpose scopes: admission predicates for write and read (LIB-18).

Pure and deterministic: no I/O, no model calls, no engine imports, no clock (callers pass
timestamps). Not wired yet (T5-03 wires it). Decisions: G-10 (tiers; a detector supplies the label,
nothing here detects), G-11 (purpose scopes plus a per-subject do-not-derive switch), ER-31
(consent and purpose are part of cache keys through :attr:`ConsentRegistry.generation`), S3-1
(scope is tenant plus owner).

* **Tiers** order ``public < personal < sensitive < special_category``. :func:`tier_from_pii` maps
  the engine's ``none/low/high/regulated`` labels. Sensitive and special-category are
  **default-deny**: nothing is stored or read for them without an explicit grant.
* **Purposes** are ``:``-separated paths (``personalisation`` is broader than
  ``personalisation:diet``); ``*`` is the broadest. A record stored under purpose ``P`` is served
  to a read declaring purpose ``R`` only when ``R`` is ``P`` or narrower than ``P``. A record kept
  under a narrow purpose is therefore never returned to a broader read.
* **Consent** is an append-only log of grant/revoke entries per (tenant, owner, purpose, grantee).
  For a question about purpose ``X`` the most specific covering entry wins, ties go to the latest.
  With no entry the default is allow for public/personal and deny above. Revoking hides the
  records stored under that purpose (and narrower ones) from every read, without deleting them;
  erasure is separate.
* **Generation**: every registry change returns a new registry with ``generation + 1`` (even a
  repeated identical grant, the conservative choice), and the generation belongs in cache keys.
* **Viewers**: the owner; a member of a group the record is shared with (sensitive and above need
  an explicit grantee ``group:<id>`` grant); an agent (always needs an explicit grant naming it).
"""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Literal

from pydantic import ConfigDict

from memspine.schemas import InternalModel

__all__ = [
    "PURPOSE_ANY",
    "AdmissionDecision",
    "ConsentEntry",
    "ConsentRegistry",
    "Reason",
    "RecordMeta",
    "Tier",
    "Viewer",
    "ViewerKind",
    "group_grantee",
    "may_derive",
    "may_read",
    "may_store",
    "purpose_covers",
    "tier_from_pii",
]

PURPOSE_ANY = "*"
_SEP = ":"


class Tier(IntEnum):
    """Privacy tier, ordered by sensitivity."""

    PUBLIC = 0
    PERSONAL = 1
    SENSITIVE = 2
    SPECIAL_CATEGORY = 3


_PII_TO_TIER = {
    "none": Tier.PUBLIC,
    "low": Tier.PERSONAL,
    "high": Tier.SENSITIVE,
    "regulated": Tier.SPECIAL_CATEGORY,
}


def tier_from_pii(label: str) -> Tier:
    """Map a detector label (``none/low/high/regulated``, or a tier name) to a tier.

    Unknown labels fail closed to the most restrictive tier.
    """
    key = str(getattr(label, "value", label)).strip().lower()
    if key in _PII_TO_TIER:
        return _PII_TO_TIER[key]
    for tier in Tier:
        if tier.name.lower() == key:
            return tier
    return Tier.SPECIAL_CATEGORY


def _parts(purpose: str) -> tuple[str, ...]:
    p = purpose.strip().lower()
    if p == PURPOSE_ANY or not p:
        return ()
    return tuple(x for x in p.split(_SEP) if x)


def purpose_covers(broad: str, narrow: str) -> bool:
    """True when ``narrow`` is ``broad`` or a sub-purpose of it (``*`` covers everything)."""
    b, n = _parts(broad), _parts(narrow)
    return n[: len(b)] == b


class ViewerKind(StrEnum):
    OWNER = "owner"
    GROUP_MEMBER = "group_member"
    AGENT = "agent"


class Reason(StrEnum):
    ALLOWED = "allowed"
    TENANT_MISMATCH = "tenant_mismatch"
    NO_CONSENT = "no_consent"  # default-deny tier without a grant
    CONSENT_REVOKED = "consent_revoked"
    PURPOSE_TOO_BROAD = "purpose_too_broad"
    NOT_SHARED = "not_shared"
    NO_AGENT_GRANT = "no_agent_grant"
    NO_GROUP_GRANT = "no_group_grant"
    NOT_OWNER = "not_owner"
    DO_NOT_DERIVE = "do_not_derive"


class _Frozen(InternalModel):
    model_config = ConfigDict(frozen=True)


class RecordMeta(_Frozen):
    """What admission needs to know about a record (the tier comes from a detector)."""

    tenant: str
    owner: str
    tier: Tier = Tier.PERSONAL
    purpose: str = PURPOSE_ANY
    shared_groups: frozenset[str] = frozenset()


class Viewer(_Frozen):
    tenant: str
    principal: str
    kind: ViewerKind = ViewerKind.OWNER
    groups: frozenset[str] = frozenset()


class AdmissionDecision(_Frozen):
    allowed: bool
    reason: Reason
    consent_generation: int


class ConsentEntry(_Frozen):
    tenant: str
    owner: str
    purpose: str
    granted: bool
    at: float
    grantee: str | None = None  # None = general consent; else an agent id or ``group:<id>``


def group_grantee(group: str) -> str:
    return f"group:{group}"


class ConsentRegistry(_Frozen):
    """Immutable consent state. Every mutator returns a new registry with ``generation + 1``."""

    generation: int = 0
    entries: tuple[ConsentEntry, ...] = ()
    do_not_derive: frozenset[tuple[str, str, str]] = frozenset()  # (tenant, owner, subject)

    def _with(self, entry: ConsentEntry) -> ConsentRegistry:
        return self.model_copy(
            update={"generation": self.generation + 1, "entries": (*self.entries, entry)}
        )

    def grant(
        self, tenant: str, owner: str, purpose: str, at: float, grantee: str | None = None
    ) -> ConsentRegistry:
        return self._with(
            ConsentEntry(
                tenant=tenant, owner=owner, purpose=purpose, granted=True, at=at, grantee=grantee
            )
        )

    def revoke(
        self, tenant: str, owner: str, purpose: str, at: float, grantee: str | None = None
    ) -> ConsentRegistry:
        return self._with(
            ConsentEntry(
                tenant=tenant, owner=owner, purpose=purpose, granted=False, at=at, grantee=grantee
            )
        )

    def set_do_not_derive(
        self, tenant: str, owner: str, subject: str, on: bool = True
    ) -> ConsentRegistry:
        key = (tenant, owner, subject)
        dnd = self.do_not_derive | {key} if on else self.do_not_derive - {key}
        return self.model_copy(update={"generation": self.generation + 1, "do_not_derive": dnd})

    def decide(
        self, tenant: str, owner: str, purpose: str, grantee: str | None = None
    ) -> Literal["granted", "revoked", "none"]:
        """The winning entry for ``purpose``: most specific covering entry, latest on ties."""
        best: tuple[int, int] | None = None
        verdict: Literal["granted", "revoked", "none"] = "none"
        for i, e in enumerate(self.entries):
            if (e.tenant, e.owner, e.grantee) != (tenant, owner, grantee):
                continue
            if not purpose_covers(e.purpose, purpose):
                continue
            rank = (len(_parts(e.purpose)), i)
            if best is None or rank > best:
                best, verdict = rank, "granted" if e.granted else "revoked"
        return verdict


def _general_ok(reg: ConsentRegistry, meta: RecordMeta, purpose: str) -> Reason:
    v = reg.decide(meta.tenant, meta.owner, purpose)
    if v == "granted":
        return Reason.ALLOWED
    if v == "revoked":
        return Reason.CONSENT_REVOKED
    return Reason.ALLOWED if meta.tier <= Tier.PERSONAL else Reason.NO_CONSENT


def _decision(reg: ConsentRegistry, reason: Reason) -> AdmissionDecision:
    return AdmissionDecision(
        allowed=reason is Reason.ALLOWED, reason=reason, consent_generation=reg.generation
    )


def may_store(meta: RecordMeta, consent: ConsentRegistry, purpose: str) -> AdmissionDecision:
    """May ``meta`` be stored under ``purpose``? Sensitive and above need an explicit grant."""
    return _decision(consent, _general_ok(consent, meta, purpose))


def may_derive(
    consent: ConsentRegistry, tenant: str, owner: str, subject: str
) -> AdmissionDecision:
    """K_P / K_C check (G-11): denied when the subject is switched to do-not-derive."""
    if (tenant, owner, subject) in consent.do_not_derive:
        return _decision(consent, Reason.DO_NOT_DERIVE)
    return _decision(consent, Reason.ALLOWED)


def may_read(
    meta: RecordMeta, consent: ConsentRegistry, reader_purpose: str, viewer: Viewer
) -> AdmissionDecision:
    """May ``viewer`` read ``meta`` for ``reader_purpose``? Fails closed, with a reason code."""
    if viewer.tenant != meta.tenant:
        return _decision(consent, Reason.TENANT_MISMATCH)
    if not purpose_covers(meta.purpose, reader_purpose):
        return _decision(consent, Reason.PURPOSE_TOO_BROAD)
    # Revocation (or a missing opt-in on a default-deny tier) for the stored purpose hides the
    # record from everyone; the reader's own purpose must be permitted too.
    for p in (meta.purpose, reader_purpose):
        r = _general_ok(consent, meta, p)
        if r is not Reason.ALLOWED:
            return _decision(consent, r)

    if viewer.kind is ViewerKind.OWNER:
        if viewer.principal != meta.owner:
            return _decision(consent, Reason.NOT_OWNER)
        return _decision(consent, Reason.ALLOWED)

    if viewer.kind is ViewerKind.AGENT:
        if all(
            consent.decide(meta.tenant, meta.owner, p, viewer.principal) == "granted"
            for p in (meta.purpose, reader_purpose)
        ):
            return _decision(consent, Reason.ALLOWED)
        return _decision(consent, Reason.NO_AGENT_GRANT)

    common = sorted(viewer.groups & meta.shared_groups)
    if not common:
        return _decision(consent, Reason.NOT_SHARED)
    if meta.tier >= Tier.SENSITIVE:
        for g in common:
            gr = group_grantee(g)
            if all(
                consent.decide(meta.tenant, meta.owner, p, gr) == "granted"
                for p in (meta.purpose, reader_purpose)
            ):
                return _decision(consent, Reason.ALLOWED)
        return _decision(consent, Reason.NO_GROUP_GRANT)
    return _decision(consent, Reason.ALLOWED)
