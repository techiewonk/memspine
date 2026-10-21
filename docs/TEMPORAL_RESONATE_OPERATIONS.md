# Temporal and embedded Resonate operations

The durable maintenance runner is `workers.runner: temporal`. It replaces DBOS
without aliases or history conversion. `inline` remains the default, and the
existing taskiq runner is an independent brokered option.

## Install and configure

```powershell
uv sync --locked --extra dev --extra temporal
```

Run an external Temporal Server reachable at `localhost:7233` for development,
or point the configuration at your cluster. The Python SDK does not start a
server when Engine starts. Example user configuration:

```yaml
workers:
  runner: temporal
  temporal:
    deployment_id: personal-memory-dev
    address: localhost:7233
    namespace: default
    # Omit task_queue to derive memspine-pipelines-v1-{deployment_id}.
    tls: false
    rpc_timeout_s: 30
    activity_timeout_s: 300
    workflow_timeout_s: 3600
    max_concurrent_activities: 4
    retry_max_attempts: 3
    max_result_bytes: 65536
  resonate:
    journal_path: ./state/micro-steps.sqlite
    task_timeout_s: 270
    step_timeout_s: 240
    step_lease_s: 360
    journal_busy_timeout_s: 5
    max_concurrent_tasks: 4
```

Select this file through the existing `Engine(user_config=...)` configuration
entry point. The Engine registers maintenance pipelines, connects to Temporal,
and starts a Worker in its process. Normal `await eng.sleep()` uses the selected
runner. The example overrides only worker settings; storage, embedding and other
services still follow the chosen template and their provider settings.

For a secured cluster, enable `workers.temporal.tls` and supply the API key with
`MEMSPINE_WORKERS__TEMPORAL__API_KEY` through your secret mechanism. The config
uses `SecretStr`; do not put secrets in logs, checked-in YAML or Temporal payloads.
Cluster namespace/queue permissions remain an operator responsibility.

## What executes where

```text
Engine.sleep -> ordered maintenance stage -> Temporal Workflow (remote history)
                                            -> Temporal Activity (Python Worker)
                                               -> fresh PipelineContext
                                               -> local Resonate coordinator
                                               -> idempotent pipeline leaf
                                               -> application SQLite journal result
```

The Workflow payload contains identifiers, bounds and fingerprints. It does not
contain a live Engine, PipelineContext, storage client, model or credential.
The Activity reconstructs clients through the existing context factory. Embedded
Resonate is forced local and cannot silently consult `RESONATE_URL`.

Each default runner invocation is a new logical operation, so a later sleep cycle
runs again. An explicitly retried operation requires both `operation_id` and a
caller-supplied immutable `input_fingerprint`; the journal rejects changed inputs
for the same logical step. A configuration hash alone does not freeze the mutable
contents of the memory database.

The current checkpoint unit is a registered maintenance pipeline, not each LLM
call inside that pipeline. Pipelines are framework-independent. Future finer
micro-steps must expose versioned serializable boundaries and preserve provenance.

## Recovery and deployment requirements

| Event | Result / requirement |
|---|---|
| Completed result, Worker restarted with same journal | Reuse the saved result for the same bound operation |
| Process dies before result commit | Retry the pipeline; its effects must be idempotent/reconcilable |
| Uncertain timeout/cancellation | Preserve lease until expiry; stale journal claims cannot commit |
| Next Activity runs on another host with a different disk | Local result reuse is unavailable; do not advertise transparent micro-step recovery |
| Different Engine/deployment shares a queue accidentally | Identity/config checks refuse execution; isolate queues and storage deliberately |
| Journal contains a completed result with different input binding | Fail the conflicting operation; do not overwrite/reuse it |
| Large pipeline output | Enforce configured byte bound; store artifact references rather than full content |

The DB, WAL and SHM files belong on the same persistent local volume. Configure
worker placement/queue consumption so retries can reach that volume and the same
application store. Temporal itself does not provide volume affinity. Do not share
SQLite on an arbitrary network filesystem or use independent files as if they
were a shared checkpoint store.

Journal fencing controls journal commits. It cannot fence a remote LLM, database
or HTTP side effect; use stable effect keys and event/source identity where those
operations support idempotency. There is no cross-database exactly-once promise.

## Migration and scope

1. Stop old DBOS processes and resolve old pending work explicitly from
   application-owned events/checkpoints. Do not run two owners for one operation.
2. Install the new locked extra and remove old runner settings from user YAML.
3. Configure Temporal endpoint, deployment identity, TLS/auth and journal volume.
4. Validate configuration, boot the Engine and run the focused live contract gate.
5. Verify actual deployment failover/volume attachment before enabling production work.

There is no `dbos` selector, extra, runner file or `dbos_system_database_url` setting.
Old settings are rejected. Historical ADRs and reports describe prior runs only.
No database copying, re-embedding or workflow-history translation is performed.

The durable ingestion outbox has its own `workers.transport` seam (`inline` or
`taskiq`). This change replaces the former DBOS maintenance runner; it does not
route ingestion windows through Temporal automatically or replace event-log truth.

See [ADR-071](adr/ADR-071-temporal-resonate-orchestration.md),
[the change directive](TEMPORAL_RESONATE_CHANGE_DIRECTIVE.md), and
[verification evidence](provider_verification/README.md).
