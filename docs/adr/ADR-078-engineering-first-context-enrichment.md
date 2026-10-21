# ADR-078: Evidence-bound engineering-first context enrichment

Status: Accepted policies; detailed contracts and implementation pending review.
Date: 2026-10-18.
Decider: repository owner, explicit chat selections E1/R1/T1.

## Context

Raw-first background ingestion needs contextual linking across messages without treating lexical similarity as factual evidence. Initial linking should use deterministic engineering and bounded non-generative retrieval. General semantic interpretation remains a future optional tier.

## Decision

Prefill source-cited candidates and persist only after validation. Ambiguous places stay unresolved absent explicit evidence. E1: identity comes from stable IDs or explicit cited identity/alias evidence, not automatic name similarity. R1: preserve dated residence history and multiple homes; visits/work do not update residence and closure requires support. T1: use reliable source-local anchors, preserve precision and separately track evidence availability; generated ingestion clocks cannot anchor missing historical time.

## Options and trade-offs

Explicit evidence reduces false identity merges and state corruption but leaves more unresolved candidates. Auto-merging unique/fuzzy names improves apparent coverage at the cost of unsupported identity. Latest-statement replacement loses multiple-home and historical semantics. Generated-clock/forced-UTC anchoring invents precision. The owner selected the evidence-bound options.

## Consequences and next steps

Reuse existing temporal and grounding machinery. BM25/optional word vectors propose candidates only. Raw data stays unchanged. Later evidence must not leak into earlier as-of views. Pronoun/group rules, correction targeting, reconciliation limits and final schemas remain pending choices. No runtime changes, providers or model calls are activated by this ADR.

Design and benchmark limits: [context enrichment](../INGESTION_CONTEXT_ENRICHMENT.md). Extends [background ingestion](../BACKGROUND_INGESTION_PLAN.md), especially BI-01/04/06/07.

## Accepted follow-up selections

Owner selected E2-A, C1-A and L1-A: uniquely evidence-bound pronouns/groups; explicit or uniquely supported correction targets with retained history; bounded automatic retrospective linking preserving evidence-known time. Reply links do not alone prove antecedents, correction message IDs do not uniquely identify a claim, and source authority/visibility/version/erasure checks remain mandatory. Language scope, budgets and unresolved-output contracts remain under review.

## Accepted third-round selections

Owner selected N1-A/B1-A/F1-A: English semantic rules first with all raw languages preserved; configurable bounded work with visible resumable deferral; unresolved source-bound annotations and retry eligibility without hidden LLM fallback. Truncated candidate search does not prove uniqueness. Numeric defaults, retrieval method, competing-claim handling and temporal-expression coverage remain under review.

## Accepted fourth-round selections

Owner accepted LX1-A/TR1-A/C2-A/T2-A/CT1-A/K1-A: existing Tantivy/OpenSearch deployment choices with offline candidate evaluation; source-preserving dependency trimming; retained conflicts; tested temporal coverage; completeness-aware counts; optional attributed validated local catalogues. R2/S1 “A + B + C” requires clarification of configurable modes and default; no contradictory universal model requirement is adopted. Implementation remains pending.

## Final R2/S1 clarification

Owner confirmed mode 2 as default: engineering with optional measured non-generative models. BM25 and other non-generative techniques count as engineering. Keep generative LLM fallback separately disabled unless selected. Preserve configurable no-learned-component and required-semantic-component intents. Classify rerankers by actual execution, not name; model-backed engineering still has explicit dependency/resource/quality gates. This resolves the earlier pending default, without introducing runtime schema keys or activating services.

## Proposed technical contracts

[Exact schema review](../ENRICHMENT_SCHEMA_CONTRACTS.md) covers NL-01..52 with 41 review definitions and 22 behavioural contracts. Shape validation is not source-grounding or transaction proof. Q-S1..5 were selected subsequently as recorded below; accepted choices do not imply runtime implementation.

## Accepted schema selections — 2026-10-18

Owner explicitly chose Q-S1-B/Q-S2-B/Q-S3-A/Q-S4-A/Q-S5-B. Replace all affected envelope shapes in one coordinated breaking rewrite, preserving one canonical path and no compatibility aliases. Resolved temporal values must be aware datetimes; timezone-free calendar dates remain source text with unresolved datetime and retained text precision. Keep tagged claim values and explicit source-backed domain events. Persist full selected excerpt copies in bundles, retaining original source references plus a reverse copy manifest and cascade erasure requirements. No runtime code, defaults or model calls are activated. Schema review artifacts now reflect these choices; previous civil-date and references-only recommendations are superseded.

## Evidence budget selection

Owner selected a 4,096-token evidence budget on 2026-10-18. Other operating defaults in ENRICHMENT_OPERATING_POLICY.md remain proposals pending review; no runtime defaults changed.

## Operating selections accepted — 2026-10-18

Owner explicitly selected O1-A/O2-A/O3-A/O4-A: balanced configurable 4K limits; 43 typed predicates and nine attribute keys with gated extraction; immediate denial plus asynchronous tracked erasure (two-second foreground wait); development backups off and production content expiry within seven days, restore tombstone gate and honest pending-retention status. Accepted starting design remains unmeasured and unimplemented.
