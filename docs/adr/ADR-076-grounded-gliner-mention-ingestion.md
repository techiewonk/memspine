# ADR-076: Source-bound GLiNER mentions through canonical ingestion

**Status:** Accepted
**Date:** 2026-10-18
**Deciders:** Owner selected PA-119 and PA-121 implementation and focused checks

## Context

The semantic GLiNER adapter discards offsets and confidence, stringifies SDK
objects, and returns a synthetic `attribute=mention` fact. The semantic door
then keys the whole record to the first mention, allowing mention cooccurrence
to enter the fact conflict ladder. The window prepass similarly drops SDK span
metadata and finds the first surface occurrence. These are not sufficient
grounding or attribution guarantees.

## Decision

Parse SDK entity outputs into typed mention spans. Validate labels, finite
confidence (or explicitly unknown confidence), integer character bounds and
exact source slices. SDK output never supplies authoritative namespace, source
identity, sender, trust or temporal provenance. Preserve every valid explicit
occurrence; do not bind an ambiguous bare string to an arbitrary first match.

Bind persisted annotations to authoritative source records/message envelopes,
source versions/text identity and sender metadata. `SourceInfo.message_id` is
an internal session provenance key; the supplied message identity and sender
come from `SourceInfo.message`. Do not infer an unnamed sender or reinterpret
an assistant suggestion as a user assertion. SDK confidence never raises trust.

Mentions have `asserted=False` and do not establish an entity's state or a
relation. The semantic GLiNER path therefore annotates records rather than
turning mentions into single-valued fact keys. Existing generative extraction
and its canonical grounding/compiler/integration stages retain authority to
produce supported facts. Window mention metadata remains ancillary; it cannot
replace a claim's support evidence or bypass canonical grounding.

Use existing event-sourced record/envelope payloads for annotations, not a
second ingestion route or an independent database. Dedup/merge and replay must
not leave spans attached to rewritten text or manufacture source references.
For deduplication, retain dropped-source annotations in the successful MERGE
event's full dropped-record snapshot. Do not union them onto the kept record: its
text has a different source, and its write precedes the MERGE audit append.
Retrieval of the kept record therefore does not automatically aggregate merged
mentions.

Window caches must distinguish namespace, source identity/version, exact text
and attribution. Model-authored extras cannot impersonate system annotations.

The real ingestion gate exposed that `WindowLine.content` used the rendered
prompt line (whitespace flattened and sometimes prefixed with a speaker), so
support evidence offsets did not identify the original stored message. Keep
rendered transcript text for prompting, but use original record content for
compilation and grounding coordinates. An exact source quote must resolve
against its source snapshot, never against a synthetic prompt prefix.

## Options considered

| Option | Decision |
|---|---|
| Treat entity mentions as asserted facts | Rejected: presence does not prove a relation or state |
| Drop spans/confidence and keep only names | Rejected: loses grounding and attribution evidence |
| Source-bound ancillary mention annotations | Selected: useful extraction without overstating truth |
| Automatically persist typed GLiNER record/relation candidates as facts | Not selected: still requires explicit schemas and canonical assertion assessment |

## Verification

Focused fake-model checks cover malformed SDK objects, repeated/non-ASCII text,
invalid offsets/confidence, sender/namespace/source substitution, cache mutation,
event round-trips and merge safety. A separate bounded offline gate must actually
call `Engine.ingest_messages` and process a window with the real cached GLiNER
model. A deterministic generative reader may compile the window fixture, but
must be declared as a stub rather than described as a real LLM acceptance test.
Model call counts, checkpoint/runtime identity and limitations belong in the
retained evidence. No full-suite or complete-Engine performance benchmark.
