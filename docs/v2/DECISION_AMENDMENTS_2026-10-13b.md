# Owner decisions on the open review findings (2026-10-13, second set)

The owner answered the eleven items the Codex handoff left open (`CLAUDE_ENHANCEMENT_HANDOFF_2026-10-13.md`). Each is
now a planning decision; none is implemented or measured. Together with `DECISION_AMENDMENTS_2026-10-13.md`
(ER-06, ER-24, ER-31) and the selected directions for the other findings, every ER item has an owner choice.

| Id | Decision | Contract (what implementation must guarantee) | Packages |
|---|---|---|---|
| ER-07 | Claim epoch + conditional commit | Every lease takeover bumps a monotonic epoch; a deposit commits only if its epoch, source versions and erasure generation still match; a stale worker cannot write, acknowledge, advance watermarks or release the new lease; model calls stay outside write locks. | F-04, O4-01a |
| ER-08 | Quotas + explicit rejection | Accepted work is never dropped; when storage or quota limits are reached, new writes are rejected before acceptance with a distinguishable error (optionally after a bounded wait); emergency space is reserved for erasure and recovery. | RT-02, W2-12 |
| ER-09 | Contiguous watermark + holes | Progress is the highest sequence below which everything is complete plus explicit holes; projector progress, derivation coverage and source membership are tracked separately; a wait captures a fixed target that new writes cannot extend. | F-04, O4-01b, R3-15 |
| ER-10 | Tracked jobs + bounded drain | Every job is tracked; `close()` drains within a deadline and persists unfinished jobs; a cancelled job cannot commit; a model slot is not reused while its work still runs; per-tenant fair shares. | RT-02, F-08 |
| ER-11 | Context lines cited as binding evidence | The NEW line supports the assertion; CONTEXT lines that resolve references are recorded as reference-binding evidence (separate lineage role); erasing a binding source invalidates dependants; context never counts as a new occurrence. | W2-06a, W2-08, F-16a |
| ER-12 | Coverage check + targeted re-extraction (screened) | Raw text is always kept; a cheap check flags likely uncovered names, quantities and list members; targeted re-extraction only when flagged; extra calls counted; omitted-fact recall and unsupported additions measured separately. | W2-06b, W2-08, X-05 |
| ER-13 | Stable memory scope; session as provenance | Entities and assertions are keyed by a stable scope (tenant/user/agent policy); session is provenance only; retry, mention, assertion and occurrence identities are separate. | F-01, F-16c, W2-09 |
| ER-14 | Separate assessment fields | Source trust, support strength, entity-binding certainty, time precision and modality are separate fields, with explicit rules for which combinations may supersede, enter the graph or appear as tentative evidence. | F-02a, W2-08, W2-10 |
| ER-15 | One call + bounded recovery budget | One normal extraction call per window plus a small recovery budget counted by purpose (repair, truncation continuation), capped in tokens and time; validated outputs cached; processed line ranges tracked; partial output never counts as full coverage; unprocessed lines stay pending. | W2-06a, W2-06b, W2-12 |
| POL-1 | In-flight erasure: revalidate before returning | Generation and permissions are rechecked just before a read returns; erased items are dropped; an explicit read/erase linearisation point; already-delivered content cannot be recalled. | T5-04, R3-11 |
| POL-2 | Derived visibility: lineage-aware admission | A derived record is admitted only if the viewer may see its supporting lineage; otherwise it is omitted or re-derived from visible sources; numeric trust is not access control. | F-16a, R3-08, T5-01 |

## Change log
- 2026-10-13: recorded from the owner's answers.
