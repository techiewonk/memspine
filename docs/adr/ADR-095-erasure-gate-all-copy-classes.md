# ADR-095: Erasure gate across every copy class

**Status:** Accepted
**Date:** 2026-11-01
**Decider:** Repository owner (decision rounds 1-8, 2026-11-01)
**Register:** D-119

## Context

Erasure is checked at the fenced effect commit, but nothing proves forgotten content
disappears from every copy (DS-16).

## Decision

The phase C gate includes one test per copy class:
- records;
- event payload redaction;
- vector, lexical, graph and mention projections;
- copied excerpts;
- unresolved annotations;
- caches;
- logs and traces;
- restored backups, which re-hide erased ids on restore.

A forget during in-flight background work must leave no copy after the effect commit.
Unsupported copy classes fail the gate rather than being skipped silently.

## Amendment 2026-11-01 (direction review)

Further copy classes:
- the entity name index;
- the message `metadata` bag;
- open-window drafts;
- truncated packed excerpts.

Each wave that adds a copy class adds its erasure test in the same wave.

## Amendment 2026-11-02 (owner decision: hidden facts)

A derived fact whose every source has been erased is redacted by default: its text and quote
are removed and an id-only tombstone is kept for lineage. A fact that still has a live source
stays hidden with its text, pending re-derivation. `erasure_redact_hidden` remains the opt-in
that redacts every hidden fact.

## Amendment 2026-11-02 (RM-163: window drafts)

An open-window draft row holds one turn's id, window key, source version and content hash,
and never its vector. A forget deletes the turn's rows by record id, whether or not the
namespace's draft was restored in this process.

## Amendment 2026-11-07 (RM-22: independent restatement survival)

The existing RM-22 multi-source restatement exception is narrow: a semantic record may stay
visible after one supporting source is erased only when the system has replayable proof that
each source independently supports the same complete canonical claim. The proof is system-
authored at semantic dedup, typed and quote-free; it binds a digest of the semantic claim and
the independently sufficient source IDs. Claim comparison preserves role values and types,
qualifiers, modality, outcome, scope and temporal meaning. Evidence and source-local identity
fields are excluded from that comparison. Every support and binding role on each input must be
covered by that input's single source. Caller or model-supplied proof is discarded. Untyped,
malformed, multi-source, conjunctive, binding-dependent or otherwise unproven records keep the
existing conservative member behavior and remain hidden pending re-derivation. Multiple parents,
shared endpoints, generic support labels and similarity are not proof.

Erasure removes quotes from evidence spans addressed to the erased source in current survivor
records and historical event snapshots, while preserving other source evidence and lawful
claim text. The sanitized survivor is written as an event before hard deletion, so replay cannot
restore the erased quote. Proofs contain no quotes and are validated against the current claim,
parents and evidence; erased sources cannot continue to authorize support. This amendment
records only the already accepted RM-22 restatement exception and does not expand it to summary
or cross-message binding claims.

## Amendment 2026-11-07 (RM-153: completed window survivors)

When one source of a completed window effect is erased, the fenced erasure transaction removes
the affected effects and appends one ordinary window outbox item for the surviving live sources
in original window order. The erasure transaction also scrubs the erased id and its snapshot
metadata from every retained or pending window item, reopening any affected `source_missing`
hole. The new item carries the original non-content window context, retained in a quote-free
completed facts effect artifact so it remains available after item compaction, and the surviving
source versions, hashes and erasure generations captured in that transaction. It never copies
source text or annotation artifacts. The existing window handler reloads each source and accepts
it for re-derivation only while that captured snapshot is still current; the ordinary effect
fence then authorizes the result. If no sources survive, no re-derive item is written. If the
original item and its effect context are both unavailable, a durable blocked window item records
underived work without running under guessed session context. Deleting effects and recording this
obligation are atomic, so restart or retry cannot lose or duplicate survivor work.

## Amendment 2026-11-09 (RM-151: fenced correction compensation)

A rules correction can close its predecessor before its window effect commit. If that commit is
fenced, compensation hard-forgets the window's derived successor and reopens the predecessor only
when the canonical correction WRITE proves the exact predecessor and successor. Before removing
the successor, compensation records an ID-only intent naming that relation and its one raw source;
startup can finish the repair after an interrupted forget. The existing correction restoration
checks still require the same namespace and sender, lawful prior support, an unchanged closure,
no later lifecycle transition and no live successor in the chain. The restored WRITE copies the
current sanitized predecessor and changes only lifecycle fields. Replayable LINK updates reactivate
its compiled edges, and repeated retries or restarts do not append a second restoration.
A successor that later gained an independently sufficient source is retained only when its
current system-authored restatement proof validates and a separate completed window facts effect
pins that live source's current version and content hash. An invalid proof or an uncommitted
source cannot keep the refused window's output. A lost original source's quote must already be
scrubbed before this exception applies.

## Amendment 2026-11-07 (RM-158: resolver registry source evidence)

Resolver `entity.create` and `entity.alias` markers now cite the raw turn IDs and pinned versions
from every cited turn in the compiled envelope's evidence, including context turns, as `source_ids` and
`source_versions`. The derived claim record ID is not a substitute for a raw source. Every
marker represents one mention contribution: its label and context depend on **all** cited
sources. A repeated label from another turn is recorded separately, so it can survive when the
first source is erased. A cumulative context string is never attributed to a later turn.
The resolver checks every cited raw record's namespace, version and visibility before creating
the contribution and again before appending its marker. Direct resolution and envelope resolution
also reject a source ID present in a retained registry erasure fence before mutating the warm
registry; the native append transaction repeats the fence check at commit. Missing, deleted, quarantined or hidden
sources cannot authorize it; retained cold records remain eligible. A context item without a
turn citation defers registry resolution because the copied text has no provable source.
Durable direct resolution requires raw source citations, and the final flush sanitizes any
namespace-tagged source-free create or alias queued through lower-level registry calls. Pending resolver markers
carry their authoritative event namespace in the warm queue; flushing another namespace leaves
them pending, and this queue metadata is omitted from persisted marker payloads. Durable flush
rejects untagged lower-level registry changes, including merge and split markers, instead of
assigning them to the flush caller's namespace.

Hard erasure blanks the erased contribution's name, alias, surface and context in historical
markers, prunes the warm registry and its pending marker queue, and reconstructs the warm state
from sanitized contributions. The historical marker scrub is filtered by the event row's
namespace, rather than inferred from a payload scope. Older markers without valid source
provenance are blanked within that namespace because their label dependencies cannot be proved;
markers in other namespaces remain intact. If replay fails after durable redaction, the warm
registry and local resolver caches are cleared before the error propagates, and a later load
retries from the sanitized log. An opaque entity ID may remain when an independent surviving
label supports it. Merge markers no longer copy added alias text; replay derives those aliases
from the surviving entities, and split uses the resulting merge record. Durable merge markers
discard free-text reasons because no raw evidence pins those words. No raw sidecar is kept
for erased text. Split preserves a merge alias when a separate surviving contribution on the
kept entity supports the same label.

The current resolver and hard erasure share the engine's per-namespace write lock within one
process, including the final source check and marker append. This closes the tested local race
where erasure lands between resolution and persistence.

## Amendment 2026-11-07 (RM-158: native registry source fence)

After namespace and legal-hold preflight, hard erasure commits a canonical, ID-only
`memory.registry_erasure_fence` event before the first source deletion or copied-text scrub.
The event names the erased source IDs and contains no names, context or source snapshots. It
is retained by rolling event-log pruning: an interrupted erase must still deny new resolver
text while the raw read-model row remains. Replay treats it as a non-projecting control event.
An idempotent erase may append another fence; either fence denies a later contribution.

In persisted `full` and `rolling` modes, every name- or context-bearing `entity.create` and
`entity.alias` append checks **all** distinct pinned raw source IDs, versions, namespaces,
raw turn types (`episodic` or `resource`), live statuses and quarantine flags and the
accumulated erasure fences in the same native transaction as the canonical marker insertion.
SQLite takes its writer lock on insertion before reading sources and fences. PostgreSQL
serializes marker and fence transactions through a namespace advisory transaction lock,
locks source rows for the status/version check, and takes a global fence advisory lock
before allocating fence sequences so cross-namespace fence commits stay in sequence order.
MongoDB uses a snapshot transaction whose event-counter write serializes with fence appends
and whose conditional writes to each source document conflict with concurrent source
changes; the record document materializes the pinned version. A rejected append rolls back
in full and clears the
resolver's warm registry so the next load replays only committed markers. A marker committed
before the fence is included in the subsequent namespace-scoped historical scrub. Text-free
sanitized create/alias markers and text-free pending/merge/split control markers may be
appended through the registry door without a source check. The door rejects `reason` and
`added_aliases` on create/alias even with pinned sources, and rejects every known free-text
field on pending/merge/split. Historical erasure and replay scrub those misplaced fields
from legacy markers too. Generic `append_event` and `append_with_outbox`
reject all fresh `entity.*` registry markers and registry erasure fences. This guard covers
the supported registry marker fields; it is not an arbitrary payload-schema validator.
The SQL replay builder uses a private fresh-target transfer door
for canonical historical events before projecting source rows; it sanitizes copied names for
source fences present before transfer, encountered during transfer, or found in its final
source-fence scan. A fence committed after that final scan is outside this replay snapshot;
live migration still needs source quiescence or a separate ongoing replication protocol.
Each resolver invocation reads newer fences before using its warm registry and removes
matching contributions by the trusted event-row namespace, including while another process
has committed the fence but has not yet finished its historical scrub. Initial replay applies
all retained fences as well. Internal event-namespace tags are never persisted in marker
payloads. Fence reads use event sequence high-water marks; a concurrent fence that commits
after an invocation's read is observed on its next invocation, while any new marker append
still passes the native source fence.
SQLite `ephemeral` mode stores neither resolver markers nor erasure fences and rejects a
durable registry append; resolver flush retains its validated labels in memory only and
provides no cross-process registry durability.
Native PostgreSQL and MongoDB service execution remains a separate deployment gate.
