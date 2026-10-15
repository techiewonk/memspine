"""Source families, quarantine release and the restated count (S4-2, S4-3, S4-4).

Pure and deterministic: no I/O, no model calls, no engine imports. Not wired yet (W2-04 wires it).

* **Family (S4-2)**: the asserting principal. A person's family is the authenticated speaker or
  participant id; a document's is its author's person family when known (so her email and her chat are one family), else the document; a tool's is the tool name;
  a web page's is its registrable domain. Family ids carry a kind prefix (``person:``, ``operator:``,
  ``system:``, ``assistant:``, ``doc:``, ``tool:``, ``web:``, ``derived:``, ``unknown:``); principal
  ids themselves are opaque strings. Derived records and assistant echoes inherit their parents'
  families through lineage (:func:`families_of`), so a summary can never vouch for its own sources.
* **Restated (S4-3)**: the same family repeating the same value only increments ``restated``. It is
  neither corroboration nor a release (:func:`restate`).
* **Release (S4-4)**: a quarantined write is released only by a different family, written by a
  user/operator/system source at or above default trust, stating the same content or key. Web, tool and
  document families never release (:func:`corroboration`).

Registrable domains use a small built-in list of common multi-part public suffixes
(:data:`MULTI_PART_SUFFIXES`). It is deliberately not the full Public Suffix List; pass a fuller
``suffixes`` set to :func:`registrable_domain` / :func:`family_of` to plug one in.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable, Collection, Iterable
from typing import Literal, NewType

from pydantic import ConfigDict, Field

from memspine.schemas import InternalModel
from memspine.schemas.durable import LineageEdge, Support

__all__ = [
    "DEFAULT_TRUST",
    "MULTI_PART_SUFFIXES",
    "CorroborationDecision",
    "FamilyId",
    "ReleaseReason",
    "RestateState",
    "SourceDescriptor",
    "SourceKind",
    "Statement",
    "StoredRecord",
    "corroboration",
    "families_of",
    "family_kind",
    "family_of",
    "is_independent_family",
    "registrable_domain",
    "restate",
    "support_of",
]

FamilyId = NewType("FamilyId", str)

SourceKind = Literal[
    "person", "operator", "system", "assistant", "document", "tool", "web", "derived"
]
FamilyKind = Literal[
    "person", "operator", "system", "assistant", "doc", "tool", "web", "derived", "unknown"
]
ReleaseReason = Literal[
    "released",
    "self",
    "incoming_quarantined",
    "below_default_trust",
    "different_content",
    "same_family",
    "external_family",
    "non_independent_family",
]

#: Default trust a releasing source must reach (mirrors ``constants.TRUST_DEFAULT``; kept literal so
#: this module stays free of config imports; callers may pass their own).
DEFAULT_TRUST = 0.5

#: Common multi-part public suffixes. A full Public Suffix List can be passed instead.
MULTI_PART_SUFFIXES: frozenset[str] = frozenset(
    {
        "co.uk", "org.uk", "gov.uk", "ac.uk", "me.uk", "ltd.uk", "plc.uk", "sch.uk", "net.uk",
        "com.au", "net.au", "org.au", "edu.au", "gov.au",
        "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp",
        "co.nz", "org.nz", "govt.nz", "ac.nz",
        "co.in", "org.in", "gov.in", "ac.in", "net.in",
        "com.br", "org.br", "gov.br", "net.br",
        "co.za", "org.za", "gov.za", "ac.za",
        "com.cn", "org.cn", "gov.cn", "net.cn", "edu.cn",
        "com.sg", "com.hk", "com.tw", "com.mx", "com.ar", "co.kr", "co.il", "com.tr",
    }
)  # fmt: skip

_RELEASING_KINDS = frozenset({"person", "operator", "system"})
_EXTERNAL_KINDS = frozenset({"web", "tool", "doc"})
_INDEPENDENT_KINDS = frozenset({"person", "operator", "system", "doc", "tool", "web"})
_UNRESOLVED = FamilyId("derived:unresolved")


class _Frozen(InternalModel):
    model_config = ConfigDict(frozen=True)


class SourceDescriptor(_Frozen):
    """What a record's source is, in the terms S4-2 needs. All principal ids are opaque strings."""

    kind: SourceKind
    principal: str | None = None  # authenticated speaker/participant
    author: str | None = None  # document author, if known
    document_id: str | None = None
    tool_name: str | None = None
    url: str | None = None  # web page url or bare host


class StoredRecord(_Frozen):
    """The slice of a record the corroboration rules read."""

    record_id: str = Field(min_length=1)
    source: SourceDescriptor
    trust: float = Field(ge=0.0, le=1.0)
    quarantined: bool = False
    memory_type: str = "semantic"
    entity: str | None = None
    attribute: str | None = None
    value: str | None = None
    content_fingerprint: str = ""


class CorroborationDecision(_Frozen):
    """Outcome of one release check. ``family`` is the releasing family when released."""

    release: bool
    reason: ReleaseReason
    family: FamilyId | None = None


class RestateState(_Frozen):
    """Support for one value: distinct independent families and the restated count (S4-3)."""

    content_fingerprint: str = ""
    families: tuple[str, ...] = ()
    restated: int = Field(default=0, ge=0)

    @property
    def independent(self) -> int:
        """Number of distinct independent families."""
        return len(self.families)


class Statement(_Frozen):
    """One incoming statement of a value. ``derived`` marks derived records and assistant echoes."""

    families: frozenset[str]
    content_fingerprint: str = ""
    derived: bool = False


# --------------------------------------------------------------------------- families


def registrable_domain(host_or_url: str, suffixes: Collection[str] = MULTI_PART_SUFFIXES) -> str:
    """Registrable domain of a url or host: ``a.b.example.co.uk`` -> ``example.co.uk``.

    No network. IP literals and single-label hosts are returned as they are. ``suffixes`` is the set of
    multi-part public suffixes; swap in a full Public Suffix List to be exact.
    """
    text = host_or_url.strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    elif text.startswith("//"):
        text = text[2:]
    for sep in ("/", "?", "#"):
        text = text.split(sep, 1)[0]
    text = text.rsplit("@", 1)[-1]
    if text.startswith("["):  # IPv6 literal
        return text.split("]", 1)[0].strip("[]")
    if text.count(":") == 1:
        text = text.split(":", 1)[0]
    text = text.strip(".")
    try:
        ipaddress.ip_address(text)
    except ValueError:
        pass
    else:
        return text
    labels = [p for p in text.split(".") if p]
    if len(labels) <= 2:
        return ".".join(labels)
    if ".".join(labels[-2:]) in suffixes:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def family_of(
    source: SourceDescriptor, suffixes: Collection[str] = MULTI_PART_SUFFIXES
) -> FamilyId:
    """The asserting principal of ``source`` as a kind-prefixed family id (S4-2).

    A source with no usable identity gets an ``unknown:`` family, which is non-independent: it can
    never corroborate or add independent support.
    """
    kind = source.kind
    if kind in ("person", "operator", "system"):
        who = source.principal or ("system" if kind == "system" else None)
        return FamilyId(f"{kind}:{who}") if who else FamilyId(f"unknown:{kind}")
    if kind == "assistant":
        return FamilyId(f"assistant:{source.principal or 'assistant'}")
    if kind == "document":
        if source.author:
            return FamilyId(f"person:{source.author}")
        if source.document_id:
            return FamilyId(f"doc:{source.document_id}")
        return FamilyId("unknown:document")
    if kind == "tool":
        return (
            FamilyId(f"tool:{source.tool_name}") if source.tool_name else FamilyId("unknown:tool")
        )
    if kind == "web":
        domain = registrable_domain(source.url, suffixes) if source.url else ""
        return FamilyId(f"web:{domain}") if domain else FamilyId("unknown:web")
    return _UNRESOLVED  # a derived source with no lineage to say whose words it is


def family_kind(family: str) -> FamilyKind:
    """The kind prefix of a family id."""
    head = family.split(":", 1)[0]
    if head in ("person", "operator", "system", "assistant", "doc", "tool", "web", "derived"):
        return head  # type: ignore[return-value]
    return "unknown"


def is_independent_family(family: str) -> bool:
    """Whether a family can add independent support (assistant, derived and unknown cannot)."""
    return family_kind(family) in _INDEPENDENT_KINDS


def families_of(
    record: StoredRecord,
    lineage_lookup: Callable[[str], Iterable[LineageEdge]],
    suffixes: Collection[str] = MULTI_PART_SUFFIXES,
) -> frozenset[FamilyId]:
    """Families a record speaks for.

    ``lineage_lookup(record_id)`` returns the edges whose ``child`` is that record. A record with
    parents (a derived record, or an assistant echo) takes the union of its parents' families: from each
    edge's ``source_family`` if carried, else by walking that parent's own edges. Parents that cannot
    be resolved add ``derived:unresolved``. A parentless record belongs to :func:`family_of` its own
    source (for an assistant, its own non-independent ``assistant:`` family). Context edges are shown to
    the model but assert nothing, so they are skipped.
    """

    def walk(rid: str, seen: frozenset[str]) -> set[str] | None:
        edges = [e for e in lineage_lookup(rid) if e.role != "context"]
        if not edges:
            return None
        out: set[str] = set()
        for edge in edges:
            if edge.source_family:
                out.add(edge.source_family)
            elif edge.parent in seen:
                out.add(_UNRESOLVED)
            else:
                up = walk(edge.parent, seen | {edge.parent})
                out |= up if up else {_UNRESOLVED}
        return out

    inherited = walk(record.record_id, frozenset({record.record_id}))
    if inherited:
        return frozenset(FamilyId(f) for f in inherited)
    return frozenset({family_of(record.source, suffixes)})


# --------------------------------------------------------------------------- release


def _same_content(a: StoredRecord, b: StoredRecord) -> bool:
    if a.content_fingerprint and a.content_fingerprint == b.content_fingerprint:
        return True
    # (entity, attribute) keys compare only within one memory type; keyless records match by
    # fingerprint only; a key match needs the same value (two values on one key contradict).
    return (
        a.memory_type == b.memory_type
        and a.entity is not None
        and a.attribute is not None
        and a.entity == b.entity
        and a.attribute == b.attribute
        and a.value is not None
        and a.value == b.value
    )


def corroboration(
    incoming: StoredRecord,
    held: StoredRecord,
    *,
    incoming_families: frozenset[FamilyId] | None = None,
    held_families: frozenset[FamilyId] | None = None,
    default_trust: float = DEFAULT_TRUST,
) -> CorroborationDecision:
    """May ``incoming`` release the quarantined ``held`` record? (S4-4)

    Families default to :func:`family_of` each source; pass the results of :func:`families_of` to
    apply lineage inheritance. Checks run in order: self, incoming quarantined, below default trust,
    different content, same family (any overlap), external family (web, tool, document), non-independent
    family (assistant, derived, unknown). Only a person/operator/system source whose own family is not
    one of the held record's families can release.
    """
    if incoming.record_id == held.record_id:
        return CorroborationDecision(release=False, reason="self")
    if incoming.quarantined:
        return CorroborationDecision(release=False, reason="incoming_quarantined")
    if incoming.trust < default_trust:
        return CorroborationDecision(release=False, reason="below_default_trust")
    if not _same_content(incoming, held):
        return CorroborationDecision(release=False, reason="different_content")
    inc = incoming_families or frozenset({family_of(incoming.source)})
    hld = held_families or frozenset({family_of(held.source)})
    if inc & hld:
        return CorroborationDecision(release=False, reason="same_family")
    if incoming.source.kind in _RELEASING_KINDS:
        own = sorted(f for f in inc if family_kind(f) in _RELEASING_KINDS)
        if own:
            return CorroborationDecision(release=True, reason="released", family=FamilyId(own[0]))
        return CorroborationDecision(release=False, reason="non_independent_family")
    if incoming.source.kind in ("web", "tool", "document") or any(
        family_kind(f) in _EXTERNAL_KINDS for f in inc
    ):
        return CorroborationDecision(release=False, reason="external_family")
    return CorroborationDecision(release=False, reason="non_independent_family")


# --------------------------------------------------------------------------- restated


def restate(state: RestateState, incoming: Statement) -> RestateState:
    """Fold one statement into the support state (S4-3).

    A derived record or assistant echo changes nothing. Otherwise, for the independent families in the
    statement: a family already counted adds one to ``restated`` (once per statement); a new family
    joins ``families`` (so ``independent`` counts distinct families). Non-independent families
    (assistant, derived, unknown) are ignored. A statement of a different value leaves the state alone.
    """
    if incoming.derived:
        return state
    if state.content_fingerprint and incoming.content_fingerprint != state.content_fingerprint:
        return state
    fresh = sorted(f for f in incoming.families if is_independent_family(f))
    new = [f for f in fresh if f not in state.families]
    repeated = len(new) < len(fresh)
    return RestateState(
        content_fingerprint=state.content_fingerprint or incoming.content_fingerprint,
        families=tuple(sorted({*state.families, *new})),
        restated=state.restated + (1 if repeated else 0),
    )


def support_of(state: RestateState) -> Support:
    """The schema :class:`Support` for a state (independent count agrees with the family list)."""
    return Support(
        independent=state.independent, restated=state.restated, families=list(state.families)
    )
