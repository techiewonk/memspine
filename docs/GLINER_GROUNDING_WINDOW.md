# GLiNER mentions in window writes

GLiNER output is stored as source-bound entity annotation, not as a fact. With `write.prepass: gliner`, the window pre-pass asks the configured GLiNER profile for entity spans over each original turn. The parser accepts only exact text and character offsets (or a legacy surface that occurs exactly once); a malformed or ambiguous response drops that turn's entire model result. Offsets are Unicode character offsets into the original `MemoryRecord.content`, not offsets into the normalized transcript shown to the window model.

After the ordinary envelope compiler has independently validated an extraction and its cited evidence, the pre-pass stage may attach `extras.gliner_mentions` for mentions found in the cited source turns. Each annotation carries the original record ID and version, source/content binding hashes, sender and message metadata from the record, exact span, label, optional model confidence, and `asserted: false`. The window model cannot author this field: window and session-close compilation reject it recursively, even with the pre-pass disabled. The system stage defensively removes any incoming value and rebuilds it from validated GLiNER spans. Durable reads validate the shared annotation type, namespace and cited source version. No mention is added to envelope evidence, used to validate a quote, or used to raise the envelope trust assessment. A mention can therefore describe text in a source turn without asserting that a compiled fact is true.

Annotations stay within one source record and one window. A mention from an uncited turn is not copied onto an envelope; batched windows are compiled and annotated independently. The pre-pass cache keys include the namespace plus the record IDs, versions, exact content, sender/message identity, role, channel, speaker and parents, so reuse of a record ID for changed source material cannot reuse stale offsets. Rule-only and disabled modes do not load GLiNER weights; inference stays behind the engine-owned bounded model pool and runs off the event loop.

Semantic ingestion uses the same span parser and source-binding format. These annotations do not create semantic entity keys, fact keys, or new evidence. Confidence remains unknown when the SDK does not provide it and is never treated as trust.

## Storage and limits

Semantic annotations live in `RecordFingerprint.entity_mentions` in the existing
record/event payload. Window annotations live in the stored assertion envelope.
Successful dedup merges retain the dropped record's annotations in the MERGE
snapshot; they are not unioned onto the kept record's different text. Retrieving
the kept record does not automatically aggregate all merged mentions.

The cache isolation checks cover model-result annotations and source snapshots.
The pre-existing learned-alias collection is Engine-wide; this change does not
claim complete namespace isolation for every alias/prompt hint. Mentions do not
establish relationships, resolve relative dates or supersede canonical evidence.
See [ADR-076](adr/ADR-076-grounded-gliner-mention-ingestion.md) and
[the real offline acceptance gate](GLINER_INGESTION_ACCEPTANCE.md).

The real gate exposed a distinct support-evidence issue in window compilation:
rendered speaker prefixes shifted persisted span offsets. `WindowLine.content`
now uses original record text for compilation and grounding; the displayed
transcript remains formatted. Evidence spans and GLiNER mention spans therefore
both refer to stored source content, including original Unicode/whitespace.
