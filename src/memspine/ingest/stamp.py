# ruff: noqa: E501
"""LIB-29: stamping for the insert path (W-04, W-05, W-08, M-08; insert step 3 and 4).

Pure and unwired (W2-02 wires it): no I/O, no clock read, no model call. ``recorded_at`` comes in as a
value from the caller's injected clock.

Three clocks (S6-3): source time (when it was said), recorded time (when it was stored) and the
client-declared zone. Event time comes later, from extraction. Rules:

* Source time is normalised to an aware ``datetime`` kept in its own zone (``source_at_utc`` is the same
  instant in UTC). Accepted: aware or naive ``datetime``, ISO 8601 strings (with ``Z`` or an offset, or
  a bare date), epoch seconds or milliseconds (number or digit string). An unparseable value gives
  ``source_at = None`` and the flag ``stamp_unparsed``; it never falls back to now (W-04).
* A zone is the segment zone, else the envelope zone, else unknown. A naive time with an unknown zone is
  read as wall clock in ``default_tz``, flagged ``tz_unknown`` with the note ``tz_assumed_default``
  and ``source_tz == "unknown"``; it is never shifted into another zone.
* DST edges: a wall time inside a spring-forward gap is flagged ``dst_gap`` (resolved with the offset
  before the transition); a repeated autumn hour is flagged ``dst_ambiguous`` (resolved to the first).
* Skew: a source time later than ``recorded_at`` plus the tolerance is flagged ``future_skew`` and kept.
  A source time earlier than the conversation's last source time is flagged ``out_of_order`` and kept
  (flag only; the integrate step decides what an older state means).
* Roles: speaker, addressee and participants are normalised to principal ids; provenance class, base trust
  and derive use come from the merged :class:`~memspine.ingest.roles.RolePolicy`.
* Write id (S3-3): ``hash(tenant, owner, conversation/session id, turn index or source time, content)``.
  Client keys are ignored; ``recorded_at`` is excluded, so a true retry collapses to the same id while the
  same words in a later session (or at another position) get a new one.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field

from memspine.ingest.roles import RolePolicy

__all__ = [
    "StampIn",
    "StampPolicy",
    "StampedTurn",
    "normalise_principal",
    "parse_source_time",
    "stamp",
    "write_id",
]

SrcClassName = Literal["user_stated", "assistant_proposed", "tool", "document", "external"]
Precision = Literal["day", "second", "unknown"]

_UNKNOWN = "unknown"
_PREFIX = re.compile(r"^\s*([^\W\d_][\w .'\-]{0,39}?)\s*:\s+\S")
_OFFSET = re.compile(r"^(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)
_DIGITS = re.compile(r"^-?\d+(?:\.\d+)?$")
_NON_NAMES = frozenset({"note", "http", "https", "re", "fwd", "fw", "ps", "nb", "todo", "warning"})


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------------------------- models


class StampPolicy(_Frozen):
    roles: RolePolicy = Field(default_factory=RolePolicy)
    future_skew_tolerance_s: float = Field(default=300.0, ge=0.0)
    out_of_order_tolerance_s: float = Field(default=0.0, ge=0.0)
    epoch_ms_threshold: float = Field(default=1e11, gt=0.0)  # |epoch| above this is milliseconds
    prefix_speaker_roles: tuple[str, ...] = ("user", "assistant", "document", "external")
    aliases: Mapping[str, str] = Field(default_factory=dict)  # normalised name -> principal id


class StampIn(_Frozen):
    """One segment plus the envelope facts stamping needs (a flat view of Segment and SourceEnvelope)."""

    tenant: str = Field(min_length=1)
    owner: str = Field(min_length=1)  # memory owner: a user or a shared group (S3-1)
    source_id: str = Field(min_length=1)  # conversation / session / document id
    seg_index: int | None = Field(default=None, ge=0)  # turn index within the source
    role: str
    content: str
    source_at: Any = None  # datetime | str | int | float | None
    tz: str | None = None  # segment zone
    envelope_tz: str | None = None
    speaker: str | None = None
    addressee: str | None = None
    participants: Sequence[str] | None = None  # envelope list
    session_speakers: Sequence[str] = ()  # speakers so far in this conversation
    last_source_at: datetime | None = None  # latest source time already seen in this conversation
    caller_id: str | None = None  # ignored by the write id (S3-3)


class StampedTurn(_Frozen):
    # the three clocks
    source_at: datetime | None
    source_at_utc: datetime | None
    source_tz: str
    tz_unknown: bool
    recorded_at: datetime
    precision: Precision
    # axes (M-08)
    spk: str | None
    addr: str | None
    participants: tuple[str, ...]
    participant_tags: tuple[str, ...]
    # provenance (W-08 stage 1)
    role: str
    src_class: SrcClassName
    base_trust: float
    derive_use: str
    family: str
    # identity (S3-3)
    write_id: str
    # diagnostics
    flags: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


# ------------------------------------------------------------------------------------------- principals


def normalise_principal(name: str | None, aliases: Mapping[str, str] | None = None) -> str | None:
    """NFKC, collapse whitespace, casefold; an alias map may rename; empty gives None."""
    if name is None:
        return None
    s = " ".join(unicodedata.normalize("NFKC", str(name)).split()).casefold()
    if not s:
        return None
    if aliases:
        return aliases.get(s, s)
    return s


# -------------------------------------------------------------------------------------------- zones


def _zone(name: str) -> tzinfo | None:
    n = name.strip()
    if not n:
        return None
    if n.upper() in {"Z", "UTC", "GMT", "UT"}:
        return UTC
    m = _OFFSET.match(n)
    if m:
        h, mi = int(m.group(2)), int(m.group(3) or 0)
        if h > 23 or mi > 59:
            return None
        delta = timedelta(hours=h, minutes=mi)
        return timezone(-delta if m.group(1) == "-" else delta)
    try:
        return ZoneInfo(n)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def _zone_label(name: str, z: tzinfo) -> str:
    if isinstance(z, ZoneInfo):
        return z.key
    off = z.utcoffset(None)
    if off is None or off == timedelta(0):
        return "UTC"
    total = int(off.total_seconds() // 60)
    sign = "+" if total >= 0 else "-"
    return f"{sign}{abs(total) // 60:02d}:{abs(total) % 60:02d}"


def _localise(naive: datetime, z: tzinfo, flags: list[str]) -> datetime:
    """Attach ``z`` to a naive wall time, flagging DST gaps and repeated hours."""
    first = naive.replace(tzinfo=z, fold=0)
    second = naive.replace(tzinfo=z, fold=1)
    back = first.astimezone(UTC).astimezone(z).replace(tzinfo=None)
    if back != naive:
        flags.append("dst_gap")  # wall time never happened; fold=0 keeps the pre-transition offset
    elif first.utcoffset() != second.utcoffset():
        flags.append("dst_ambiguous")  # repeated hour: fold=0 is the first occurrence
    return first


# --------------------------------------------------------------------------------------- source time


def parse_source_time(
    value: Any,
    tz_name: str | None,
    default_tz: str,
    *,
    epoch_ms_threshold: float = 1e11,
) -> tuple[datetime | None, str, bool, Precision, list[str], list[str]]:
    """Normalise one source time.

    Returns ``(source_at, source_tz, tz_unknown, precision, flags, notes)``. ``source_at`` is aware or None.
    """
    flags: list[str] = []
    notes: list[str] = []
    dflt = _zone(default_tz)
    if dflt is None:
        raise ValueError(f"default_tz {default_tz!r} is not a known zone")
    declared: tzinfo | None = None
    if tz_name is not None and tz_name.strip():
        declared = _zone(tz_name)
        if declared is None:
            flags.append("tz_invalid")
            notes.append(f"declared zone {tz_name!r} is not recognised")

    if value is None or (isinstance(value, str) and not value.strip()):
        label = _zone_label(tz_name, declared) if declared is not None and tz_name else _UNKNOWN
        return None, label, False, "unknown", flags, notes
    precision: Precision = "second"
    dt: datetime | None = None
    is_epoch = False
    if isinstance(value, bool):
        dt = None
    elif isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
        precision = "day"
    elif isinstance(value, int | float) or (
        isinstance(value, str) and _DIGITS.match(value.strip())
    ):
        x = float(value)
        if x != x or x in (float("inf"), float("-inf")):
            dt = None
        else:
            if abs(x) >= epoch_ms_threshold:
                x /= 1000.0
                notes.append("epoch_ms")
            try:
                dt = datetime.fromtimestamp(x, UTC)
                is_epoch = True
            except (OverflowError, OSError, ValueError):
                dt = None
    elif isinstance(value, str):
        s = value.strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            precision = "day"
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00") if s.endswith(("Z", "z")) else s)
        except ValueError:
            dt = None
    if dt is None:
        flags.append("stamp_unparsed")
        label = _zone_label(tz_name, declared) if declared is not None and tz_name else _UNKNOWN
        return None, label, False, "unknown", flags, notes

    if dt.tzinfo is not None and dt.utcoffset() is not None:
        # the instant is known; the zone label is the declared name, else the carried offset
        if declared is not None and tz_name:
            return (
                dt.astimezone(declared),
                _zone_label(tz_name, declared),
                False,
                precision,
                flags,
                notes,
            )
        if is_epoch:  # the instant is exact but nobody declared a zone
            flags.append("tz_unknown")
            notes.append("epoch_zone_unknown")
            return dt, _UNKNOWN, True, precision, flags, notes
        return dt, _zone_label("", dt.tzinfo), False, precision, flags, notes
    if declared is not None and tz_name:
        return (
            _localise(dt, declared, flags),
            _zone_label(tz_name, declared),
            False,
            precision,
            flags,
            notes,
        )
    # naive time, zone unknown: wall clock in the default zone, flagged, never shifted
    flags.append("tz_unknown")
    notes.append("tz_assumed_default")
    return _localise(dt, dflt, flags), _UNKNOWN, True, precision, flags, notes


# ------------------------------------------------------------------------------------------- write id


def write_id(
    tenant: str,
    owner: str,
    source_id: str,
    position: int | str | None,
    content: str,
) -> str:
    """S3-3: hash of scope, conversation/session id, position (turn index or source time) and content."""
    parts = [tenant, owner, source_id, "" if position is None else str(position), content]
    h = hashlib.blake2b(digest_size=16)
    for p in parts:
        b = p.encode("utf-8", "surrogatepass")
        h.update(len(b).to_bytes(8, "big"))
        h.update(b)
    return "w_" + h.hexdigest()


# ----------------------------------------------------------------------------------------------- stamp


def _family(role: str, spk: str | None, source_id: str, owner: str) -> str:
    """S4-2: the asserting principal; a document without a known author is the document itself."""
    if role in ("user", "operator"):
        return f"person:{spk or owner}"
    if role == "assistant":
        return f"agent:{spk or 'assistant'}"
    if role in ("tool", "system"):
        return f"tool:{spk or source_id}"
    if spk:
        return f"{role}:{spk}"
    return f"{role}:{source_id}"


def stamp(
    envelope_in: StampIn,
    *,
    recorded_at: datetime,
    default_tz: str = "UTC",
    policy: StampPolicy | None = None,
) -> StampedTurn:
    """Stamp one segment. Deterministic for fixed arguments; ``recorded_at`` is the injected clock value."""
    pol = policy or StampPolicy()
    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise ValueError("recorded_at must be timezone-aware")
    rule = pol.roles.rule(envelope_in.role)  # raises on an unknown role

    zone_name = (
        envelope_in.tz if envelope_in.tz and envelope_in.tz.strip() else envelope_in.envelope_tz
    )
    src_at, src_tz, tz_unknown, precision, flags, notes = parse_source_time(
        envelope_in.source_at,
        zone_name,
        default_tz,
        epoch_ms_threshold=pol.epoch_ms_threshold,
    )
    src_utc = src_at.astimezone(UTC) if src_at is not None else None
    if src_utc is not None:
        if src_utc > recorded_at.astimezone(UTC) + timedelta(seconds=pol.future_skew_tolerance_s):
            flags.append("future_skew")
        last = envelope_in.last_source_at
        if last is not None:
            last_utc = last.astimezone(UTC) if last.tzinfo is not None else last.replace(tzinfo=UTC)
            if src_utc < last_utc - timedelta(seconds=pol.out_of_order_tolerance_s):
                flags.append("out_of_order")

    # axes
    aliases = pol.aliases
    spk = normalise_principal(envelope_in.speaker, aliases)
    content = envelope_in.content
    if spk is None and envelope_in.role in pol.prefix_speaker_roles:
        m = _PREFIX.match(content)
        if m and m.group(1).strip().casefold() not in _NON_NAMES:
            spk = normalise_principal(m.group(1), aliases)
            notes.append("speaker_from_prefix")
    addr = normalise_principal(envelope_in.addressee, aliases)
    if envelope_in.participants is not None:
        base = list(envelope_in.participants)
    else:
        base = list(envelope_in.session_speakers)
    people: list[str] = []
    for raw in (*base, spk, addr):
        p = normalise_principal(raw, aliases)
        if p is not None and p not in people:
            people.append(p)
    # the speaker and addressee are always present; the rest keep their listed order
    participants = tuple(people)

    pos: int | str | None = envelope_in.seg_index
    if pos is None and src_utc is not None:
        pos = src_utc.isoformat()
    if pos is None:
        flags.append("write_id_no_position")
    wid = write_id(envelope_in.tenant, envelope_in.owner, envelope_in.source_id, pos, content)

    return StampedTurn(
        source_at=src_at,
        source_at_utc=src_utc,
        source_tz=src_tz,
        tz_unknown=tz_unknown,
        recorded_at=recorded_at,
        precision=precision,
        spk=spk,
        addr=addr,
        participants=participants,
        participant_tags=tuple(f"participant:{p}" for p in participants),
        role=envelope_in.role,
        src_class=rule.src_class,
        base_trust=rule.base_trust,
        derive_use=rule.derive_use,
        family=_family(envelope_in.role, spk, envelope_in.source_id, envelope_in.owner),
        write_id=wid,
        flags=tuple(dict.fromkeys(flags)),
        notes=tuple(dict.fromkeys(notes)),
    )
