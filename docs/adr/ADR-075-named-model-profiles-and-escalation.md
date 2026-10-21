# ADR-075: Named model profiles, role bindings and bounded escalation

**Status:** Accepted
**Date:** 2026-10-18
**Deciders:** Owner selected multi-model recommendations and implementation

## Context

ADR-074 eliminated repeated GLiNER loads but also bound classification, semantic
NER and prepass to one checkpoint. Sharing is a resource optimization, not a
requirement to use one model for every task. Generative roles already support
different providers and tier planning; specialized extraction must retain the
same freedom. Static tiers alone do not implement runtime escalation.

## Decision

Add `models.profiles`, a mapping of stable names to strict `kind`, `provider`
and native `settings` records. Supported kinds are `llm` and `gliner`.
Generative settings are validated by the selected provider descriptor; GLiNER
settings use `GlinerSettings`. Profiles are configuration, not stored memories.
Add `models.bindings` from task/role to profile name. The GLiNER task names are
`gliner_decision`, `gliner_semantic`, and `gliner_prepass`; generative task names
are existing engine prompt roles. Validate names, references, kind and required
capabilities before runtime work. No implicit GLiNER-to-chat API conversion.

Remove the single `models.gliner` field without an alias. Profiles and bindings
default to empty mappings; an enabled GLiNER consumer requires an explicit
binding. Native profile `settings: {}` still receives provider-owned defaults.
This avoids dangling default GLiNER references when an application declares
only LLM profiles. `models.gliner_max_models` bounds resident GLiNER models; it
defaults to one. Increase it explicitly when different checkpoints must remain
resident together. Pool keys contain effective settings, so different profile
names with identical settings still share weights. Independent Engines retain
independent resource owners. Share model resources, never user extraction results.

Named provider construction carries an optional stable `LLMBuildContext.resource_key`
(`llm.profile:<name>`) separately from the logical role. Native ownership uses
that key so two models serving one role cannot collide during registration or
shutdown. Inline bindings retain role-based resource keys.

Named LLM bindings coexist with the intentional inline `llm.roles` option;
configuring both for one role is an error. Explicit role bindings override
automatic tier choices. Existing tier catalogue placement is retained, not
duplicated into another automatic planner. Profile references make System 1
(fast) and System 2 (stronger verification/reasoning) choices explicit.

Add opt-in `models.escalation` policies per bound generative role. A policy names
ordered target LLM profiles, explicit triggers (`error`, `empty`, `invalid_json`),
a finite attempt count, a required total timeout and a required per-attempt
output-token setting. All attempts remain under the same total deadline.
Cancellation propagates. Invalid configuration and programming errors do not
silently activate a fallback. Strict JSON validation is a configured trigger;
it is not semantic answer validation or a confidence estimator. Exhaustion
must surface failure rather than silently accepting an invalid result.

Apply consent/remote-data gates independently to every candidate, including a
remote fallback behind a local primary. Count actual model attempts, not just
the outer routing operation. Native providers must honor generation settings;
an output setting is not a guarantee of billing or computation cost on an
arbitrary external backend. No paid calls or automatic hosted-model enablement.

## Options considered

| Option | Decision |
|---|---|
| Single shared GLiNER checkpoint | Superseded as the only configuration; retained as the default named binding |
| Independent model settings on every consumer | Rejected: repeated declarations and ownership drift |
| Named profiles with explicit task bindings | Selected: independent selection plus identical-resource sharing |
| Automatic escalation on model-written confidence | Rejected for this change: requires calibrated signals and quality evaluation |
| Explicit bounded failure/format escalation | Selected: observable, testable and off unless configured |

## Verification and delivery

PA-130–136 cover schema/resolution, GLiNER routing, generative bindings,
escalation, prerequisite classification, focused tests and documentation. Use
fake providers for resource/call/budget/consent checks; preserve slim imports
when optional features are off. Regenerate configuration snapshots and stage
references. Do not run a complete Engine performance benchmark.

Deployment acceptance is a separate backlog from local development. Replace
the overloaded `NEEDS_ENV` tracker state with `DEPLOYMENT_CHECK`, `NEEDS_INPUT`
and `NEEDS_TOOLCHAIN`, without marking unexecuted checks complete.
