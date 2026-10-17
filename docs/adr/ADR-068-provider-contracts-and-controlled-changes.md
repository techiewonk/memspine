# ADR-068: Provider contracts and controlled changes

Status: implementation in progress (2026-10-17). Decisions Q1–Q35 in [the provider plan](../PROVIDER_ARCHITECTURE_PLAN.md) govern this ADR.

## Decision

Use one authoritative descriptor registry per category and derive the inspection catalog from it. Builders receive typed category-specific contexts and immutable validated settings, return typed service/capability/resource results, and register cleanup before connecting. Native clients own connections; Engine uses SQL access and provider lifecycle contracts. Explicit selections fail rather than substituting another provider. Automatic graph selection has a policy-approved compatible candidate set.

This is a breaking configuration migration. Native constructor options live under selected-provider settings; old names, aliases and retirement maps are removed. Schema validation rejects unknown fields and registry selection rejects unregistered providers.

The owner's all-service consistency clarification requires explicit `provider` and
descriptor-validated `settings` for each LLM role too. Model-name prefixes do not
select an explicit role's adapter. Tier planning creates explicit bindings and
honors the catalogue's required transport deadline. Structured retry policy is
bound to each engine's router so starting another engine cannot change it.

Keep primary graph and optional outbox mirror independent. Controlled changes require explicit planning, execution/resume, verification and scoped cleanup. Durable operation state, fencing and generation ownership protect replay and activation. SQL is authoritative and is never automatically deleted. Development may clean an old owned projection only after successful verification; production defaults to seven days when retention is omitted.

Persist local deployment ownership in authoritative SQL metadata. Require an explicit deployment identity in production and for shared/remote providers. Reject identity changes against existing owned data.

Lexical retrieval is an independent port, currently implemented by Tantivy. Future OpenSearch and Weaviate adapters can register lexical and vector descriptors independently, borrowing one owned connection where appropriate. Hybrid fusion remains read policy. Native hybrid queries require a separate verified capability and must not be counted again as two fusion legs. Future adapters are not advertised as supported before implementation and backend verification.

Lifecycle state, active health and projection readiness are separate signals. Infrastructure probes have explicit deadlines; model quality probes never run implicitly or incur paid startup calls.

## Consequences and validation

Plugins migrate to descriptors; no compatibility ownership modules remain. Runtime settings and profile semantics must be tested alongside construction, partial-start failure, namespace isolation, erasure, replay, stale-plan rejection, fence conflicts, activation and retention. Existing historical evaluation outputs remain immutable. Real external backend checks are distinct from fake contract tests, and unavailable backend checks remain unverified.

The phase checklist and detailed test gates remain in the provider plan. A passing local contract suite does not establish remote-provider support or complete the rollout.
