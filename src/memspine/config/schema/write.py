"""Config area: write (F-07 split of the former ``config/schema.py``)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WriteConfig(BaseModel):
    """Write-time governance tags (I48, I52, I53). Every key is off by default: a record is
    stored exactly as it was before. The tags are labels only (never the text)."""

    model_config = ConfigDict(extra="forbid")

    #: I52: grade each record none/low/medium/high with a category (``core/sensitivity.py``)
    #: and tag it ``sens:<grade>`` / ``sensc:<category>``. ``heuristic``: the fixed lexicon.
    #: ``decider``: the lexicon, raised by the decider ``sensitivity`` task when it is sure
    #: (``read.decider: opendecider``); the decider can raise a grade, never lower it.
    sensitivity: Literal["off", "heuristic", "decider"] = "off"
    #: I53: ``session``: ``write_messages`` tags each turn ``participant:<name>`` for every
    #: speaker of the call (the turn's own speaker first; a turn's ``name``/``speaker`` key,
    #: else its role), and a derived record (mined fact, summary) inherits the union of its
    #: parents' participants and the strictest visibility. ``viewer`` reads then filter.
    participants: Literal["off", "session"] = "off"
    #: I48: tag engine-derived facts ``src:inferred`` (mined / reflected / consolidated, or
    #: assistant-proposed with no user turn behind them), cap their trust at
    #: ``inferred_trust_cap`` and record the distinct supporting user turns as ``support:<id>``.
    #: A mined fact whose words are at least ``inferred_explicit_overlap`` contained in one user
    #: turn is that user's statement, not an inference, and is left alone.
    inferred: Literal["off", "on"] = "off"
    inferred_trust_cap: float = Field(default=0.4, ge=0.0, le=1.0)
    inferred_explicit_overlap: float = Field(default=0.6, gt=0.0, le=1.0)
    inferred_support_overlap: float = Field(default=0.3, gt=0.0, le=1.0)

    #: W1 (adaptive-window write, call 1; ``docs/ENGINE_COMPARISON_AND_WRITE_PATH_DESIGN.html``
    #: "Decided design"). ``off`` (default): no window call, a write is unchanged. ``facts``:
    #: one structured ``window_extract`` call per window yields atomic, dated, grounded facts
    #: (semantic layer, parents = the source turns). ``facts_graph`` adds the entity-edge
    #: section (handed to the sleep stage ``extract_graph``, the call-3 wiring); ``all`` adds
    #: the episode-fragment section. Needs an LLM role ``window_extract`` and semantic memory.
    window_extract: Literal["off", "facts", "facts_graph", "all"] = "off"
    #: A window closes at the first of: a topic shift, this many turns, ``window_max_tokens``
    #: (prompt tokens, 4 chars each), ``window_idle_s`` of silence, or the session's end.
    window_max_turns: int = Field(default=6, ge=1, le=64)
    window_max_tokens: int = Field(default=1500, ge=64)
    #: Topic shift: the similarity of two consecutive turns (their embeddings) falls below the
    #: store's running ``mean - k * sd`` of that similarity (per namespace, no LLM). k = 2 is
    #: the usual two-sigma outlier line, not tuned. 0 turns the topic boundary off.
    window_topic_k: float = Field(default=2.0, ge=0.0)
    #: Seconds without a write in a namespace before the open window is cut (async mode). 0: never.
    window_idle_s: float = Field(default=120.0, ge=0.0)
    #: ``auto``: split a window's call into facts+edges / episode after repeated parse failures;
    #: ``always`` / ``never``. Only matters when more than one section is enabled.
    window_split: Literal["auto", "always", "never"] = "auto"
    #: True (live): window calls run in a background task after the write returns (idle
    #: debounced). False (batch / eval): they run only at ``Engine.flush_windows()``,
    #: ``close_session`` or ``sleep``. The result is identical; only the timing differs.
    window_async: bool = True
    #: Cache each call by (model, prompt version, sections, window text hash), so a re-run on
    #: the same history is free (needs the engine cache; in memory unless a durable one is set).
    window_cache: bool = True

    #: W2-05 (LIB-14, W-11 "E + B"; needs ``window_extract``). ``True``: the window segmenter cuts
    #: at a source-time gap (``segment_gap_s``) and a calendar-day change, and grows adaptively (a
    #: window past ``window_max_turns`` keeps taking short turns until ``segment_grow_tokens`` or
    #: ``segment_max_turns``; ``window_max_tokens`` always binds); closed windows of one namespace
    #: wait until ``batch_max_tokens`` / ``batch_max_windows`` / ``batch_max_age_s`` and then share
    #: ONE extraction call, each window a separately numbered block (grounding stays per window; an
    #: item citing two windows is dropped). Episode sections still run per window. A crash loses
    #: nothing (with ``workers.outbox`` the windows are durable rows, the batch is rebuilt).
    #: Taps ``write.segment.*`` / ``write.batch.*``. ``False`` (default): the old segmenter and one
    #: call per window, unchanged.
    segmentation_v2: bool = False
    #: Seconds of source-time silence that cut a window (0: no gap cut). Needs ``segmentation_v2``.
    segment_gap_s: float = Field(default=1800.0, ge=0.0)
    #: Also cut when the source date changes (calendar day). Needs ``segmentation_v2``.
    segment_calendar_day_cut: bool = False
    #: A window past ``window_max_turns`` keeps growing until it holds this many tokens.
    segment_grow_tokens: int = Field(default=400, ge=1)
    #: Hard turn cap of a growing window (never below ``window_max_turns``).
    segment_max_turns: int = Field(default=24, ge=1, le=256)
    #: Token budget of one batched call (estimated tokens). Needs ``segmentation_v2``.
    batch_max_tokens: int = Field(default=3000, ge=64)
    #: Most windows in one call; 1 turns batching off.
    batch_max_windows: int = Field(default=8, ge=1, le=64)
    #: Longest a closed window waits for company before its batch goes out (seconds).
    batch_max_age_s: float = Field(default=30.0, ge=0.0)

    #: W2-06a (LIB-05; W-14, W-15, P-05, P-06; needs ``window_extract`` with ``facts``). ``True``:
    #: the ``facts`` section is extracted as ``ItemV1`` items (compact keys, each citing the
    #: numbered lines it came from, content only) and every item is validated on its own and
    #: compiled into a stored ``AssertionEnvelope`` (time axes from the turn stamping, provenance
    #: class and trust from the cited line's role, evidence roles support / binding / context). An
    #: invalid item (no citation, a line out of range, only CONTEXT lines cited, a reserved system
    #: name such as ``trust`` or ``user_id``) is dropped and its siblings kept. At most ONE repair
    #: re-ask per extraction call carries only the invalid items and their typed errors; an item
    #: still invalid goes to the persisted dead-letter list (reason code, attempts, item hash; no
    #: content unless the tap content opt-in is on). A new kind is accepted with a safe default
    #: (stored as ``event``, never superseding) until 64 kinds exist, then it becomes ``other``.
    #: Unknown fields are kept in the envelope extras. Taps ``write.item.valid`` /
    #: ``write.item.repaired`` / ``write.item.dead``. The repair call goes through the LLM router,
    #: so ``economy.ledger_v2`` counts it. ``edges`` / ``episode`` sections keep their own prompt.
    #: ``False`` (default): the old fact section.
    envelope_v2: bool = False

    #: W2-14 (LIB-01; W-04, M-10, R-07; needs ``envelope_v2``): temporal anchoring as an envelope stage
    #: (order 100). For each envelope it fills the event interval with its precision and basis by the
    #: anchor ladder: (1) an explicit date in the model's time text, the cited span or the assertion
    #: text, (2) a relative phrase ("last Tuesday", "letzte Woche", "il y a trois jours", "hace dos
    #: semanas") resolved against the cited turn's ``source_at`` (``stamping_v2``), else the window's
    #: nearest stamped turn, (3) with ``stamping_v2`` off only, the window ``valid_from`` as an
    #: approximate, ``inferred`` session anchor, (4) else unknown (never "now"). Precision is never
    #: finer than the evidence ("in 2019" is year precision, never a made-up day). ``before`` /
    #: ``after`` / ``during`` / ``since`` / ``until`` between two assertions of the window ("the week
    #: before her trip") are written as a typed relation with no date. The deposited ``happened:`` label
    #: carries the written precision. ``False`` (default): the model's time text is kept as text only.
    temporal_v2: bool = False

    #: W2-10 (LIB-02; W-21, W-22, M-12). ``True`` (needs ``write.envelope_v2``): an envelope stage at
    #: order 400 finds the live assertions on each state envelope's (subject, attribute) key (W2-09's
    #: resolved entity id when present, else the normalised name) and decides the integration operator
    #: through the conflict ladder: cardinality, then trust tier (the default is above low and an equal
    #: tier supersedes), then time (a closed past interval coexists), then negation binding (a bound
    #: negation retires). The verdict (append / supersede / merge / coexist / contest / defer_kc, with
    #: rung and hint) rides the envelope and the deposit applies it: supersede retires the old value as
    #: history, coexist and contest keep both (a contest is tagged ``disputed``). An undeclared
    #: attribute with a conflicting live value and no hint that the value changed is deferred to K_C
    #: (``offline.reconcile_v2``) and counted (``integrate_defer_kc``). Events and unkeyed facts coexist.
    #: Taps ``write.integrate.*``. ``False`` (default): the old write door decides alone, unchanged.
    integration_v2: bool = False

    #: W2-10: operator-declared attribute cardinality for ``write.integration_v2``, attribute name ->
    #: ``one`` (single-valued: a newer value supersedes) or ``many`` (values coexist). An attribute
    #: not listed is undeclared (M-12 / W-22: coexist, or deferred to K_C when nothing says it changed).
    integration_cardinality: dict[str, Literal["one", "many"]] = Field(default_factory=dict)

    #: W2-10: with ``write.integration_v2``, an undeclared key's unhinted conflict is deferred to K_C
    #: (``False``, default); ``True`` lets the ladder decide it at once (undeclared means many: coexist).
    integration_decide_unhinted: bool = False

    #: W2-08 (W-17, W-08; LIB-03): grounding stage over the compiled envelopes (needs ``envelope_v2``;
    #: envelope-stage order 200). Per assertion it locates the supporting span in the cited lines and
    #: writes the offsets onto the evidence, runs the rule checks (cited turns exist; number, unit,
    #: date and name appear in the span; negation and modality preserved; the speaker matches the
    #: asserter), settles what the rules cannot decide with the adjudicator, and promotes an
    #: assistant proposal to ``user_confirmed`` only on a real user yes on the next line. An ungrounded
    #: assertion is dropped or marked tentative (``ground_action``) with a reason code counted in the
    #: window stats (``ground.dropped.<code>``). Taps ``write.ground.*``. ``False`` (default): the
    #: compiled envelopes are deposited as they are.
    grounding_v2: bool = False
    #: W2-08: what happens to an assertion the checks find unsupported or only partly supported:
    #: ``drop`` it, or keep it ``tentative`` (never promoted, never superseding).
    ground_action: Literal["drop", "tentative"] = "drop"
    #: W2-08: who settles the assertions the rules leave ``undecided``. ``lexical``: word overlap
    #: (accepts at high coverage, rejects only same-language text with almost none). ``decider``:
    #: the OpenDecider ``grounded`` task first (``read.decider``, CPU unless set otherwise), lexical
    #: when it is unsure or unavailable. ``off``: stays ``uncertain`` (kept).
    ground_adjudicator: Literal["lexical", "decider", "off"] = "lexical"

    #: W2-06b (LIB-05; W-16, P-04, P-09, P-11, G-W49; needs ``envelope_v2``). ``True``: ONE model call
    #: per window (or per batch of windows) fills every requested section (facts, edges, episode)
    #: under one schema, instead of the facts call plus a second call for edges / episode. The prompt
    #: has three parts: NEW (the numbered lines to extract from), CONTEXT (the last turns of the
    #: previous window, to resolve pronouns; never cited) and KNOWN (entities already in the store
    #: with their aliases, rendered compactly, capped and deduplicated; hooks for the P-23 pre-pass
    #: and the online resolver). The JSON schema is passed as ``response_format`` to providers that
    #: declare support (OpenAI-compatible, Ollama, llama.cpp), per provider, never required; any
    #: other provider is answered by the W2-06a validator plus its one repair. The same-language and
    #: coarse-date partials still apply. Taps ``write.prompt.v2`` (sections, known_count,
    #: schema_mode). ``False`` (default): unchanged.
    prompt_v2: bool = False
    #: W2-06b: most KNOWN entities listed in one prompt (after dedup; entities named in the NEW lines
    #: first). Needs ``prompt_v2``.
    prompt_known_cap: int = Field(default=40, ge=0)
    #: W2-06b: turns of the previous window shown as CONTEXT. Needs ``prompt_v2``. ``0``: no CONTEXT.
    prompt_context_turns: int = Field(default=3, ge=0)
    #: W2-06b: ``auto`` passes the JSON schema to providers that support it; ``off`` never does.
    #: Needs ``prompt_v2``.
    prompt_schema: Literal["auto", "off"] = "auto"

    #: F-16a (M-09, G-08, POL-2): the lineage table and the single derived-write door. ``True``:
    #: every derived record written through ``derive/write.py`` gets one lineage edge per parent
    #: (``support`` unless the site names a role), its trust capped at the least-trusted parent, a
    #: typed ``layer`` and ``fingerprint.derivation`` (F-02a), and is refused when it has no parent
    #: at all. ``False`` (default): the old write path exactly, and the table is never written.
    lineage_table: bool = False

    #: W2-01 (LIB-30; W-01, W-02, W-03, W-24): one intake gateway in front of ``write_messages``,
    #: ``write_episode`` and ``ingest``. ``True``: every call becomes a ``SourceEnvelope``; each segment
    #: gets an S3-3 write id (scope, source, version, position, content), so a retry (a re-sent batch,
    #: a crash and a re-send) writes nothing new; the call returns after a durable ledger commit and the
    #: old write door runs on the fresh segments only; an interrupted apply is finished at the next
    #: ``start()``. A re-ingested document with edited content becomes a new source version: unchanged
    #: chunks stay, changed ones are archived with the facts derived only from them (derived items that
    #: keep another live parent are re-pointed). The receipt carries readiness per capability and
    #: never claims one the engine does not run (``raw_search`` is ready at return; ``facts`` /
    #: ``graph`` / ``summaries`` follow the window outbox barrier). Taps ``write.intake.*``. ``False``
    #: (default): the old write path, unchanged.
    intake_v2: bool = False

    #: W2-11 (W-23): the asset describer. ``off`` (default, the BD-8 hold): ``ingest_asset`` is refused
    #: and nothing else changes. ``ingest``: ``Engine.ingest_asset`` stores the asset reference (content
    #: hash + path/URI, never the bytes) as a record and, right away, a description as a derived record
    #: (parent: the asset record, trust capped, through ``derive/write.py``). ``on_read``: the reference
    #: is stored without a description; ``Engine.search`` describes a returned asset record that has none,
    #: once (the description is a record, so it is cached), waiting at most ``asset_describe_timeout_s``.
    #: The describer is the optional LLM role ``describe_asset`` (vision-capable model, a MD-01 tier);
    #: with none bound it writes a metadata-only description (filename, mime, size, alt text, EXIF) and
    #: makes no model call. Needs ``write.intake_v2``. Taps ``write.asset.*``.
    asset_describer: Literal["off", "ingest", "on_read"] = "off"
    #: W2-11: longest an ``on_read`` read waits for descriptions; past it the read returns without them
    #: and the work continues in the background (queued, one task per asset).
    asset_describe_timeout_s: float = Field(default=3.0, gt=0)
    #: W2-02 (LIB-29; W-04, W-05, W-08, M-08): stamping on every raw turn. ``True``: three
    #: clocks (``source_at`` from the turn's own timestamp and never "now", ``source_tz``
    #: declared by the turn or envelope else ``unknown``, ``recorded_at`` the engine clock),
    #: a naive time with an unknown zone kept as wall clock flagged ``tz_unknown`` (not
    #: shifted by ``read.naive_timezone``), the speaker / addressee / participants typed
    #: columns (participants on by default, filled from the call's speakers), and the
    #: provenance class from the role. A user turn that answers an assistant turn in the
    #: same call is run through the assent check (a real yes is tagged
    #: ``assent_of:<assistant record>``, "ok, thanks" is not). Runs on the direct path and
    #: inside the intake gateway (``write.intake_v2``). The event interval and its precision
    #: are filled at derivation, not here. Taps ``write.stamp.*``. ``False`` (default):
    #: turns are stamped as before.
    stamping_v2: bool = False
    #: W2-02 (W-05): with ``stamping_v2``, also write the v1 ``participant:<name>`` tags
    #: (``v1``, kept for the dual-read period; ``none`` writes only the typed column). They
    #: feed the ``viewer`` read filter, so a deployment that reads with ``viewer=`` may set
    #: ``none`` and rely on the typed column.
    stamp_participant_tags: Literal["v1", "none"] = "v1"
    #: W2-02 (W-04): the zone a naive source time with no declared zone is read in (as wall
    #: clock; the record is still flagged ``unknown``). ``None``: ``read.naive_timezone``.
    stamp_default_tz: str | None = None

    #: W2-03 (LIB-13; W-06, W-09, I-8): role policy, echo guard and routing. ``True``: raw storage
    #: keeps every role (a system or tool turn is written, tagged ``derive_use:excluded`` /
    #: ``evidence``, instead of skipped); derivation (window extraction, session-close input) drops
    #: system turns and echoes and treats assistant text as a proposal (class
    #: ``assistant_proposed``, trust never above user-stated) and tool text as evidence. A turn
    #: that carries a recall marker (the engine's own wrappers or ``wrap_recall``) or overlaps the
    #: session's last read above ``echo_threshold`` is tagged ``echo`` / ``echo_of:<id>`` and is
    #: never re-derived as a new fact; a user restatement is an ``assent_candidate``, not support.
    #: With ``write.intake_v2``, a turn that carries ``entity`` + ``attribute`` (or ``memory_type``
    #: skill / plan) is also written as a typed record with the raw turn as its parent; unkeyed
    #: turns stay raw. Taps ``write.role.*``, ``write.echo.*``, ``write.route.*``. ``False``
    #: (default): unchanged.
    role_policy_v2: bool = False
    #: W2-03: with ``role_policy_v2``, the shingle overlap (max of Jaccard and containment of the
    #: memory in the turn) at or above which a turn counts as an echo of a memory the last read
    #: returned.
    echo_threshold: float = Field(default=0.6, gt=0.0, le=1.0)

    #: O4-11 (LIB-44, O-25): explicit feedback only. ``True``: ``Engine.feedback`` refuses any
    #: channel that is not a deliberate one (``user_message``, ``ui_control``, ``operator_edit``,
    #: ``api``; silence, dwell, regeneration and classifier signals are refused), checks who may
    #: give it (the memory owner, or a name in ``feedback_operators`` for corrections,
    #: rejections and ratings; an agent never), and adds the kinds ``correction`` /
    #: ``rejection`` / ``confirmation`` (each becomes a bound assertion that goes through the
    #: write door and the conflict ladder, never an edit of the record) and ``rating`` (logged,
    #: no assertion). Also enables ``Engine.correction_latency``. ``False`` (default): the
    #: like / dislike / note verb exactly as before.
    feedback_v2: bool = False
    #: O4-11: principals allowed to give operator feedback under ``feedback_v2`` (never a
    #: confirmation).
    feedback_operators: list[str] = Field(default_factory=list)


class IngestConfig(BaseModel):
    """E04 attachment handling at write time. Off by default: a message's ``attachments`` are
    ignored exactly as before. On: each attachment's identity (source turn, original URI,
    source caption, availability) is registered and the record is tagged ``asset:<id>``;
    nothing is downloaded at ingest."""

    model_config = ConfigDict(extra="forbid")

    assets: Literal["off", "on"] = "off"
    #: Cache directory (gitignored in the eval harness): ``registry.sqlite`` and the
    #: downloaded bytes under ``blobs/`` by content hash. None: registry in memory, no blobs
    #: (so ``read.asset_evidence: fetch`` is refused at start).
    asset_dir: str | None = None
    asset_max_bytes: int = Field(default=5_000_000, ge=1)
    asset_timeout_s: float = Field(default=15.0, gt=0.0)
    asset_allowed_mime: list[str] = Field(
        default_factory=lambda: ["image/jpeg", "image/png", "image/gif", "image/webp"]
    )
    #: ``none``: no description is produced (text-only). ``ollama``: a local vision model.
    asset_vision: Literal["none", "ollama"] = "none"
    asset_vision_model: str = "qwen2.5vl:3b"
    asset_vision_url: str = "http://localhost:11434"
    asset_vision_timeout_s: float = Field(default=120.0, gt=0.0)


class DataShapeConfig(BaseModel):
    """I25: facts about the data the engine will serve, declared by the data adapter (or
    the deployer). Only ``data_profile: auto`` reads them; an undeclared fact (None /
    ``unknown``) selects no preset."""

    model_config = ConfigDict(extra="forbid")

    has_timestamps: bool | None = None
    has_question_date: bool | None = None
    speaker_kind: Literal["named", "user_assistant", "single_author", "unknown"] = "unknown"
    turn_length: Literal["short", "medium", "long", "unknown"] = "unknown"
    history_size: Literal["small", "medium", "large", "unknown"] = "unknown"
    language: str | None = None
    abstention_possible: bool | None = None
