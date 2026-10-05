# ADR-039: Hard forget reaches summaries and the vector table's history

- **Status:** accepted (Wave 1 trust review fixes, 2026-10-05)
- **Date:** 2026-10-05
- **Decision id:** amends M7 / #43 (erasure cascade) and ADR-021 (LanceDB is the vector store).

## Context

The review found three gaps in a hard forget, and in the `verify_forget` proof that checks it:

1. **Summaries.** The consolidation and reorganize summaries named their members only in the
   WRITE payload (`consolidation.member_record_ids`). Their `source.parents` was empty. The cascade
   walk (`_descendants`) and `verify_forget` read only `source.parents`. A forgotten turn's text could
   therefore survive in a summary, while `verify_forget` still reported `clean: true`.
2. **Proof depth.** `verify_forget` checked only direct children.
3. **Vector history.** A LanceDB delete writes only a deletion file. The vector stays in its data
   file, and `checkout()` of an older table version still returns the row.

## Decision

- **Forget by cascade.** A summary is derived content, so it goes when any of its members goes. It
  is not re-derived without that member: a regenerated summary would be a new LLM call on the write
  path of an erasure, and its text could not be checked against the erased content. Consolidation
  and reorganize summaries now record their members as `source.parents`.
- **Older summaries.** Summaries written before this change are found from the log. The walk also
  follows `member_record_ids` in WRITE (`consolidation`, `reflection`) and CONSOLIDATE events. It
  follows the `source.parents` kept in redacted WRITE snapshots too, so the walk can pass through an
  intermediate record that is already erased. `verify_forget` uses the same transitive walk.
- **Vector purge.** After the log is redacted, a hard forget calls `LanceDBVectorStore.purge_deleted`.
  This rewrites every live row with a no-op update, so every old fragment ends fully deleted and is
  dropped. It then runs `optimize(cleanup_older_than=0)`, which keeps only the latest version and
  removes the files that only older versions used. The purge runs under the store's write lock.
  `delete_unverified` stays off, so the files of another engine's in-flight commit are kept.
  `rebuild()` also purges, because replay re-deletes the forgotten rows.
- **Proof.** `verify_forget` adds `vector_history_absent`. It scans every retained table version, up
  to `LANCE_VERIFY_MAX_VERSIONS`, and reports False while this process holds an unpurged delete.
  `clean` now needs this field to be True. A failed purge (for example a commit conflict with
  another writer) leaves the result unproven, never clean.
- **Lexical residue.** Tantivy drops a deleted document's terms only when its segment is merged, and
  tantivy-py cannot force a merge. The proof reports this in `residual_risks` and does not claim it.

## Consequences

- **Cost.** A hard forget now costs one pass over the vector table, and `verify_forget` costs one
  pass over the log. Both are erasure-time calls.
- **Integrity mode.** Summaries now declare parents. With `integrity.enabled`, a summary's
  effective trust is re-checked against its members (MTI-D, B4'), the same as for reflections. A
  quarantined or archived member therefore lowers the summary.
- **Not covered.** Say a process deleted a row without purging, and a routine 7-day cleanup in a
  later process then removed the pre-delete versions. The row's bytes can stay in a data file that
  the version scan cannot see. Hard forget always purges, so this needs a crash between the delete
  and the purge.
