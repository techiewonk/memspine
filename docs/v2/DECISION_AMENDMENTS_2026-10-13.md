# Confirmed decision amendments - 2026-10-13

The user selected the recommendations for ER-06, ER-24 and ER-31. These amendments supersede those three selections in the original export and its historical recheck; they do not mark implementation complete.

| Decision | Selected | Contract |
|---|---|---|
| ER-06 | A | Durable acceptance is the default: commit authoritative SQL data and outbox intents atomically, then return per-capability readiness. Callers may explicitly wait for required raw indexes or derived memory/graph through bounded barriers with timeout and readiness reporting. |
| ER-24 | C + A | C + A: adaptive complete evidence bundles under an exact reader-tokenizer budget, including rendered headers, citations and configured reserves. Estimated accounting is an explicitly labelled fallback and must not claim a hard token guarantee. |
| ER-31 | B | Result/context caches include effective scope and viewer/purpose, time view, policy/model versions, record versions and invalidation generations. Recheck admission at use; keep result/context caching disabled until this contract is implemented. Embedding-cache keys separately include encoder/config identity. |

Current full selection record: [reconciled JSON](data/decisions_reconciled_2026-10-13.json).

ER-07 through ER-15 remain unanswered. The in-flight erasure and derived-visibility follow-up questions remain open. ER-27 retention limits and ER-29 viewer-policy reconciliation remain conditions for implementation. Other original selections are preserved.

Self-contained shareable handoff: [Claude enhancement handoff](CLAUDE_ENHANCEMENT_HANDOFF_2026-10-13.md).
