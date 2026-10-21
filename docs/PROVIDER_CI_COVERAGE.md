# Provider CI coverage and explicit native gate

PA-040 reviews existing jobs before adding coverage. PA-041 adds only a missing,
manual native-service gate; no remote workflow has been triggered by this change.

| Workflow/job | Installed scope | Existing/added gate | Practical boundary |
|---|---|---|---|
| `ci.yml` / check | Locked core + dev, Python 3.13 on Linux and Windows | Ruff, format, mypy and general tests | Does not provision native servers or heavy model inference |
| `ci.yml` / gates | Locked core + dev, Linux | Golden CPU, imports, prompt lint, Alembic head and flag checks | Model-free code gates |
| `ci.yml` / evals | Stub harness with separately supplied eval-only dependencies | Existing evaluation unit tests | Historical schema migration remains owner-deferred; no claim this entire job is green |
| `ci.yml` / orchestration | Core + dev + `[temporal]`, Linux and Windows | Actual optional-SDK typing, journal/runner tests and ActivityEnvironment | Serverless SDK path; live dispatch still needs an explicit test server |
| `provider-native.yml` / native-contracts | Dev + selected remote extras, Linux | **Added:** Docker services and strict native contracts, manually triggered | No production endpoints, model weights or credentials required |

The existing pytest CI commands now retain `-rs` skip reasons for future audit.
This improves evidence collection; historical aggregate skips remain unattributed.

## Native gate

```powershell
$py = 'C:/Users/hembad/AppData/Local/Temp/memspine-dependency-validation-313/Scripts/python.exe'
& $py scripts/run_provider_live_contracts.py --docker --require-all `
  --report docs/provider_verification/native-strict-local.json
```

Add `--pull-missing` only when image downloads are intended. Without strict mode,
the existing exploratory runner may report pytest skips for unavailable services.
`--require-all` instead fails if service settings are incomplete, if any contract
skips, or if pytest reports no executed passes. Failed preflight/runner outcomes
also produce a report. The report contains presence flags, not credential values.

CI pins the six service images by repository digest from the recorded local
native contract baseline. `MEMSPINE_TEST_IMAGE_<SERVICE>` overrides the test image
and disables fallback; a requested pin cannot silently become another image.
Update pins deliberately with a new runtime/version report. These are test
baselines, not claims that they are the newest releases or production choices.

Containers bind loopback-only ephemeral ports and have per-run unique names.
The runner removes only its task-owned containers in `finally`. The manual job
uses read-only repository permissions and uploads the sanitized JSON report even
when the native gate fails. A runner/host kill can bypass Python cleanup; hosted
runner destruction is the final infrastructure cleanup boundary.

The current runner covers SQL, Neo4j graph, Redis/Valkey cache, OpenSearch lexical,
and Weaviate lexical/vector operations. The separate pgvector integration contract
is additional coverage and is not silently counted in this combined gate. Native
hybrid search, paid LLM APIs, full model inference and production rollout are not
covered here.

See [verification](provider_verification/README.md),
[platform/install matrix](PROVIDER_PLATFORM_MATRIX.md) and
[the tracker](PROVIDER_TODO.md).
