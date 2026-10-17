# Provider deployment checks and service execution

This runbook separates infrastructure evidence from model quality and actual
deployment approval. It does not authorize database copying or re-embedding.

## Inspect a running deployment

Use the Engine that your application has already started. Inspection does not
open another Engine, ingest messages, call a model, migrate data or run cleanup:

```python
from dataclasses import asdict
from memspine.providers.deployment import inspect_deployment

report = await inspect_deployment(engine)
print(asdict(report))  # allowlisted metadata; no native settings or memory text
```

`infrastructure_ready` means the selected infrastructure responds and projection
offsets are caught up. `production_profile` only reports whether
`runtime.environment` is `production`. `production_validated` remains false:
the library cannot infer production permissions, restore/failover behavior,
capacity or model quality from a health check. `unchecked` lists those dimensions.

For a real deployment, record its source/config revisions, service versions,
permission checks and workload results alongside this report. Use synthetic
records in a dedicated test namespace only when writes are explicitly approved.
Budgeted model quality/connectivity checks are separate opt-in work; no paid
probe is hidden in startup or health inspection.

## Execute non-projection changes through a deployment controller

`HTTPServiceChangeBackend` is a working transport adapter for LLM/reranker forward
revisions, cache coldstarts and worker handovers. It requires your actual
deployment controller. It does not replace that controller with simulated success
or implement vendor-specific restart/lease transfer itself.

```python
import os
import httpx
from memspine.providers.http_service_changes import HTTPServiceChangeBackend
from memspine.providers.service_changes import build_service_change_strategies

async with httpx.AsyncClient(
    base_url=os.environ["MEMSPINE_CHANGE_CONTROL_URL"],
    headers={"Authorization": "Bearer " + os.environ["MEMSPINE_CHANGE_CONTROL_TOKEN"]},
    verify=True,
) as client:
    backend = HTTPServiceChangeBackend(
        client, request_timeout_s=60.0,
        max_request_bytes=1048576, max_response_bytes=1048576,
    )
    strategies = build_service_change_strategies({
        ("llm", "forward_revision"): backend,
        ("reranker", "forward_revision"): backend,
        ("cache", "coldstart_reset"): backend,
        ("worker", "handover"): backend,
    })
    changes = engine.provider_changes(
        revision_guard=revision_guard, strategies=strategies, plan_ttl_s=900.0,
    )
    # Plan/execute/verify explicitly while the borrowed HTTP client is open.
```

Only register operations that the real controller implements and is authorized
to execute. The client owns its HTTP lifecycle; Engine does not close a borrowed
control-plane client. No shell commands, connection settings or secrets are
loaded from persisted change plans. Database/embedding migration requests are
rejected by this transport adapter.

### Wire contract

All phases use `POST <base_url>/v1/provider-changes`. Base URLs may include a
deployment-specific path prefix. Redirects are refused. The request contains:

| Field | Contract |
|---|---|
| `protocol_version` | Integer `1` |
| `phase` | `prepare`, `execute`, `verify`, `activate`, `cleanup` |
| `plan` | Persisted typed `ChangePlan`, including credential-free target config references |
| `operation` | Typed `OperationRecord`, including deployment, operation revision and fence epoch |
| `checkpoint` | Current typed checkpoint for execute; null otherwise |
| `result` | Typed verified result for activate/cleanup; null otherwise |
| `Idempotency-Key` header | Operation ID, fence epoch, operation revision and phase |

| Phase | Required HTTP 200 JSON response |
|---|---|
| prepare | `ChangePreparation` with exact owner, source/target fingerprint and bounded execution policy |
| execute | `OperationCheckpoint` whose phase confirms completed execution |
| verify | Discriminated `ChangeResult` with actual native proof and `VERIFIED` status |
| activate | `ServiceActivationReceipt`: operation/deployment/service IDs, target revision/fingerprint and boolean `activated: true` |
| cleanup | Discriminated `ChangeResult` with actual scoped cleanup proof and `CLEANED` status |

The common strategy validates category, generations, ownership and proof fields.
The HTTP adapter additionally validates the activation acknowledgement before
the coordinator can mark activation complete. Non-200 responses, redirects,
oversized requests/responses, malformed JSON and timeouts fail with sanitized
errors. Total deadlines also bound a server that keeps streaming slowly.

The remote controller must authenticate and authorize the caller; reject stale
fence epochs atomically; persist idempotency results/checkpoints; and reconcile
unknown HTTP outcomes before repeating destructive work. A new retry/fence key
is not permission to execute an already completed migration again. It must keep
admission stopped through activation and prove worker lease/ack handover where
applicable. Network acknowledgement alone is not proof of those facts.

## Hard-crash cleanup recovery

Use the typed, audited guard-backed recovery API described in
[provider operations](PROVIDER_OPERATIONS.md). The application must supply a
real quiescence verifier and maintain exclusion of the previous owner until the
recovery and retry finish. Never substitute an elapsed timeout or `true` flag.

## Evidence and open deployment prerequisites

Local contract/native-container results are in
[provider verification](provider_verification/README.md). An actual production
target/runbook and authorized workload have not been supplied in this session.
Production execution remains an environment prerequisite rather than a result
the repository's tests can manufacture.
