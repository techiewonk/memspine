# Architectural Change Directive: Temporal macro-orchestration + local Resonate micro-orchestration

Date: 2026-10-17. Audience: backend engineers and implementation agents.
Status: active maintenance runner migrated and locally verified; owner selected option B, embedded Resonate plus an application-owned durable step journal (2026-10-17).
DBOS has been removed from active source, dependencies, templates and configuration. The runtime now implements Temporal + embedded Resonate through the existing maintenance TaskRunner. Production deployment acceptance remains separate.
Reference template verified with CPython 3.13.5, `temporalio==1.34.0` and
`resonate-sdk==0.8.1` in an isolated environment.

## Active implementation and verified scope

| Component | Canonical source | Current responsibility |
|---|---|---|
| TaskRunner adapter | [`temporal_runner.py`](../src/memspine/workers/temporal_runner.py) | Lazy SDK lifecycle, Client/Worker, fresh context, local Resonate, input-bound journal and bounded results |
| Deterministic Workflow | [`temporal_workflow.py`](../src/memspine/workers/temporal_workflow.py) | Serializable identity-only Activity invocation and bounded retries |
| Application journal | [`step_journal.py`](../src/memspine/workers/step_journal.py) | SQLite committed result reuse, changed-input rejection, leases and fencing |
| Provider settings | [`schema/llm.py`](../src/memspine/config/schema/llm.py) | Typed Temporal/Resonate defaults and cross-field bounds |
| Runtime wiring | [`container.py`](../src/memspine/runtime/container.py) | Provider settings to runner; Engine registers pipelines before start |

See [operations/configuration](TEMPORAL_RESONATE_OPERATIONS.md) and the
[current live gate](provider_verification/temporal-live-final-tests.log).
The checkpoint unit is a registered maintenance pipeline, not every nested model
call. The existing durable ingestion outbox is an independent inline/taskiq seam.
This change does not serialize PipelineContext or add orchestration imports to
pipeline functions. Workflows use the default SDK sandbox. Root package exports
are lazy so importing a pure Workflow does not import Engine/Rich/model clients.

The original example below remains a pure processing illustration. Current
application configuration and runtime defaults are those in the operations guide.

## 0. Mandatory correction to the requested guarantees

The topology is feasible. The specified native embedded SQLite checkpoint setup
is **not verified or provided by the inspected Resonate SDK**. Do not invent a
`Resonate(storage="sqlite://...")` API or describe in-memory promises as persistent.
The inspected SDK's local connection creates an in-memory ServerState; its
constructor has no SQLite-file parameter. Its current SDK differs from the
older generated API page: use the pinned package source and async APIs.
[Resonate package/source](https://pypi.org/project/resonate-sdk/0.8.1/),
[current guide](https://docs.resonatehq.io/develop/python).

Resonate's durable coordinators also have replay constraints. Nondeterministic
operations belong in leaf functions reached through `await ctx.run(...)`.
Neither framework serializes an arbitrary Python stack or resumes an interrupted
network request at its exact machine instruction. A leaf interrupted before
completion may execute again. [Replay constraints](https://docs.resonatehq.io/develop/constraints).

The selected implementation uses local **in-memory Resonate promises** plus a
separate application-owned SQLite **step-result journal**. The SDK promises are
still not persistent. Completed leaf outputs are reused when the same journal
file survives. Journal unit tests include an actual subprocess exit and reopening;
the real SDK example verifies repeated Activity execution reuses completed rows.
Real Temporal dispatch, failed-Activity retry, Worker reopen/result reuse and Engine
sleep/reopen checks pass. Hard container failover, volume reattachment and production
ingestion orchestration integration remain deployment/future-integration gates.

## 1. Topology and responsibility boundaries

```text
Application / ingestion API
  | validate, persist raw event + durable dispatch intent, return receipt
  v
Outbox dispatcher ---- start Workflow with stable business identity ----+
                                                                     |
              EXTERNAL TEMPORAL SERVER CLUSTER + PERSISTENT HISTORY <--+
                | workflow tasks / activity tasks / timers / retries
                v
           Python Worker processes (horizontally scalable)
             |
             +-- Temporal Workflow sandbox
             |     deterministic macro business decisions only
             |     long waits, approvals, cross-service order, compensation
             |
             +-- Temporal Activity attempt
                   | fresh application context / injected service clients
                   +-- embedded Resonate runtime, strictly local
                   |     async coordinator -> ctx.run(leaf) -> dynamic loop
                   |     bounded concurrency, time and iterations
                   |     local promises: RAM; application step results: SQLite
                   +-- application SQL/events and artifact store as needed
                         stable side-effect keys / immutable source references

Optional future persistence extension:
  Activity -> application-owned step journal -> local SQLite on durable volume
  This is NOT a built-in Resonate SQLite store and is NOT in the template.
```

| Layer | MUST own | MUST NOT be mistaken for |
|---|---|---|
| Temporal Server | Workflow event history, scheduling, durable timers and retry coordination | A transaction manager for arbitrary application databases |
| Temporal Workflow | Deterministic business sequence, compensation policy, cross-service coordination | A place to open DB clients, run models, inspect wall-clock time or perform I/O |
| Temporal Activity | I/O, model calls, application context hydration and local orchestration | An exactly-once side-effect boundary |
| Embedded Resonate | Bounded local task graphs, dynamic processing, async leaf coordination | Persisted local recovery when using its in-memory connection |
| Application persistence | Accepted raw events, idempotent deposits, stable artifacts and projection lineage | A second orchestration history to be mutated without ownership rules |

Temporal's replay uses recorded events and Activity results. Use Workflow-safe
time/random/timer APIs when such values are needed in macro control flow; do not
call native time/random/network APIs there. Activities are the correct boundary
for Resonate and non-deterministic application work.
[Temporal Workflow definitions](https://docs.temporal.io/workflow-definition).

## 2. Execution contract

1. Admit a job using namespace, operation ID, source-message IDs and immutable
   configuration/model/schema revisions. Bind the operation ID to its input hash;
   reject reuse with changed input. Use references rather than full transcripts
   in Temporal arguments/history. Apply payload encryption/access policy where needed.
2. Persist source events and dispatch intent atomically in application storage.
   The outbox starts the Temporal Workflow using a stable ID. Reconcile unknown
   start outcomes against that ID; do not silently accept conflicting input.
3. The Workflow executes normal server-backed Activities. Do not substitute
   Temporal Local Activities where server scheduling/retry is required.
4. The Activity hydrates current connections from stable job references, creates
   its owned local Resonate runtime and registers the micro coordinator.
5. The coordinator awaits `ctx.run` for model/API/DB/random/time effects. Branch
   on returned results; bound loops and parallel fan-out. Do not put live clients,
   locks, queues or PipelineContext in serialized job arguments.
6. Leaf adapters have independent deadlines, cooperative cancellation and stable
   side-effect idempotency keys. A random promise ID is not business deduplication.
7. Deposit validated results through the existing engine/event door. Use atomic
   deduplication or a transactional outbox for each external effect. A remote LLM
   provider without idempotency/reconciliation can be called or billed again.
8. Heartbeat liveness from an Activity-context task. A heartbeat containing only
   `state=running` is not a checkpoint. Persist progress/artifact references before
   advertising completion; heartbeats can be throttled and the last update lost.
9. Return small result/receipt references. Mark macro completion only after the
   application commit acknowledgement. Do not return success for dead-letter failures.

Retry ownership: Temporal owns outer retries. The reference sets Resonate
`Never()` to avoid multiplicative retry loops. A future bounded leaf retry budget
must fit inside the Activity deadline and explicitly disable overlapping client
retries. The example's local invocation ID includes the Activity attempt because
failed local roots are terminal; stable business-side keys exclude attempts.

## 3. Crash and recovery semantics

| Failure | Verified baseline behavior | Required safety |
|---|---|---|
| Workflow Worker dies | Another Worker replays macro history | Replay-compatible workflow code and versioning |
| Activity Worker dies | Heartbeat or Activity timeout permits a retry, subject to retry policy/budgets and Worker availability | Idempotent/reconcilable effects and stable job identity |
| Retried Activity runs elsewhere | A fresh local Resonate graph is constructed; prior RAM state is absent | Recompute, or read portable application checkpoints/artifacts |
| Same worker restarts with local SQLite journal | Only committed application checkpoints may be reused, if the same durable volume is mounted | Journal verification, immutable input/schema revisions and exclusive/fenced ownership |
| Container/host storage is lost | Container-local files do not follow a Temporal task | Recover a durable volume or recompute from authoritative data |
| Crash after effect, before checkpoint/ack | Effect may already have occurred and retry may repeat it | Destination idempotency, transactional outbox or explicit reconciliation |
| Heartbeat timeout but old process continues | Another Activity attempt may overlap the old one | Fencing/idempotent writes; timeout does not kill arbitrary external work |

A future durable Resonate configuration must replay its coordinator and reuse
recorded child results. The first incomplete or ambiguously completed leaf can
rerun. This is recovery from a logical boundary, not a frozen stack.
Temporal schedules eligible retries; it does not guarantee a reboot, same host,
same volume, or immediate rerouting. [Activity heartbeat/error handling](https://docs.temporal.io/develop/python/best-practices/error-handling).

## 4. Selected local durability policy: B

| Option | Strict embedded/no Resonate server? | Cross-process micro-state | Directive |
|---|---|---|---|
| A: local Resonate memory + Temporal retries | Yes | No; graph recomputes | Verified runnable baseline, only if coarse retries are acceptable |
| B: application-owned SQLite step journal | Yes | Committed application step outputs on a surviving/remounted volume | Selected by owner; example implemented/tested, production integration pending |
| C: Resonate server/sidecar backed by SQLite | No, it adds a server process and protocol calls | Depends on server storage and volume availability | Requires changing the stated embedded-only constraint |
| D: custom embedded persistent connector | Potentially | Unproven until implemented and tested | Requires transactional promise/task/timer/CAS semantics and conformance testing; no fake adapter |

For B, store a plain file path such as `/var/lib/memspine/local_resonate.db` in
application settings and let the chosen SQLite client create it. A URI string
alone does not install a persistence engine. Keep the database/WAL/SHM on the same
durable filesystem, use supported SQLite locking and bounded busy timeouts, and
avoid independent writers sharing one unfenced checkpoint scope. One node's
private disk is not a fleet-wide recovery store. A per-volume owner must be fenced
before another owner resumes; Temporal task queues provide no volume affinity.

Minimum journal identity: deployment + namespace + operation + pipeline revision
+ logical step path. Bind immutable input and model/schema revisions in a stored
fingerprint and reject changed input under the same identity; putting the input
hash only in the lookup key would hide conflicting retries. Commit output or immutable
artifact reference before marking a step complete. Track attempts separately.
Changing branching inputs or schemas invalidates checkpoint reuse; do not skip
steps solely because a row with a matching numeric index exists.

## 5. Python reference template

The following modules are executable orchestration scaffolding, not a claim of
production deployment acceptance. The demonstration leaves perform pure bounded
normalization; replace them with injected application adapters. Real deposition,
external effects, credentials, tenancy authorization and optional persistent
checkpoints remain application integration work. The runtime remains local even
when ambient `RESONATE_URL` points at a server.

Install the SDKs in a dedicated template environment; do not modify the main
application lock until the migration phase is selected:

```powershell
$env:PYTHONPATH = "docs/examples"
uv tool run --from uv==0.13.1 uv run --isolated --no-project `
  --with temporalio==1.34.0 --with resonate-sdk==0.8.1 `
  python -m temporal_resonate.worker
```

`localhost:7233` is the requested development endpoint, not a Temporal server
started by this code. Production must provide the external cluster address,
namespace, authentication/TLS and its underlying durable service storage.

### contracts.py

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Job:
    namespace: str
    operation_id: str
    items: list[str]  # Bounded synthetic data; use artifact references for real memory content.
    max_rounds: int = 3


@dataclass(frozen=True)
class Result:
    namespace: str
    operation_id: str
    items: list[str]
    digest: str
    rounds: int
```

### micro.py

```python
"""Pure example leaves. Replace with injected, idempotent application adapters."""

from __future__ import annotations

from typing import Any

from resonate.context import Context
from resonate.resonate import Resonate
from resonate.retry import Never


async def prepare(ctx: Context, items: list[str]) -> list[str]:
    return [item.strip() for item in items if item.strip()]


async def refine(ctx: Context, items: list[str]) -> list[str]:
    # An LLM/network call belongs in a leaf like this, with its own deadline
    # and stable external idempotency key. It does not belong in the coordinator.
    return sorted({item.casefold() for item in items})


async def review(ctx: Context, items: list[str]) -> bool:
    return items == sorted(set(items)) and all(item == item.strip().casefold() for item in items)


async def micro_pipeline(ctx: Context, items: list[str], max_rounds: int) -> dict[str, Any]:
    current = await ctx.run(prepare, items)
    for round_index in range(max_rounds):
        current = await ctx.run(refine, current)
        accepted = await ctx.run(review, current)
        if accepted:
            return {"items": current, "rounds": round_index + 1}
    raise ValueError("Micro-processing exhausted its iteration budget")


def new_local_runtime() -> Resonate:
    # env={} prevents RESONATE_URL/HOST in the worker environment selecting
    # a remote transport. The verified SDK selects its in-memory LocalConnection.
    runtime = Resonate(env={}, autostart=False, retry_policy=Never(), max_concurrent_tasks=4)
    runtime.register(micro_pipeline, name="micro-pipeline-v1")
    runtime.start()
    return runtime
```

### Application-owned journal and checkpointed runtime

[`step_journal.py`](../src/memspine/workers/step_journal.py) implements SQLite WAL,
transactional claims, finite leases, monotonically increasing fencing tokens,
input-conflict rejection and committed JSON results. [`durable.py`](examples/temporal_resonate/durable.py)
wraps each Resonate leaf with that journal. The coordinator replays after retry,
while completed application leaves return saved outputs. An interrupted leaf can
run again. Journal fencing does not fence a third-party API or graph database;
external writes still need idempotency keys or reconciliation.

`MEMSPINE_MICRO_JOURNAL_PATH` defaults to `local_resonate.db` for development.
Production Worker startup requires it explicitly. Place the DB/WAL/SHM together
on durable local storage and route recovery to a worker with that same volume.
The example uses a 120-second step lease, 30-second leaf deadline, 100-second
execution deadline and a 90-second Resonate promise budget. A new attempt may
receive `StepBusyError` until the previous lease expires. No distributed volume
ownership controller or production storage-affinity configuration is provided.
Timeouts retain the outstanding lease, and the checkpoint callback receives a
stable operation/step effect key for an external adapter to forward. Expiry still
permits replay; external effect reconciliation remains required. The result
verifier binds identity, rounds and items to the originating job.
The pure demo has no model/schema revision; real model adapters must add immutable
model/prompt/schema revisions to the input fingerprint and bump the pipeline key
for incompatible changes.

### activities.py

```python
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import asdict
from datetime import timedelta

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .contracts import Job, Result
from .durable import new_durable_local_runtime


def digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


async def keep_heartbeating(operation_key: str) -> None:
    while True:
        # Created inside the Activity, preserving Temporal's activity context.
        # This is liveness metadata, NOT a persisted micro-step checkpoint.
        activity.heartbeat({"operation_key": operation_key, "state": "running"})
        await asyncio.sleep(5)


@activity.defn
async def run_micro(job: Job) -> Result:
    if (
        not isinstance(job.namespace, str)
        or not 1 <= len(job.namespace) <= 256
        or not isinstance(job.operation_id, str)
        or not 1 <= len(job.operation_id) <= 256
        or not isinstance(job.items, list)
        or type(job.max_rounds) is not int
        or not 1 <= job.max_rounds <= 8
        or len(job.items) > 128
        or any(not isinstance(item, str) or len(item) > 256 for item in job.items)
    ):
        raise ApplicationError(
            "Invalid processing request", type="InvalidInput", non_retryable=True
        )

    # Business idempotency keys use operation_key, without a Temporal attempt.
    operation_key = digest({"deployment": os.environ.get("MEMSPINE_DEPLOYMENT", "development"),
                            "namespace": job.namespace, "operation": job.operation_id,
                            "pipeline": "micro-v1"})
    request_fingerprint = digest(asdict(job))
    info = activity.info()
    # A failed local promise is terminal. Each Activity attempt gets a new root;
    # the local graph replays, reusing committed application step outputs.
    invocation_id = digest([operation_key, info.workflow_run_id, info.activity_id, info.attempt])
    local = new_durable_local_runtime(
        os.environ.get("MEMSPINE_MICRO_JOURNAL_PATH", "local_resonate.db"),
        operation_key, invocation_id, request_fingerprint,
    )  # One owned runtime per Activity attempt; persistent journal shared across retries.
    try:
        async with asyncio.TaskGroup() as tasks:
            heartbeat = tasks.create_task(keep_heartbeating(operation_key))
            try:
                async with asyncio.timeout(100):
                    handle = local.options(timeout=timedelta(seconds=90)).run(
                        invocation_id, "journaled-micro-pipeline-v1", job.items, job.max_rounds
                    )
                    output = await handle.result()
                    return Result(
                        job.namespace,
                        job.operation_id,
                        output["items"],
                        digest({"job": asdict(job), "items": output["items"], "rounds": output["rounds"]}),
                        output["rounds"],
                    )
            finally:
                heartbeat.cancel()
    finally:
        # SDK stop drains execution tasks; real leaves must have bounded I/O
        # deadlines and cooperative cancellation. It cannot kill a stuck thread.
        await local.stop()


@activity.defn
async def verify_result(job: Job, result: Result) -> Result:
    expected = digest({"job": asdict(job), "items": result.items, "rounds": result.rounds})
    if (
        result.namespace != job.namespace
        or result.operation_id != job.operation_id
        or type(result.rounds) is not int
        or not 1 <= result.rounds <= job.max_rounds
        or expected != result.digest
    ):
        raise ApplicationError("Result integrity failure", type="InvalidResult", non_retryable=True)
    return result
```

### workflows.py

```python
from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from .contracts import Job, Result


@workflow.defn
class ProcessingWorkflow:
    @workflow.run
    async def run(self, job: Job) -> Result:
        result = await workflow.execute_activity(
            "run_micro",
            job,
            result_type=Result,
            start_to_close_timeout=timedelta(minutes=3),
            schedule_to_close_timeout=timedelta(minutes=15),
            heartbeat_timeout=timedelta(seconds=20),
            cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=2),
                maximum_interval=timedelta(seconds=30),
                maximum_attempts=5,
            ),
        )
        return await workflow.execute_activity(
            "verify_result",
            args=[job, result],
            result_type=Result,
            start_to_close_timeout=timedelta(seconds=15),
            schedule_to_close_timeout=timedelta(minutes=1),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
```

### worker.py

```python
from __future__ import annotations

import asyncio
import os
from datetime import timedelta

from temporalio.client import Client
from temporalio.worker import Worker

from .activities import run_micro, verify_result
from .workflows import ProcessingWorkflow


async def main() -> None:
    tls = os.environ.get("TEMPORAL_TLS", "false").lower() == "true"
    if os.environ.get("APP_ENV") == "production" and not tls:
        raise RuntimeError("Configure Temporal TLS/authentication for production")
    if os.environ.get("APP_ENV") == "production" and not os.environ.get("MEMSPINE_MICRO_JOURNAL_PATH"):
        raise RuntimeError("Configure the journal path on a persistent worker volume")
    client = await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
        tls=tls,
        api_key=os.environ.get("TEMPORAL_API_KEY"),
    )
    worker = Worker(
        client,
        task_queue=os.environ.get("TEMPORAL_TASK_QUEUE", "memspine-processing-v1"),
        workflows=[ProcessingWorkflow],
        activities=[run_micro, verify_result],
        max_concurrent_activities=8,
        graceful_shutdown_timeout=timedelta(seconds=30),
    )
    await worker.run()  # Activities create and close strictly local Resonate runtimes.


if __name__ == "__main__":
    asyncio.run(main())
```

## 6. DBOS migration mapping

Existing applications may use:

```python
@DBOS.workflow()
def process(job_id):
    source = read_source(job_id)       # @DBOS.step()
    candidates = infer(source)        # @DBOS.step()
    return deposit(job_id, candidates)  # @DBOS.step()
```

Replace the macro boundary with a Temporal Workflow, put a bounded local pipeline
inside its processing Activity, and map internal step calls explicitly:

```python
# Inside the Resonate coordinator, itself executed by a Temporal Activity:
source = await ctx.run(read_source_leaf, source_reference)
candidates = await ctx.run(infer_leaf, source, model_revision)
# The deposit can be another Temporal Activity when its durable macro boundary
# matters. It must deduplicate using an attempt-independent operation key.
```

| DBOS responsibility | Replacement | Migration requirement |
|---|---|---|
| `@DBOS.workflow()` top-level state | `@workflow.defn` / `@workflow.run` | Rewrite scheduling, time and waits to deterministic Workflow APIs |
| `@DBOS.step()` external operation | Temporal Activity or Resonate leaf inside an Activity | Choose boundary by independent recovery/visibility needs |
| DBOS SQLite system DB | Temporal durable history; optional separate local journal | No history compatibility or automatic checkpoint translation |
| DBOS automatic recovery | Temporal retry + chosen local recovery policy | Test actual process death and ownership transfer |
| DBOS decorator at pipeline layer | Thin orchestration adapters | Domain pipelines stay framework-free |

The current MemSpine DBOS adapter checkpoints whole pipeline invocations, not
serialized internal PipelineContext steps. Its dispatch function rebuilds context
from a factory. Preserve that separation: the migration is not a mechanical
replacement of decorators on live-context functions.

## 7. Implementation sequence and exit gates

| Step | Directive | Exit gate |
|---|---|---|
| ACD-01 | DONE: owner chose B | Application-owned SQLite journal; no native SDK durability claim |
| ACD-02 | DONE: typed settings, optional extra and locked SDKs | No SDK imports in core/domain pipelines; strict settings, lifecycle and capabilities |
| ACD-03 | DONE for maintenance invocation: identity/config/input fingerprints | Namespace/source identity, immutable revisions, input binding and authorization checked |
| ACD-04 | Add durable outbox-to-Temporal submission | Crash between accept and dispatch loses no job; duplicate starts reconcile |
| ACD-05 | DONE for registered maintenance pipeline Workflows and deployment queues | Replay test passes; no I/O/model/client construction inside Workflow |
| ACD-06 | DONE: bounded local Resonate Activity | Local-only transport verified; cancellation/deadlines/retry budgets work |
| ACD-07 | Integrate ingestion model work and result deposits | Exact provenance, historical source time, sender identity and idempotent graph/fact writes preserved |
| ACD-08 | DONE locally: canonical journal wired into active maintenance runner | Completed-step reuse proven under process death and ownership transfer; no unproven SQLite capability |
| ACD-09 | Drain and retire DBOS workers | No dual ownership; all remaining jobs finish, are explicitly cancelled/compensated, or restart from application checkpoints |
| ACD-10 | DONE: DBOS removed from selectors, extra and active wiring | Breaking change, no aliases/RETIRED map; historical documents/results remain historical |
| ACD-11 | PARTIAL: live maintenance retry/reopen and journal process-exit gates pass | Gates below passed with real Temporal and selected persistence policy |
| ACD-12 | Deploy with observable canary and bounded concurrency | Cluster permissions, capacity, recovery and rollout evidence recorded |

Do not treat the current TaskRunner protocol as a drop-in serializable transport.
Its `run(name, ctx)` accepts live context and returns completion stats. A remote
submission boundary needs job references and a receipt/handle contract; the Worker
constructs a fresh context. Keep foreground ingestion acceptance separate from
waiting for multi-day orchestration.

## 8. Required focused tests and operational controls

- Positive: local runtime construction, async leaf execution, dynamic branching,
  metadata/schema rejection, sandbox imports, result validation and repeated inputs.
- Kill the Activity process after completed step N; retry on another Worker.
  For A expect recomputation; for B/D prove only committed valid checkpoints reuse.
- Crash after a business write before Activity completion; assert one logical
  deposit even when an Activity attempt runs twice.
- Interrupt during an LLM call; record possible duplicate provider cost and unknown
  outcome rather than asserting exactly-once inference.
- Remove/recreate a container or volume; verify expected data-loss/fallback behavior.
- Force stale-owner overlap, heartbeat loss, cancellation, shutdown, SQLite lock
  contention and exhausted retry/iteration budgets. Acknowledged timeout is not fencing.
- Deploy changed Workflow/micro-step code against old histories/checkpoints;
  use versioned workflow implementations and immutable pipeline revisions.
- Correlate deployment, namespace, workflow/run/activity/attempt, operation key,
  micro invocation and artifact references. Do not log raw memory or credentials.
- Bound model work separately from coroutine count; use thread/process/GPU
  executors for blocking work without starving heartbeat or workflow loops.
- Multi-day waits remain Temporal timers/signals; use Continue-As-New where history
  growth warrants it. Never keep a local Activity process alive for days to wait.

The included verification tool uses Temporal ActivityEnvironment plus real local
Resonate and validates Workflow sandbox preparation. It does **not** exercise
Temporal Server dispatch, live multi-worker failover, volume reattachment or
production readiness. Separate journal tests prove SQLite committed-result reuse,
subprocess-exit recovery and concurrent/stale-claim fencing:

```powershell
$env:PYTHONPATH = "docs/examples"
uv tool run --from uv==0.13.1 uv run --isolated --no-project `
  --with temporalio==1.34.0 --with resonate-sdk==0.8.1 `
  python -m temporal_resonate.verify_template
```

Scope: do not block this work on unrelated historical evaluation-arm migrations.
Test changed ingestion/orchestration boundaries; broaden only when a change or
new failure creates a concrete dependency.

## 9. Evidence and sources

- [Verified SDK package](https://pypi.org/project/resonate-sdk/0.8.1/).
- [Resonate async Python guide](https://docs.resonatehq.io/develop/python): currently
  labels its examples 0.7.4, older than the installed 0.8.1 inspected here.
- [Resonate constraints](https://docs.resonatehq.io/develop/constraints): conceptual
  replay rules apply, but some Python examples use older generator APIs; do not
  copy their obsolete `ctx.time`/`ctx.random` examples into the current template.
- [Temporal Workflow definitions](https://docs.temporal.io/workflow-definition).
- [Temporal errors, retries and heartbeat handling](https://docs.temporal.io/develop/python/best-practices/error-handling).
- [Temporal Python SDK](https://github.com/temporalio/sdk-python).
- [Historical source/signature baseline](provider_verification/temporal-resonate-template.json).
- [Current journal + SDK example evidence](provider_verification/temporal-resonate-journal-template.json).
- [Journal crash/fencing tests](provider_verification/step-journal-tests.log).

The literal requirement of native embedded SQLite Resonate plus seamless exact
sub-step recovery remains unfulfilled by the verified SDK. Do not erase this gate
from the implementation directive or replace it with a fabricated constructor.
