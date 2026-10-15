"""W2-02: wiring of LIB-29 into the write path (``write.stamping_v2``).

``ClockStamper`` and ``RoleStamper`` are the two typed facades of :func:`memspine.ingest.stamp.stamp`
(one function stays the single source of truth for the rules; each facade fills the fields it owns):

* :class:`ClockStamper`: ``source_at`` / ``source_tz`` / ``recorded_at`` (W-04). The event interval and
  its precision are *not* stamped here; derivation fills them (M-10).
* :class:`RoleStamper`: ``spk``, ``addr``, ``participants[]`` and the v1 participant tags (W-05), plus the
  role's provenance class and base trust (W-08 stage 1).

:class:`ConversationStamper` walks one ``write_messages`` call: it keeps the call's speakers and the last
source time, stamps each turn, and runs the assent check of a user turn against the assistant turn it
answers. :func:`apply_stamp` copies a :class:`~memspine.ingest.stamp.StampedTurn` onto a record; it is
handed to the write door through the :data:`PENDING_STAMP` context variable (the door's signature stays
as it is), and does nothing when the variable is unset, so flag-off writes are untouched.

Participants are stamped on the *raw* turn only. A derived record's participants and visibility are
inherited from its parents in ``derive/write.py`` (FX-EX1A); :func:`participants_of_call` is the helper
other code may use to get the call's participants without re-deriving them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from memspine.ingest.provenance import AssentChecker, AssentVerdict
from memspine.ingest.stamp import StampedTurn, StampIn, StampPolicy, normalise_principal, stamp
from memspine.store.records import MemoryRecord, RecordAxes

__all__ = [
    "ASSENT_TAG_PREFIX",
    "PENDING_STAMP",
    "ClockStamp",
    "ClockStamper",
    "ConversationStamper",
    "PendingStamp",
    "RoleStamp",
    "RoleStamper",
    "apply_stamp",
    "participants_of_call",
]

#: ``assent_of:<assistant record id>`` marks a user turn that is a real assent to that proposal.
ASSENT_TAG_PREFIX = "assent_of:"
_VALID_ROLES = frozenset(
    {"user", "assistant", "tool", "system", "operator", "document", "external"}
)


@dataclass(frozen=True)
class ClockStamp:
    source_at: datetime | None
    source_at_utc: datetime | None
    source_tz: str
    tz_unknown: bool
    recorded_at: datetime
    precision: str
    flags: tuple[str, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class RoleStamp:
    spk: str | None
    addr: str | None
    participants: tuple[str, ...]
    participant_tags: tuple[str, ...]
    family: str
    src_class: str
    base_trust: float
    derive_use: str
    notes: tuple[str, ...]


class ClockStamper:
    """The three clocks of one segment (W-04). Pure; ``recorded_at`` is the caller's clock value."""

    def __init__(self, default_tz: str = "UTC", policy: StampPolicy | None = None) -> None:
        self.default_tz = default_tz
        self.policy = policy or StampPolicy()

    def stamp(
        self,
        source_at: Any,
        recorded_at: datetime,
        *,
        tz: str | None = None,
        envelope_tz: str | None = None,
        last_source_at: datetime | None = None,
    ) -> ClockStamp:
        st = stamp(
            StampIn(
                tenant="-",
                owner="-",
                source_id="-",
                role="user",
                content="",
                source_at=source_at,
                tz=tz,
                envelope_tz=envelope_tz,
                last_source_at=last_source_at,
            ),
            recorded_at=recorded_at,
            default_tz=self.default_tz,
            policy=self.policy,
        )
        return ClockStamp(
            source_at=st.source_at,
            source_at_utc=st.source_at_utc,
            source_tz=st.source_tz,
            tz_unknown=st.tz_unknown,
            recorded_at=st.recorded_at,
            precision=st.precision,
            flags=st.flags,
            notes=st.notes,
        )


class RoleStamper:
    """Speaker, addressee and participants (W-05, M-08) plus the role's class and trust (W-08 stage 1)."""

    def __init__(self, policy: StampPolicy | None = None) -> None:
        self.policy = policy or StampPolicy()

    def stamp(
        self,
        role: str,
        content: str,
        *,
        speaker: str | None = None,
        addressee: str | None = None,
        participants: Sequence[str] | None = None,
        session_speakers: Sequence[str] = (),
    ) -> RoleStamp:
        st = stamp(
            StampIn(
                tenant="-",
                owner="-",
                source_id="-",
                role=role,
                content=content,
                speaker=speaker,
                addressee=addressee,
                participants=participants,
                session_speakers=session_speakers,
            ),
            recorded_at=datetime.now(UTC),
            policy=self.policy,
        )
        return RoleStamp(
            spk=st.spk,
            addr=st.addr,
            participants=st.participants,
            participant_tags=st.participant_tags,
            family=st.family,
            src_class=st.src_class,
            base_trust=st.base_trust,
            derive_use=st.derive_use,
            notes=st.notes,
        )


@dataclass(frozen=True)
class PendingStamp:
    """What the write door applies to the next record it builds."""

    stamped: StampedTurn
    extra_tags: tuple[str, ...] = ()


PENDING_STAMP: ContextVar[PendingStamp | None] = ContextVar("memspine_pending_stamp", default=None)


def participants_of_call(
    messages: Sequence[Mapping[str, Any]], aliases: Mapping[str, str] | None = None
) -> list[str]:
    """Everyone present in one ``write_messages`` call, in order of first turn (v1 convention: a turn with
    no ``speaker`` / ``name`` counts as its role). Normalised principal ids."""
    out: list[str] = []
    for turn in messages:
        if not isinstance(turn, Mapping):
            continue
        who = normalise_principal(
            str(turn.get("speaker") or turn.get("name") or turn.get("role") or ""), aliases
        )
        if who and who not in out:
            out.append(who)
    return out


@dataclass
class ConversationStamper:
    """Stamps the turns of one call in order. Stateful only in the call's speakers and last source time."""

    namespace: str
    source_id: str
    default_tz: str = "UTC"
    policy: StampPolicy = field(default_factory=StampPolicy)
    session_speakers: tuple[str, ...] = ()
    envelope_tz: str | None = None
    last_source_at: datetime | None = None
    assent: AssentChecker = field(default_factory=AssentChecker)
    # the assistant turn the next user turn may answer: (record id, text)
    _proposal: tuple[str, str] | None = None

    def turn(
        self,
        index: int,
        turn: Mapping[str, Any],
        content: str,
        *,
        recorded_at: datetime,
    ) -> PendingStamp:
        role = str(turn.get("role") or "user")
        if role not in _VALID_ROLES:
            role = "external"
        who = turn.get("speaker") or turn.get("name")
        to = turn.get("addressee")
        tz = turn.get("tz") or turn.get("timezone")
        parts = turn.get("participants")
        st = stamp(
            StampIn(
                tenant=self.namespace,
                owner=self.namespace,
                source_id=self.source_id,
                seg_index=index,
                role=role,
                content=content,
                source_at=turn.get("timestamp"),
                tz=tz if isinstance(tz, str) else None,
                envelope_tz=self.envelope_tz,
                speaker=who if isinstance(who, str) else None,
                addressee=to if isinstance(to, str) else None,
                participants=[str(p) for p in parts] if isinstance(parts, list | tuple) else None,
                session_speakers=self.session_speakers,
                last_source_at=self.last_source_at,
            ),
            recorded_at=recorded_at,
            default_tz=self.default_tz,
            policy=self.policy,
        )
        if st.source_at_utc is not None and (
            self.last_source_at is None or st.source_at_utc > self.last_source_at
        ):
            self.last_source_at = st.source_at_utc
        return PendingStamp(st)

    # -- assent (W-08 stage 2) -----------------------------------------------------------------------

    def note_assistant(self, record_id: str, text: str) -> None:
        """Remember the assistant turn the next user turn may answer."""
        self._proposal = (record_id, text)

    def assent_for(self, role: str, content: str) -> tuple[AssentVerdict | None, str | None]:
        """For a user turn: the verdict against the last assistant turn and that turn's record id."""
        if role not in ("user", "operator") or self._proposal is None:
            return None, None
        rid, text = self._proposal
        return self.assent.check(content, text), rid

    def clear_proposal(self) -> None:
        self._proposal = None


def apply_stamp(record: MemoryRecord, pending: PendingStamp) -> MemoryRecord:
    """Copy a stamp onto a raw record. A derived record (has parents) keeps its class and axes."""
    st = pending.stamped
    update: dict[str, Any] = {}
    if st.source_at is not None:
        update["source_at"] = st.source_at
        update["source_precision"] = "day" if st.precision == "day" else "instant"
    update["source_tz"] = st.source_tz
    ax = record.axes
    update["axes"] = RecordAxes(
        speaker=st.spk or ax.speaker,
        subject=list(ax.subject),
        addressee=st.addr or ax.addressee,
        asker=ax.asker,
        participants=list(st.participants),
    )
    if not record.source.parents:
        update["provenance_class"] = st.src_class
    if pending.extra_tags:
        update["tags"] = [*record.tags, *(t for t in pending.extra_tags if t not in record.tags)]
    return record.model_copy(update=update)
