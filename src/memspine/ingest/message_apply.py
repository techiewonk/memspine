"""Per-turn apply loop for the message ingestion path."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from memspine.config import constants
from memspine.core.correction import detect_correction
from memspine.core.forget_request import is_forget_request
from memspine.core.reference_edges import REF_PREFIX, antecedent_kind
from memspine.ingest import role_wiring
from memspine.ingest.index_view import PENDING_INDEX_PREFIX
from memspine.ingest.message_helpers import (
    IDEM_TAG_PREFIX,
    INTAKE_LANDED,
    _index_view_prefixes,
    _looks_like_recall,
    _participants_mode,
    _reply_target,
    _speaker_of,
    _stamp_turn,
    _turn_content,
)
from memspine.ingest.stamping import PENDING_STAMP, ConversationStamper, participants_of_call
from memspine.models.encoders.query.temporal_query import RECOMMENDATION_TAG, is_recommendation
from memspine.observability import inspect as insp
from memspine.observability import tap
from memspine.observability.logging import get_logger
from memspine.store.events import EventKind, MemoryEvent, fingerprint_payload
from memspine.store.records import MemoryRecord, SourceInfo
from memspine.store.scope import validate_namespace

_log = get_logger(__name__)


async def apply_turns(
    eng: Any,
    messages: Sequence[Mapping[str, Any]],
    namespace: str,
    actor: str,
    session_id: str | None,
    channel: str,
    group_id: str | None,
    tags: list[str] | None,
    valid_from: datetime | None,
) -> list[MemoryRecord]:
    """The per-turn loop of :meth:`ingest_messages`: each turn goes through
    the write door on its own, with its own firewall and ladder decisions."""
    records: list[MemoryRecord] = []
    written_at: dict[int, str] = {}  # G27: message index -> its record id
    ref_cfg = eng._config().read  # A07
    prev_turn: tuple[str, str] | None = None  # (record id, speaker) of the last turn written
    fw = eng._config().firewall
    if eng._perspective_options().on:
        # I39: the call's participants are known before its first turn is annotated
        state = await eng._persp_state_for(validate_namespace(namespace))
        for turn in messages:
            if isinstance(turn, Mapping):
                who = turn.get("speaker") or turn.get("name")
                if isinstance(who, str) and who.strip():
                    state["known"].add(who.strip().lower())
    session_speakers = (  # I53: everyone present in this call, in order of first turn
        list(dict.fromkeys(_speaker_of(t) for t in messages if isinstance(t, Mapping)))
        if await _participants_mode(eng, namespace) == "session"
        else []
    )
    role_policy_v2 = eng._config().write.role_policy_v2  # W2-03
    conv_stamper = None
    stamp_config = eng._config().write
    if eng._config().write.stamping_v2:  # W2-02
        first_message = next(
            (
                t
                for t in messages
                if isinstance(t, Mapping) and isinstance(t.get("_message_envelope"), Mapping)
            ),
            None,
        )
        conv_stamper = ConversationStamper(
            namespace=validate_namespace(namespace),
            source_id=(first_message.get("session_id") if first_message else session_id)
            or "nosession",
            default_tz=stamp_config.stamp_default_tz or eng._config().read.naive_timezone,
            session_speakers=tuple(
                participants_of_call([t for t in messages if isinstance(t, Mapping)])
            ),
        )
    index_prefixes = _index_view_prefixes(eng, messages, valid_from)  # R14-INDEXVIEW ([] = off)
    active_stamper_key: tuple[str, str] | None = None
    for i, turn in enumerate(messages):
        envelope = turn.get("_message_envelope") if isinstance(turn, Mapping) else None
        effective_session = (
            str(turn.get("session_id") or session_id)
            if isinstance(envelope, Mapping)
            else session_id
        )
        if isinstance(envelope, Mapping) and isinstance(envelope.get("conversation_id"), str):
            current_conversation = envelope["conversation_id"]
            stamper_key = (effective_session or "nosession", current_conversation)
            if active_stamper_key is not None and stamper_key != active_stamper_key:
                prev_turn = None
                if conv_stamper is not None:
                    conv_stamper.clear_proposal()
                    conv_stamper = ConversationStamper(
                        namespace=validate_namespace(namespace),
                        source_id=effective_session or "nosession",
                        default_tz=stamp_config.stamp_default_tz
                        or eng._config().read.naive_timezone,
                        session_speakers=tuple(
                            participants_of_call([t for t in messages if isinstance(t, Mapping)])
                        ),
                    )
            active_stamper_key = stamper_key
        try:
            role = turn["role"]
            content = _turn_content(turn)
        except (KeyError, TypeError) as exc:
            raise ValueError(
                f"messages[{i}] must be a mapping with 'role' and 'content' keys"
            ) from exc
        reply_to = _reply_target(turn, i, written_at)
        role_dec = (
            role_wiring.decide(eng, validate_namespace(namespace), effective_session, role, content)
            if role_policy_v2
            else None
        )  # W2-03: raw keeps every role; derivation reads the tags
        if role in fw.skip_message_roles and role_dec is None:
            tap.event("write.turn.skipped", reason="role", role=role, turn=i)
            continue  # H21: system/tool text is not memory
        if fw.skip_injected_recall and role_dec is None and _looks_like_recall(content):
            # H21: recalled memory echoed back is not new evidence. R2-11:
            # never silently: a MARKER event traces what was dropped (by
            # fingerprint, not content) so a genuine turn that merely
            # quotes a marker can be found and re-ingested.
            _log.info("memory.skip_injected_recall", namespace=namespace)
            tap.event("write.turn.skipped", reason="injected_recall", turn=i)
            eng._require_started()
            await eng._append_and_project(
                MemoryEvent(
                    kind=EventKind.MARKER,
                    namespace=validate_namespace(namespace),
                    actor=actor,
                    payload={
                        "marker": "recall_skipped",
                        "role": role,
                        "session_id": effective_session,
                        "message_index": i,
                        "content_fingerprint": fingerprint_payload({"content": content}),
                        "chars": len(content),
                    },
                )
            )
            continue
        turn_tags = list(tags or [])
        if role_dec is not None:
            turn_tags.extend(role_dec.tags)
        if eng._perspective_options().on:  # I39: explicit participants ride as tags
            who = turn.get("speaker") or turn.get("name")
            to = turn.get("addressee")
            if isinstance(who, str) and who.strip():
                turn_tags.append(f"spk:{who.strip().lower()}")
            if isinstance(to, str) and to.strip():
                turn_tags.append(f"addr:{to.strip().lower()}")
        if fw.tag_assistant_claims and role == "assistant":
            turn_tags.append("assistant_claim")
        if role == "assistant" and eng._config().read.role_aware and is_recommendation(content):
            turn_tags.append(RECOMMENDATION_TAG)  # W11: the recommendation ledger
        if (
            role == "user"
            and eng._memory_policy(eng._config(), "episodic").get("forget_detector")
            and is_forget_request(content)
        ):
            # G25: tagged and listed by forget_requests(); never deleted on a regex.
            turn_tags.append(constants.FORGET_REQUEST_TAG)
        correction = (
            detect_correction(content)
            if role == "user"
            and eng._memory_policy(eng._config(), "episodic").get("correction_detector")
            else None
        )
        if correction is not None:
            turn_tags.append(constants.CORRECTION_TAG)  # W17d
        if "_idem" in turn:  # W2-01: the intake write id rides as a tag (restart recovery)
            turn_tags.append(f"{IDEM_TAG_PREFIX}{turn['_idem']}")
        asset_refs = eng._attachment_refs(turn, i, effective_session)
        for _ref, aid in asset_refs:
            turn_tags.append(f"asset:{aid}")  # E04: ingest.assets
        if ref_cfg.reference_edges == "on" and reply_to is None and prev_turn is not None:
            # A07: a short reply / deictic turn answers the turn right before it
            ref_kind = antecedent_kind(
                content,
                _speaker_of(turn),
                prev_turn[1],
                max_words=ref_cfg.reference_short_words,
            )
            if ref_kind is not None:
                reply_to = prev_turn[0]
                turn_tags.append(f"{REF_PREFIX}{ref_kind}")
        stamp = eng._event_time(turn.get("timestamp")) or valid_from
        if stamp is not None and effective_session and eng._config().read.session_sequence:
            stamp = eng._sequenced(namespace, effective_session, eng._localize(stamp))
        pending = None
        if conv_stamper is not None:  # W2-02: three clocks, axes, participants, provenance class
            pending = _stamp_turn(eng, conv_stamper, i, turn, role, content, bool(session_speakers))
        pending_token = PENDING_STAMP.set(pending) if pending is not None else None
        role_token = role_wiring.PENDING_ROLE.set(role_dec) if role_dec is not None else None
        index_token = (
            PENDING_INDEX_PREFIX.set(index_prefixes[i])
            if index_prefixes and index_prefixes[i]
            else None
        )
        try:
            if isinstance(turn.get("_message_envelope"), Mapping):
                meta = turn["_message_envelope"]
                if meta.get("created_at_generated") and eng._config().read.skip_defaulted_dates:
                    turn_tags.append(constants.UNDATED_TAG)
                turn_tags.extend(
                    f"{key}:{meta[key]}" for key in ("message_id", "session_id", "conversation_id")
                )
                sender = meta.get("sender") or {}
                if sender.get("id"):
                    turn_tags.extend(
                        [f"sender_id:{sender['id']}", f"spk:{str(sender['id']).lower()}"]
                    )
                if sender.get("kind"):
                    turn_tags.append(f"sender_kind:{sender['kind']}")
            record = await eng.write(
                content,
                namespace=namespace,
                memory_type="episodic",
                source=SourceInfo(
                    role=role,
                    channel=channel,
                    message_id=str(turn.get("session_id") or session_id)
                    if turn.get("session_id") or session_id
                    else None,
                    message=turn.get("_message_envelope"),
                ),
                actor=actor,
                group_id=group_id,
                tags=turn_tags or None,
                valid_from=stamp,
                reply_to=reply_to,
                participants=([_speaker_of(turn), *session_speakers] if session_speakers else None),
                visibility=(
                    str(turn["visibility"]) if session_speakers and "visibility" in turn else None
                ),
            )
        finally:
            if pending_token is not None:
                PENDING_STAMP.reset(pending_token)
            if role_token is not None:
                role_wiring.PENDING_ROLE.reset(role_token)
            if index_token is not None:
                PENDING_INDEX_PREFIX.reset(index_token)
        if conv_stamper is not None:
            conv_stamper.clear_proposal()
            if role == "assistant" and not record.quarantined:
                conv_stamper.note_assistant(record.record_id, content)
        if asset_refs:
            eng._register_assets(asset_refs, turn, i, effective_session, record.record_id)
        if correction is not None and not record.quarantined:
            await eng._apply_correction(record.namespace, record, correction, records)
        records.append(record)
        written_at[i] = record.record_id
        if insp.inspect_enabled():  # TI-1: message index -> rid (the pre-record events' join)
            insp.turn_bound(
                rid=record.record_id,
                index=i,
                session=effective_session,
                speaker=_speaker_of(turn),
                quarantined=bool(record.quarantined),
                wid=str(turn["_idem"]) if "_idem" in turn else None,
            )
        if "_idem" in turn:  # W2-01: tell the intake gateway which record this write id became
            sink = INTAKE_LANDED.get()
            if sink is not None:
                sink[str(turn["_idem"])] = record.record_id
        if not record.quarantined:
            prev_turn = (record.record_id, _speaker_of(turn))
    return records
