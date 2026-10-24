"""Config area: write (F-07 split of the former ``config/schema.py``)."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field

from memspine.config.schema.base import ConfigModel


class WriteConfig(ConfigModel):
    """Write-time governance tags (I48, I52, I53). Every key is off by default: a record is
    stored exactly as it was before. The tags are labels only (never the text)."""

    model_config = ConfigDict(extra="forbid")

    #: I52: grade each record none/low/medium/high with a category (``core/sensitivity.py``)
    #: and tag it ``sens:<grade>`` / ``sensc:<category>``. ``heuristic``: the fixed lexicon.
    #: ``decider``: the lexicon, raised by the decider ``sensitivity`` task when it is sure
    #: (``model.decider.read``); the decider can raise a grade, never lower it.
    sensitivity: Literal["off", "heuristic", "decider"] = "off"
    #: I53: ``session``: ``ingest_messages`` tags each turn ``participant:<name>`` for every
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
    #: True (live): window calls run in a background task after the write returns (idle
    #: debounced). False (batch / eval): they run only at ``Engine.flush_windows()``,
    #: ``close_session`` or ``sleep``. The result is identical; only the timing differs.
    window_async: bool = True
    #: Cache each call by (model, prompt version, sections, window text hash), so a re-run on
    #: the same history is free (needs the engine cache; in memory unless a durable one is set).
    window_cache: bool = True

    #: Seconds of source-time silence that cut a window (0: no gap cut). Needs ``segmentation``.
    segment_gap_s: float = Field(default=1800.0, ge=0.0)
    #: Also cut when the source date changes (calendar day). Needs ``segmentation``.
    segment_calendar_day_cut: bool = False
    #: A window past ``window_max_turns`` keeps growing until it holds this many tokens.
    segment_grow_tokens: int = Field(default=400, ge=1)
    #: Hard turn cap of a growing window (never below ``window_max_turns``).
    segment_max_turns: int = Field(default=24, ge=1, le=256)
    #: Token budget of one batched call (estimated tokens). Needs ``segmentation``.
    batch_max_tokens: int = Field(default=3000, ge=64)
    #: Most windows in one call; 1 turns batching off.
    batch_max_windows: int = Field(default=8, ge=1, le=64)
    #: Longest a closed window waits for company before its batch goes out (seconds).
    batch_max_age_s: float = Field(default=30.0, ge=0.0)

    #: W2-14 (LIB-01; W-04, M-10, R-07; needs ``envelope``): temporal anchoring as an envelope stage
    #: (order 100). For each envelope it fills the event interval with its precision and basis by the
    #: anchor ladder: (1) an explicit date in the model's time text, the cited span or the assertion
    #: text, (2) a relative phrase ("last Tuesday", "letzte Woche", "il y a trois jours", "hace dos
    #: semanas") resolved against the cited turn's ``source_at`` (``stamping``), else the window's
    #: nearest stamped turn, (3) with ``stamping`` off only, the window ``valid_from`` as an
    #: approximate, ``inferred`` session anchor, (4) else unknown (never "now"). Precision is never
    #: finer than the evidence ("in 2019" is year precision, never a made-up day). ``before`` /
    #: ``after`` / ``during`` / ``since`` / ``until`` between two assertions of the window ("the week
    #: before her trip") are written as a typed relation with no date. The deposited ``happened:`` label
    #: carries the written precision. ``False`` (default): the model's time text is kept as text only.
    temporal: bool = False

    #: W2-10 (LIB-02; W-21, W-22, M-12). ``True`` (needs ``write.envelope``): an envelope stage at
    #: order 400 finds the live assertions on each state envelope's (subject, attribute) key (W2-09's
    #: resolved entity id when present, else the normalised name) and decides the integration operator
    #: through the conflict ladder: cardinality, then trust tier (the default is above low and an equal
    #: tier supersedes), then time (a closed past interval coexists), then negation binding (a bound
    #: negation retires). The verdict (append / supersede / merge / coexist / contest / defer_kc, with
    #: rung and hint) rides the envelope and the deposit applies it: supersede retires the old value as
    #: history, coexist and contest keep both (a contest is tagged ``disputed``). An undeclared
    #: attribute with a conflicting live value and no hint that the value changed is deferred to K_C
    #: (``offline.reconcile``) and counted (``integrate_defer_kc``). Events and unkeyed facts coexist.
    #: Taps ``write.integrate.*``. ``False`` (default): the old write door decides alone, unchanged.
    integration: bool = False

    #: W2-10: operator-declared attribute cardinality for ``write.integration``, attribute name ->
    #: ``one`` (single-valued: a newer value supersedes) or ``many`` (values coexist). An attribute
    #: not listed is undeclared (M-12 / W-22: coexist, or deferred to K_C when nothing says it changed).
    integration_cardinality: dict[str, Literal["one", "many"]] = Field(default_factory=dict)

    #: W2-10: with ``write.integration``, an undeclared key's unhinted conflict is deferred to K_C
    #: (``False``, default); ``True`` lets the ladder decide it at once (undeclared means many: coexist).
    integration_decide_unhinted: bool = False

    #: W2-08 (W-17, W-08; LIB-03): grounding stage over the compiled envelopes (needs ``envelope``;
    #: envelope-stage order 200). Per assertion it locates the supporting span in the cited lines and
    #: writes the offsets onto the evidence, runs the rule checks (cited turns exist; number, unit,
    #: date and name appear in the span; negation and modality preserved; the speaker matches the
    #: asserter), settles what the rules cannot decide with the adjudicator, and promotes an
    #: assistant proposal to ``user_confirmed`` only on a real user yes on the next line. An ungrounded
    #: assertion is dropped or marked tentative (``ground_action``) with a reason code counted in the
    #: window stats (``ground.dropped.<code>``). Taps ``write.ground.*``. ``False`` (default): the
    #: compiled envelopes are deposited as they are.
    grounding: bool = False
    #: W2-08: what happens to an assertion the checks find unsupported or only partly supported:
    #: ``drop`` it, or keep it ``tentative`` (never promoted, never superseding).
    ground_action: Literal["drop", "tentative"] = "drop"
    #: W2-08: who settles the assertions the rules leave ``undecided``. ``lexical``: word overlap
    #: (accepts at high coverage, rejects only same-language text with almost none). ``decider``:
    #: the OpenDecider ``grounded`` task first (``read.decider``, CPU unless set otherwise), lexical
    #: when it is unsure or unavailable. ``off``: stays ``uncertain`` (kept).
    ground_adjudicator: Literal["lexical", "decider", "off"] = "lexical"

    #: W2-15 (W-17, W-15; LIB-17): coverage check and ONE targeted re-extraction (needs ``envelope``;
    #: envelope-stage order 250, after grounding). Coverage is the share of the window's informative
    #: lines (greetings / filler, and system / echo turns per the W2-03 tags, excluded) cited as
    #: support by at least one kept assertion. Below ``coverage_threshold`` with an uncited
    #: informative line left, the stage makes at most one extra call on the ``window_extract`` LLM
    #: for those lines only (W2-06b prompt with KNOWN); the reply is validated, compiled and run
    #: through the earlier stages again, and an item is added only if it comes out grounded. The call
    #: is counted in the cost ledger (stage ``coverage_reextract``) and in the window notes
    #: (``coverage.*``). ``False`` (default): unchanged.
    coverage: bool = False
    #: W2-15: re-extract when coverage is below this share (0..1). Needs ``coverage``.
    coverage_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    #: W2-15: most uncited lines sent in the one targeted call. Needs ``coverage``.
    coverage_max_targets: int = Field(default=6, ge=1)
    #: W2-15: neighbouring lines shown as CONTEXT on each side of the targets. Needs ``coverage``.
    coverage_context_lines: int = Field(default=2, ge=0)

    #: W2-06b: most KNOWN entities listed in one prompt (after dedup; entities named in the NEW lines
    #: first). Needs ``prompt``.
    prompt_known_cap: int = Field(default=40, ge=0)
    #: W2-06b: turns of the previous window shown as CONTEXT. Needs ``prompt``. ``0``: no CONTEXT.
    prompt_context_turns: int = Field(default=3, ge=0)
    #: W2-06b: ``auto`` passes the JSON schema to providers that support it; ``off`` never does.
    #: Needs ``prompt``.
    prompt_schema: Literal["auto", "off"] = "auto"

    #: W2-06c (W-12): ``derive.OnlineExtractor`` is the single named entry for online extraction
    #: (turns in, envelopes out, deposited) over the outbox window stage. ``True``: the window stats
    #: carry the stage hook list (``online_extractor``) and the per-stage drop / rewrite counters
    #: (``envelope_stages``), and a window mode with no LLM role configured (the no-model ``core``
    #: template) skips extraction with a warning instead of failing at start, so ingestion makes zero
    #: model calls. ``False`` (default): the stats are unchanged and that start fails (D-10).
    online_extractor: bool = False

    #: W2-13 (P-23; needs ``window_extract``). A small local pre-pass over each window's raw turns,
    #: as hints, before the extraction call. ``rules``: a regex baseline (capitalised names, dates,
    #: numbers, aliases learned from deposited envelopes). ``gliner``: the rules plus GLiNER2
    #: entities (local CPU, ``[ner]``; the rules alone when gliner2 is absent). Three uses: (1) hint
    #: lines for the KNOWN / NEW prompt context (``derive.prepass.prepass_hints``), (2) a window whose
    #: every turn is greeting / thanks / acknowledgement filler and in which nothing was seen skips
    #: the extraction call (not when the ``episode`` section is on; counted as
    #: ``windows_prepass_skipped``), (3) with ``envelope`` an order-500 stage flags envelopes whose
    #: proper-name entity the pre-pass never saw (``extras.prepass_unseen``); it never adds or drops
    #: a fact. Tap ``write.prepass.*``. ``off`` (default): unchanged.
    prepass: Literal["off", "rules", "gliner"] = "off"

    #: FU-GAPS (needs ``write.envelope``): persist the in-memory learned registries as MARKER
    #: events and reload them at start, so a restart does not forget them: the W2-06a open kind set
    #: (kinds the model coined, up to the cap) and, with ``write.prepass``, the W2-13 learned
    #: aliases. Markers ``learned.kind`` and ``learned.alias``. ``False`` (default): the registries
    #: live in memory only, unchanged.
    learned_persist: bool = False

    #: F-16a (M-09, G-08, POL-2): the lineage table and the single derived-write door. ``True``:
    #: every derived record written through ``derive/write.py`` gets one lineage edge per parent
    #: (``support`` unless the site names a role), its trust capped at the least-trusted parent, a
    #: typed ``layer`` and ``fingerprint.derivation`` (F-02a), and is refused when it has no parent
    #: at all. ``False`` (default): the old write path exactly, and the table is never written.
    lineage_table: bool = False

    #: W2-11 (W-23): the asset describer. ``off`` (default, the BD-8 hold): ``ingest_asset`` is refused
    #: and nothing else changes. ``ingest``: ``Engine.ingest_asset`` stores the asset reference (content
    #: hash + path/URI, never the bytes) as a record and, right away, a description as a derived record
    #: (parent: the asset record, trust capped, through ``derive/write.py``). ``on_read``: the reference
    #: is stored without a description; ``Engine.search`` describes a returned asset record that has none,
    #: once (the description is a record, so it is cached), waiting at most ``asset_describe_timeout_s``.
    #: The describer is the optional LLM role ``describe_asset`` (vision-capable model, a MD-01 tier);
    #: with none bound it writes a metadata-only description (filename, mime, size, alt text, EXIF) and
    #: makes no model call. Taps ``write.asset.*``.
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
    #: through the canonical intake gateway. The event interval and its precision
    #: are filled at derivation, not here. Taps ``write.stamp.*``. ``False`` (default):
    #: turns are stamped as before.
    stamping: bool = False
    #: W2-02 (W-05): with ``stamping``, also write the v1 ``participant:<name>`` tags
    #: (``v1``, kept for the dual-read period; ``none`` writes only the typed column). They
    #: feed the ``viewer`` read filter, so a deployment that reads with ``viewer=`` may set
    #: ``none`` and rely on the typed column.
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
    #: A turn that carries ``entity`` + ``attribute`` (or ``memory_type``
    #: skill / plan) is also written as a typed record with the raw turn as its parent; unkeyed
    #: turns stay raw. Taps ``write.role.*``, ``write.echo.*``, ``write.route.*``. ``False``
    #: (default): unchanged.
    role_policy: bool = False
    #: W2-03: with ``role_policy``, the shingle overlap (max of Jaccard and containment of the
    #: memory in the turn) at or above which a turn counts as an echo of a memory the last read
    #: returned.
    echo_threshold: float = Field(default=0.6, gt=0.0, le=1.0)

    #: R14-INDEXVIEW (OBS-02/03/09/11, R14-M-4, R14-M-5): an index text per turn record, apart from
    #: its display text. ``True``: the embedder and the lexical index see the turn's text behind a
    #: deterministic prefix (no model call): the speaker's name, the turn's own absolute date with
    #: month and weekday words, each relative-date expression resolved to a calendar date from the
    #: turn's own timestamp (never the system clock; an undated turn gets no date words), the
    #: participants' names behind a bare we / they / you, and, when the turn is elliptical (rule
    #: score at least ``index_view_elliptical_threshold``: short, starts with a pronoun or assent
    #: word, no content noun), the content words of the previous turn of the call. The record, its
    #: id, order and display text are unchanged; the text rides in the WRITE event payload
    #: (``index_text``), so no schema change; a rebuild re-embeds the same text. Needs a re-ingest.
    #: ``False`` (default): the index sees the content only, event payloads byte-identical.
    index_view: bool = False
    #: R14-INDEXVIEW: most extra whitespace tokens the prefix may add to a turn (previous-turn words
    #: are cut first).
    index_view_max_extra_tokens: int = Field(default=48, ge=1, le=512)
    #: R14-INDEXVIEW: a turn of at most this many words meets the "short" rule of the gate.
    index_view_elliptical_max_words: int = Field(default=8, ge=1, le=64)
    #: R14-INDEXVIEW: rules (of 3: short, pronoun/assent start, no content noun) a turn must meet to
    #: be treated as elliptical.
    index_view_elliptical_threshold: int = Field(default=2, ge=1, le=3)
    #: R14-IVFIX: which turns inherit the previous turn's words. ``"anaphoric"`` (default): a
    #: phatic turn (greeting, thanks, laughter, emoji, pleasantry) never does, and a turn must point
    #: back (pronoun / deictic / assent word in its first three words), be short and have no content
    #: word of its own. ``"elliptical"``: the R14-INDEXVIEW rule (shortness alone can trigger it),
    #: kept for comparability with screen S69.
    index_view_carry: Literal["anaphoric", "elliptical"] = "anaphoric"

    #: O4-11: principals allowed to give operator feedback under ``feedback`` (never a
    #: confirmation).
    feedback_operators: list[str] = Field(default_factory=list)

    #: W2-09 (LIB-04; W-19, M-15; needs ``write.envelope``): online entity resolution, an envelope
    #: stage (order 300). ``True``: the subject and object of every compiled assertion are resolved
    #: against the entities of their own scope (tenant + owner), with no model call: exact name, alias,
    #: nickname and spelling rules first, then name-embedding similarity. At or above
    #: ``resolve_high`` the canonical entity id is attached; between ``resolve_grey`` and
    #: ``resolve_high`` (or on a doubtful rule verdict) a fresh entity is created, marked
    #: ``pending_merge`` with its candidates, and NOT merged (the sleep reconciliation decides);
    #: below that a new entity is created. Homonyms (two people called Noor in different places) stay
    #: apart. Entities, merges and splits are logged as ``memory.marker`` events (``entity.*``), so a
    #: replay through migrate rebuilds them and a merge is undone by one SPLIT. The resolved ids are
    #: on ``subject.entity_id`` / ``object.entity_id`` and ``extras["resolve"]`` for W2-10. Keys are
    #: tenant + owner with ``isolation.scope_keys``, the namespace otherwise. Taps
    #: ``write.resolve.*``. ``False`` (default): no resolution, mentions keep ``entity_id=None``.
    resolver: bool = False
    #: W2-09: embedding cosine at or above which a mention is the same entity (calibrated on the
    #: synthetic homonym/alias set in ``derive/resolve_calibration.py``).
    resolve_high: float = Field(default=0.9, gt=0.0, le=1.0)
    #: W2-09: embedding cosine at or above which (and below ``resolve_high``) a mention is a
    #: ``pending_merge`` candidate.
    resolve_grey: float = Field(default=0.7, gt=0.0, le=1.0)
    #: W2-09: bound of the name-vector cache and of the rule resolver's decision cache.
    resolve_cache: int = Field(default=1024, ge=1)


class IngestConfig(ConfigModel):
    """E04 attachment handling at write time. Off by default: a message's ``attachments`` are
    ignored exactly as before. On: each attachment's identity (source turn, original URI,
    source caption, availability) is registered and the record is tagged ``asset:<id>``;
    nothing is downloaded at ingest. RM-113 item 18: the registry rows are a table of the
    records database (``storage.records``) and the downloaded bytes go to
    ``storage.documents`` by content hash (``read.asset_evidence: fetch`` needs that slot)."""

    model_config = ConfigDict(extra="forbid")

    assets: Literal["off", "on"] = "off"
    asset_max_bytes: int = Field(default=5_000_000, ge=1)
    asset_timeout_s: float = Field(default=15.0, gt=0.0)
    asset_allowed_mime: list[str] = Field(
        default_factory=lambda: ["image/jpeg", "image/png", "image/gif", "image/webp"]
    )
    #: The vision model that describes an asset is ``model.vision`` (ADR-082).


class DataShapeConfig(ConfigModel):
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
