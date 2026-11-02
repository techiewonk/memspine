# ADR-088: SemanticEnvelope is the one canonical claim schema

**Status:** Accepted
**Date:** 2026-11-01
**Decider:** Repository owner (decision rounds 1-8, 2026-11-01)
**Register:** D-112

## Context

The rules-only producer validates claims against `SemanticEnvelope`. Stores, the read path
and the model-backed path use `AssertionEnvelope`, with an optional `.semantic` field read
only by the window worker (RM-24, DS-19). Two claim shapes mean two code paths everywhere.

## Decision

In the first cycle (ADR-086, fresh schema), every claim, rules-only or model-produced, is a
`SemanticEnvelope`: registry-typed predicate, role bindings with evidence spans, typed
`status` (asserted, planned, negated, contested) and `outcome` (none, fulfilled, failed),
valid time with precision, `known_at`, source binding (record, version, fingerprint,
binding hash) and producer (kind, revision). `AssertionEnvelope` is removed with no alias.
The projector, graph compiler, read side, inspect/REST/MCP and the model path consume the
single shape. Status and outcome move from edge-only qualifiers onto the claim (RM-57);
edges are compiled from the claim.

## Consequences

One validation and projection path. The model path must produce role-bound claims or
unresolved annotations (ADR-090). The F1..F5/F1..F12 fixtures (RM-22, RM-54) are written
against this schema.

## Amendment 2026-11-01 (direction review)

The claim keeps three separate typed fields with the vocabularies already accepted:
- `modality`: asserted, negated, hypothetical, planned, reported, question
  (ENRICHMENT_SCHEMA_CONTRACTS §Claim);
- `status`: active, tentative, contested, superseded, hidden;
- `outcome`: none, done, missed, failed, declined (RM-57 owner selection).

This ADR supersedes only the edge-only placement of those fields (ADR-080 §1), not their
vocabularies. The Decision's shorthand list ("asserted, planned, negated, contested";
"none, fulfilled, failed") is replaced by these fields. Only `modality=asserted` with
`status=active` reads as a current fact. The first implementation commit is schema-only,
so the parallel streams can rebase onto it.

### Implementation (RM-129, 2026-11-01)

The claim keeps the existing vocabularies as three separate typed fields
(docs/ENRICHMENT_SCHEMA_CONTRACTS.md section 3 and the RM-57 selection). The decision moves
only their edge-only placement (ADR-080 section 1) onto the claim:

- `assessment.modality`: asserted, negated, hypothetical, planned, reported, question;
- `status`: active, tentative, contested, superseded, hidden;
- `outcome`: none, done, missed, failed, declined (a hypothetical or question carries none).

Registry typing (`registry`), `role_bindings` and `producer` (rules, model or record, with a
revision) are claim fields; the registry predicate must equal the claim attribute and role
evidence must be cited by the claim. Only an asserted claim reads as an asserted fact.
Durable kind id: `semantic_envelope`.

RM-146 (2026-11-01): `known_at` (timezone-aware; the time the claim was recorded) and
`source_binding_hash` (the D-98 mention binding hash over the source record snapshot,
`memspine.store.source_binding`) are claim fields, set by the rules, model and record
producers. The model path sets the hash only when its window lines carry one.

Open follow-ups: the edge compiler takes `status` from the live record rather than the write-time snapshot; reported claims are not facts.

## Amendment 2026-11-02 (owner decision: rules scope)

The rules-only default gains conservative deterministic rules:
- residence moves with origin and destination at source precision;
- visits;
- corrections that supersede a prior claim from the same sender;
- single-sentence pronoun coreference;
- measurements with typed units, currency and amounts;
- multi-source restatement support, so a claim keeps every supporting source.

Anything ambiguous stays an unresolved annotation.

### Implementation (RM-22, RM-54, 2026-11-02)

`derive/discourse_rules.py` holds the rules; `derive/non_generative_facts.py` carries the typed
candidate (property key, unit, ISO currency, Decimal amount, context, outcome, resolved target,
event start, source origin) to registry validation, and `PredicateTyping` stores the typed
value. Moves bind `move_origin` / `move_destination` (and the destination residence) with the
event at source precision, resolved only against an aware source time. A visit is a `visited`
event. A `he` / `she` subject binds only to the one name of the preceding sentence. A
correction (`I meant X, not Y`) or a cancellation is resolved by the window against the live
claims of the same sender: the prior claim is archived with status `superseded` and the
successor (the corrected claim, or the commitment with outcome `declined` / `missed`) is
current; a known-time read before the change still finds the prior claim. An ingested
document chunk yields only `reported` claims attributed to the document. The non-user reason
is `source_not_user` (no alias). A restatement merged into a claim keeps its source in the
claim's parents and evidence (a `support` lineage edge under `write.lineage_table`).
Everything a rule cannot decide stays an unresolved annotation with its reason.

Open: without `write.lineage_table` the erasure plan reads a multi-source claim as conjunctive
(trust fallback), so it is hidden when one source is erased; erasing a correction does not
reactivate the claim it superseded.
