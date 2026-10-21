# Provider follow-up delivery standard

Applies to the owner-selected PA follow-up work. Selection authorizes local
implementation and verification; it does not invent a release destination,
production environment or measured model-quality result.

| Gate | Required evidence |
|---|---|
| Contract | Explicit inputs, provider-owned settings, output types, ownership and failure behavior |
| Architecture | One canonical ingest path; event log remains truth; projections remain rebuildable; no compatibility aliases |
| Correctness | Observable behavior, invalid inputs, retries where applicable, namespace/source identity and cleanup |
| Optional runtime | Slim imports; no model download or service startup merely from discovery/import |
| Review | Independent review of new contracts and measurement definitions; findings fixed and retested |
| Tooling | Focused pytest with skip reasons, Ruff check/format, source typing for source changes |
| Measurements | Actual sample count, corpus/oracle definition, timing boundaries, host/model/settings and limitations |
| Evidence | Exact command, passed/failed/skipped outcomes, source/lock and artifact hashes; failed attempts retained or explicitly superseded |
| Completion | `DONE` only for the scoped completion criterion; preparation and partial runs stay separate from execution |

## Correctness, quality and performance are separate

A matching source span proves where a substring came from. It does not prove its
entity type, a relation, intent, consent, or truth. A model confidence score is
candidate metadata. The existing grounding and integration stages remain required.

Synthetic hash embeddings exercise plumbing and deterministic lexical fixtures;
they do not establish semantic retrieval quality. A runtime smoke is distinct
from a held-out model evaluation. Gold/expected outputs are scored after inference
and never supplied as answer hints. Changing thresholds after seeing results
requires a new independent evaluation set.

Report Recall@k against all relevant items; a capped target hit rate or Precision@k
must use its own name. Separate first inference from repeated-input warm samples.
State percentile estimator and sample size; a handful of samples does not establish
production tail latency. Do not relabel list visibility as derivation queue lag,
readiness probes as search performance, or endpoint RSS delta as proof of no leaks.

## Environment and side effects

Local measurements use synthetic data and task-owned scratch databases/services.
Docker experiments pin images, bind loopback ephemeral ports, and remove only
their exact owned containers/volumes. No foreign service cleanup, database copying,
automatic re-embedding, paid model call or production activation is implied.

The GLiNER candidate adapter receives a caller-owned model. Model loading policy
and shared runtime ownership are separate gates until implemented. A local snapshot
test does not claim that upstream repository-ID revision/offline kwargs propagate.

## Review and release packet

Use [the selected-work delivery register](PROVIDER_PROPOSED_DELIVERY.md),
[stable task tracker](PROVIDER_TODO.md), [current operations](PROVIDER_OPERATIONS.md),
[breaking guide](PROVIDER_UPGRADE_GUIDE.md) and
[read-only reference gate](../scripts/check_provider_release.py).
The evidence index distinguishes local results, remote native experiments and
unexecuted environment prerequisites. A focused working-tree review is not a
clean full release sweep, a published PR or deployment acceptance.
