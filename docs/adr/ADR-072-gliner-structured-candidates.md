# ADR-072: Source-cited structured GLiNER candidates

**Status:** Accepted for explicitly constructed candidate extraction only
**Date:** 2026-10-17
**Deciders:** Owner-authorized provider follow-up implementation; engineering mapping below

## Context

PA-120 requests typed record and relation extraction from GLiNER2.5. The Engine
already has an event-sourced ingestion path and canonical grounding/integration
stages. A correct source substring is evidence of a mention, not proof that a
model's proposed type or relationship is true. The local Small-model acceptance
fixture demonstrated this: a valid substring was misclassified as an organization.

## Decision

Provide an optional, explicitly constructed structured candidate adapter. The
caller supplies the model and a strict task schema. Source identifiers, sender,
namespace and ownership come from caller context, never model output. Validate
schema allowlists and exact bounded source spans before returning typed candidates.
Reject malformed or mismatched ownership rather than silently coercing it.

The adapter has no persistence authority. It may produce canonical evidence and
grounding inputs, but a consumer must still pass the existing grounding, temporal,
entity-resolution and conflict integration stages before writing an assertion.
It creates no second ingestion API, compatibility layer or automatic model load.

## Options considered

| Option | Complexity | Benefit | Cost / limitation |
|---|---|---|---|
| Persist SDK records directly | Low initially | Fast prototype | Bypasses ownership and assertion contracts; rejected |
| Typed candidates with source evidence | Moderate | Reusable task schemas, explicit evidence, existing integration boundary | Consumer still needs assertion semantics; selected |
| Enable a shared model stage everywhere now | High | Potential model reuse | Shared lifecycle/settings and loader-policy gates are unfinished; separate PA-118/119/121/123 |

## Consequences

- Candidate production can be checked independently of generative extraction.
- Model confidence and source citations remain distinguishable from verified truth.
- Schema changes affect candidate identities; automatic Engine cache/runtime wiring
  must account for effective task schema, checkpoint and inference settings.
- Slim-core imports remain lazy: importing the adapter must not import Torch or
  load model weights. No optional feature is enabled by this ADR.

## Verification and follow-up

Malformed schemas, invalid spans, ownership mismatches and unsupported model fields
need focused fail-closed tests. Real local SDK inference is a separate gate from
fake-model contract tests. See [adapter documentation](../GLINER25_STRUCTURED_EXTRACTION.md)
and [local model results](../GLINER25_LOCAL_RESULTS.md). The provider tracker retains
unfinished lifecycle and Engine integration items; candidate tests do not close them.
