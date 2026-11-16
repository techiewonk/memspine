# Python 3.13 dependency compatibility and freshness

## PA-109 native inference follow-up (2026-11-16)

After the PA-113 checkpoint, actual native inference passed on clean commit
`8a07d714b80443e8f14616c9729732b216625a18` with both Windows/Python 3.13.9
source-built llama-cpp-python 0.3.36 and 0.3.37. This supersedes the earlier
native-inference limitation for this bounded CPU smoke scope only.

The existing Ollama `gemma2:2b` GGUF was read without downloading or modifying it.
Its 1,629,509,152 bytes match manifest/model SHA-256
`7462734796d67c40ecec2ca98eddf970e171dbb6b370e43fd633ee75b69abe1b`.
The real MemSpine provider builder used CPU-only inference with two threads,
512 context tokens, 64-token batches and at most 24 generated tokens per call.
Each package version returned `4`, `Paris`, and `6` to three fixed synthetic
prompts. Two roles shared one native Llama handle; closing the first left the
second usable, the closed holder rejected inference, and final lifecycle
cleanup released the native handle. Loaded MemSpine source paths were checked.

[Execution, scripts, outputs, model provenance, native DLL hashes and revision record](provider_verification/checkpoints/2026-11-16-pa109/index.json)
preserve the evidence. The first harness attempt blocked Windows asyncio's
local socket-pair setup before provider construction; its failed log is retained.
The corrected harness installs its socket-connect denial after event-loop setup
and before provider construction. Both subsequent environments passed. After event-loop setup and before provider
construction, the hook was configured to raise on any Python `socket.connect`
attempt and remained active through inference; no such attempt occurred. This
hook does not cover earlier imports or network calls made directly by native code.

This proves one cached model's native CPU inference and lifecycle, not model
quality, general model compatibility, GPU support, throughput or deployment
acceptance. Earlier PA-113 records retain their historical scope. The lock hash
in each inference record identifies the dependency lock. The commit, clean
before/after state and loaded-source hashes identify the source revision; the
explicit package version and DLL hashes distinguish the two optional environments.

## PA-113 refresh candidate (2026-11-16)

The isolated PA-113 candidate refreshes the complete declared dependency graph
with uv 0.13.8 on CPython 3.13.9. It resolves 234 packages, the same count as
the frozen `aed6cbf3` lock: 71 version upgrades, no downgrades, additions, or
removals. The direct/build audit covers 68 packages and reports zero outdated
releases within declared constraints. All 71 upgraded releases have compatible,
non-yanked, stable files. `opentelemetry-semantic-conventions==0.66b2` is an
unchanged transitive beta required exactly by stable OpenTelemetry SDK 1.45.2;
the refresh does not add or upgrade a prerelease.

The first `uv lock --upgrade` operation reported 75 package updates. The final
old-lock-to-final-lock comparison counts distinct package **version** changes,
which are 71. The operational resolver count and final version-diff count
measure different stages; the 71-package comparison is the release audit basis.

Ladybug remains within `>=0.22.1,<0.23` to preserve the graph storage format
boundary. Hugging Face Hub remains within `>=2.2,<3` under ADR-084/085, with
2.4.0 selected. No dependency overrides were added. The Python 3.13/Windows
wheel audit excludes Linux-only NVIDIA/Triton files by their platform markers.
`llama-cpp-python==0.3.37` has no matching published Windows wheel; it built
successfully from source in a separate fresh environment with the installed
VS2019 x64 toolchain. Native import and mock lifecycle tests passed; model
inference was not exercised.

The old-lock and refreshed-lock comparison used the same 815 frozen selected
test files in 46 chunks. The old lock produced 11,631 passed, 55 failed, 249
skipped and 5 deselected; the initial refreshed-lock run produced 11,624
passed, 55 failed, 256 skipped and 5 deselected. After fixture alignment, the
refreshed-lock chunks matched the old-lock totals of 11,631 passed, 55 failed,
249 skipped and 5 deselected. Separately, 199 config tests passed under each
lock.
The candidate already included two disclosed, behavior-preserving temporal
guard consolidations required by Ruff 0.17.5; it was therefore not a
byte-identical runtime source comparison. Their original and amended focused
tests each passed 60 cases. The 55 failed cases matched exactly and are tracked
as pre-existing baseline gaps. This is a diagnostic comparison, not a clean
full-suite gate. A separate
encrypted SQLite roundtrip passed 10/10 in each fresh SQLCipher environment.
On the repaired candidate source, the 51 originally failing unit cases are
covered by a paired 303-pass selected gate, and the four ingest snapshot cases
passed separately under each lock. These bounded reruns do not replace the
original 46-chunk full comparison. The reviewed source and fixture repairs
have their own approval records; the two temporal Ruff edits are disclosed as
separate candidate changes.

With repository-owned local services, the refreshed lock passed 44 reviewed
Postgres/Mongo records projection cases, 24 additional scoped Postgres/Mongo
cases, 22 live Neo4j cases, nine mixed inline/live Temporal integration cases,
19 Temporal unit cases (including three worker-settings cases), and two
additional outbox cases: ActivityEnvironment error mapping and one real
time-skipping continue-as-new workflow. The strict six-service
provider gate passed eight cases without skips. The seven-backend resource
lifecycle gate completed five warmups and 30 measured cycles per connector and
Engine row with zero outstanding resources. The separate model extras lane
passed 89 offline selected cases under each lock, with one real model-card case
deselected; no optional heavy-model weights were downloaded or paid calls made.
The separate ingest equivalence snapshot passed four cases under each lock
using hash embeddings; its base variants retain the default ONNX reranker.
Those cases do not establish optional heavy-model inference coverage. Optional
heavy-model quality and local inference remain unverified.
Ruff and format passed on the final candidate source with the refreshed
environment. After the reviewed 49-file repair transplant, strict mypy passed
all 696 source files under each lock and the refreshed environment kept all 19
import-boundary contracts. The earlier pre-transplant mypy logs are retained as
historical evidence, separate from these final-source gates. The installed
graph passed `uv pip check`. The requested strict MkDocs build cannot run
because this repository has no `mkdocs.yml` configuration.

The corrected RM-96 full-size benchmark used identical candidate source under
the old-lock and refreshed-lock interpreters, sequentially on a quiet owned
host. Its deterministic fixture contained 100,000 compiled edges plus 20,000
mention edges (120,000 namespace edges), with 20,000 requested entity nodes
and 39,999 actual loaded nodes per backend. Correctness parity passed before
timing for SQL adjacency, Ladybug and Neo4j; no backend was skipped. The
current filtered graph-leg p95 (140 samples per backend) was 6.618 to 5.536 ms
for SQL adjacency, 17.135 to 15.731 ms for Ladybug, and 17.770 to 18.749 ms
for Neo4j, old lock to refreshed lock. Each is within the declared 50 ms
filtered-leg budget. The active depth-one/depth-two walk p95 values were,
respectively, 4.751/26.862 to 4.845/27.087 ms for SQL adjacency,
1.360/14.337 to 1.454/11.119 ms for Ladybug, and 5.189/8.498 to
4.916/8.695 ms for Neo4j (60 samples per depth). Forced shared-reference
controls are reported separately: Neo4j depth-two shared p95 was 68.692 to
64.980 ms; historical Ladybug pre-RM-116 controls are hundreds of milliseconds.
The refreshed Neo4j depth-two pushdown control had one 232.198 ms maximum
sample among 60 (p95 8.853 ms); this tail does not alter the separate
filtered-leg budget result and remains a performance limitation.
This one paired run establishes compatibility and the stated budget, not a
statistical performance improvement. The unchanged historical benchmark cannot
exercise the pre-RM-130 seed-cache or full Engine search metrics; those remain
future current-default performance work under RM-110/RM-134/PA-053.

The first full-size old-lock attempt reached cleanup after measurement but failed
during `Neo4jGraphStore.clear()` of its 39,999-node private graph because its
single transaction exhausted the server heap. The exact private label was
subsequently removed in 400 bounded batches, with zero remaining nodes. A
two-reviewed PA-113 benchmark-only cleanup change made the fresh paired run
pass without changing the runtime store. Runtime `clear()` at this scale is a
documented native resource limitation and is not validated by the new harness.
External harness setup failures from stale historical benchmark APIs and omitted
Neo4j lifecycle wrapper settings were classified and preserved, not dependency regressions.

The table below records the current candidate direct/build resolution from the
public metadata audit. Earlier dated measurements follow it for history.

### Current direct/build package table

| Package | Locked | Latest allowed | 3.13 host wheel |
|---|---:|---:|:---:|
| aiosqlite | 0.22.1 | 0.22.1 | Yes |
| alembic | 1.20.1 | 1.20.1 | Yes |
| any-llm-sdk | 1.50.0 | 1.50.0 | Yes |
| bitsandbytes | 0.50.2 | 0.50.2 | Yes |
| boto3 | 1.44.3 | 1.44.3 | Yes |
| cashews | 7.6.0 | 7.6.0 | Yes |
| chonkie | 1.7.0 | 1.7.0 | Yes |
| datasketch | 2.0.0 | 2.0.0 | Yes |
| dishka | 1.10.1 | 1.10.1 | Yes |
| fakeredis | 2.40.0 | 2.40.0 | Yes |
| fastapi | 0.163.1 | 0.163.1 | Yes |
| fastuuid | 0.14.0 | 0.14.0 | Yes |
| flashrank | 0.2.10 | 0.2.10 | Yes |
| gliner2 | 2.1.0 | 2.1.0 | Yes |
| greenlet | 3.5.6 | 3.5.6 | Yes |
| hatchling | build-isolated | 1.33.0 | Yes |
| httpx | 0.28.1 | 0.28.1 | Yes |
| huggingface-hub | 2.4.0 | 2.4.0 | Yes |
| import-linter | 2.16 | 2.16 | Yes |
| jinja2 | 3.1.6 | 3.1.6 | Yes |
| json-repair | 0.64.9 | 0.64.9 | Yes |
| ladybug | 0.22.3 | 0.22.3 | Yes |
| lancedb | 0.41.0 | 0.41.0 | Yes |
| langmem | 0.0.30 | 0.0.30 | Yes |
| llama-cpp-python | 0.3.37 | 0.3.37 | No |
| llmlingua | 0.2.2 | 0.2.2 | Yes |
| markitdown | 0.1.9 | 0.1.9 | Yes |
| mkdocs-material | 9.8.1 | 9.8.1 | Yes |
| mkdocstrings | 1.0.8 | 1.0.8 | Yes |
| model2vec | 0.10.0 | 0.10.0 | Yes |
| mypy | 2.4.1 | 2.4.1 | Yes |
| neo4j | 6.4.1 | 6.4.1 | Yes |
| onnxruntime | 1.32.0 | 1.32.0 | Yes |
| opensearch-py | 3.2.0 | 3.2.0 | Yes |
| opentelemetry-api | 1.45.2 | 1.45.2 | Yes |
| opentelemetry-sdk | 1.45.2 | 1.45.2 | Yes |
| orjson | 3.13.0 | 3.13.0 | Yes |
| packaging | 26.3 | 26.3 | Yes |
| pgvector | 0.5.1 | 0.5.1 | Yes |
| psycopg | 3.3.9 | 3.3.9 | Yes |
| pydantic | 2.14.3 | 2.14.3 | Yes |
| pydantic-settings | 2.16.3 | 2.16.3 | Yes |
| pymongo | 4.20.0 | 4.20.0 | Yes |
| pytest | 9.2.0 | 9.2.0 | Yes |
| pytest-asyncio | 1.5.0 | 1.5.0 | Yes |
| pytest-cov | 7.1.0 | 7.1.0 | Yes |
| pyyaml | 6.0.3 | 6.0.3 | Yes |
| resonate-sdk | 0.8.1 | 0.8.1 | Yes |
| ruff | 0.17.5 | 0.17.5 | Yes |
| safetensors | 0.8.0 | 0.8.0 | Yes |
| sentence-transformers | 6.3.0 | 6.3.0 | Yes |
| sqlalchemy | 2.1.8 | 2.1.8 | Yes |
| sqlcipher3 | 0.6.5 | 0.6.5 | Yes |
| structlog | 26.1.0 | 26.1.0 | Yes |
| tantivy | 0.27.0 | 0.27.0 | Yes |
| taskiq | 0.13.0 | 0.13.0 | Yes |
| taskiq-redis | 1.2.4 | 1.2.4 | Yes |
| temporalio | 1.36.1 | 1.36.1 | Yes |
| tokenizers | 0.23.3 | 0.23.3 | Yes |
| torch | 2.15.0 | 2.15.0 | Yes |
| transformers | 5.23.0 | 5.23.0 | Yes |
| typer | 0.29.1 | 0.29.1 | Yes |
| types-pyyaml | 6.0.12.20261113 | 6.0.12.20261113 | Yes |
| tzdata | 2026.6 | 2026.6 | Yes |
| uvicorn | 0.54.0 | 0.54.0 | Yes |
| weaviate-client | 4.24.1 | 4.24.1 | Yes |
| xxhash | 4.1.0 | 4.1.0 | Yes |
| zstandard | 0.25.0 | 0.25.0 | Yes |

## Active dependency policy

Select newly added or upgraded releases from stable, non-yanked files compatible
with Python 3.13 and the complete declared graph, without dependency overrides.
The unchanged transitive OpenTelemetry beta required by its stable SDK is
disclosed above. Keep model inference optional;
the Python target remains `>=3.13`. Preserve Ladybug's `<0.23` storage-format
boundary and the active Hugging Face Hub `>=2.2,<3` range. The PA-113 raw
metadata audit is the external evidence artifact
`pa113-candidate-audit-final-source.json` (68 direct/build rows, zero outdated).
The repository-linked audit in the dated section below belongs to an earlier
lock and must not be read as the current PA-113 result.

## Earlier dependency measurements

> Orchestration update (2026-10-17): DBOS has been removed from active packaging
> and runtime. The durable runner is Temporal Server + embedded Resonate with an
> application-owned SQLite result journal, installed through `[temporal]`; see
> [ADR-071](adr/ADR-071-temporal-resonate-orchestration.md). Earlier dependency
> measurements and DBOS comparisons below are historical, not current setup instructions.


Provider replacement refresh (RM-123/RM-124, 2026-10-23, uv 0.13.5):
fastembed and litellm are removed. Core directly declares ONNX Runtime,
tokenizers and Hugging Face Hub. `[anyllm]` adds any-llm-sdk 1.40.1 base only;
provider SDK extras are caller-selected. The `[decider]` Hub cap is now
`>=2.2.0,<3`; the universal lock resolves Hub 2.3.0 (236 packages).
The installed environment passes `uv pip check`; obsolete DeepEval was removed.
The public metadata audit reports 69 direct/build dependencies and zero outdated
entries. The table below is generated from that audit.

Audit: 2026-10-17. Target: CPython 3.13; clean validation interpreter: 3.13.5,
Windows x64. Sources: [PyPI JSON API](https://docs.pypi.org/api/json/), package
metadata and actual local installation/tests. See [ADR-070](adr/ADR-070-python313-dependency-refresh.md).

### Historical policy notes and measurements (2026-10-17)

The stable-release, no-override and optional-dependency policy above remains
active. Version counts, tool versions and validation statements below describe
the earlier refresh and are retained for provenance.

- Use current stable, non-yanked releases compatible with Python 3.13 and the
  complete declared graph. No prereleases or dependency overrides.
- Raise direct minimums to the audited baseline; exact application versions are
  reproducible in `uv.lock`. These minimums do not freeze every future release.
- Preserve optional dependencies. Torch/model inference does not enter core.
- Use uv >=0.13.1 for the current lock format; the workstation's global uv 0.8.0
  was left unchanged. Validation used isolated uv 0.13.1.
- Keep `requires-python = ">=3.13"`, `.python-version = 3.13` and existing
  Python 3.13 CI. Do not infer tested support for every later Python version.

Compared with implementation baseline `e6596f44`, the lock has **48 upgraded
package names**, **1 downgraded**, **1 added** and
**15 removed**. The downgrade is resolver-selected compatibility,
not an assertion that every transitive package can use its standalone latest.

### Historical intentional exceptions and limitations (2026-10-17/23)

| Package / area | Decision | Reason / evidence |
|---|---|---|
| Hugging Face Hub | 2.3.0; core and decider permit `<3` | The former fastembed/litellm `<2` blockers were removed by ADR-084/085. Universal lock and installed consistency checks pass; offline/cache and regression evidence is recorded with the provider replacement verification. |
| Ladybug | Latest 0.22.1 within `<0.23` | Preserve the agreed graph storage-format compatibility boundary |
| DBOS | Upgrade to 3.3.0, `<4` | Fresh-environment durable resume and runner parity passed; existing whole-pipeline recovery semantics remain |
| GLiNER2 | Upgrade to 2.1.0 with `[local]`, `<3` | Publisher metadata permits 3.13 and Transformers 5.x; weights/inference were not run |
| GLiNER2 base-only package | Base-only import reached a torch import | An isolated base-only smoke check failed without torch; our selected `[local]` includes torch. This is not proof of the full local-inference stack |
| llama-cpp-python | Latest 0.3.36 remains optional | No matching Windows x64/CPython 3.13 wheel found on PyPI; native toolchain build/runtime validation remains open |
| DeepEval | Remove unused development dependency | No source/harness imports; its Click `<8.4` conflicts with Hub's Click `>=8.4.2`. Removing it also removes the override; no dependency is forcibly ignored |
| Native/model extras | Metadata and lock resolution are separate from runtime proof | GPU, model quality, SQLCipher/native builds and every optional platform were not exercised |

### Historical verification (2026-10-17)

| Gate | Result |
|---|---|
| Public metadata audit | 66 direct/build packages; all selected release files declare a Python range including 3.13; zero outdated direct lock entries within constraints |
| Universal lock | 244 resolved packages; no Click override; lock check passed with uv 0.13.1 |
| Clean environment installation | 169 installed distributions, selected core/dev/provider extras, Python 3.13.5 |
| Installed dependency consistency | `uv pip check`: all installed packages compatible |
| Config/providers/runtime/workers/decision/combinations/runner tests | 581 passed, 5 skipped |
| Separate DBOS 3.3 durable resume and runner parity | 2 passed |
| Separate REST suite | 64 passed |
| Ruff | Passed with 0.17.1 |
| Mypy | Passed across 606 source files with 2.4.0; adjusted distinct SQLAlchemy row variables |
| Provider defaults generation | Reference remains current |
| Expanded ingestion/evaluation batch | Initially 1,822 passed, 16 skipped, 186 failed and 7 errors; all failures/errors are evaluation tests |
| Failed-case comparison in the original environment | 185 failures and 7 errors reproduced; one differing case exposed the optional community timestamp bug |
| Community timestamp correction | 24 targeted tests passed, including the formerly failing historical bundle check |
| Wheel | Built successfully with the final source and refreshed build dependency |

The community package is installed in the clean environment but absent from the
original environment. It enabled community summary creation and exposed an existing
missing source-time assignment, rather than a package incompatibility. Summary
`valid_from` now uses the latest member timestamp; `recorded_at` retains derivation
time. A regression assertion and the historical bundle check pass.

**Remaining release gap:** 185 evaluation failures and seven errors reproduce in
the original environment. Old provider keys/missing current fields are among the
observed causes. Per the owner, unrelated evaluation work is deferred while ingestion is enhanced.
These are tracked as PA-107; their tests/generators require a
separate migration. Historical results must remain immutable. The full expanded
suite was not rerun after the community fix, so it is not labelled green.
Durable, sanitized evidence: [dependency-validation.json](provider_verification/dependency-validation.json).

The independent counts overlap; do not add them as unique total coverage.
The previous provider native-container/performance reports used the older lock.
They remain historical and were not silently relabelled as refreshed-lock evidence.
No production service, paid API or model-weight download was used here.

### Historical commands (2026-10-17)

These reproduce the earlier uv 0.13.1 validation. PA-113 used uv 0.13.8 and
fresh isolated environments, as recorded in its separate evidence package.

```powershell
uv tool run --from uv==0.13.1 uv lock --upgrade
uv tool run --from uv==0.13.1 uv lock --check
uv tool run --from uv==0.13.1 uv sync --locked --extra dev
uv tool run --from uv==0.13.1 uv run --locked --extra dev python scripts/audit_dependencies.py --check
uv tool run --from uv==0.13.1 uv pip check
```

For a clean isolated install, set `UV_PROJECT_ENVIRONMENT` to a new temporary
folder before syncing; do not reuse or overwrite another task's environment.
When using that override, pass its Python executable explicitly to `uv pip check --python`.
Add each required optional `--extra` explicitly. Full native/model extras may
need build tools or large runtime dependencies. An all-extras lock resolution is
not an all-extras installation test.

Refresh the audit after a lock change. Review constraints and release notes,
run affected contracts, then broader release gates if required. CI uses
`uv sync --locked` and installed-dependency checks; no recurring scheduler was added.

### Historical direct/build package table (2026-10-17)

Latest means stable non-yanked release files whose `Requires-Python` includes
3.13. Wheel availability is for the audit host only. Build-isolated Hatchling
is not an application lock entry. The raw JSON records sources, file names,
query time and manifest/lock hashes for that earlier lock:
[dependency-audit.json](provider_verification/dependency-audit.json).

| Package | Locked version(s) | Latest allowed | Latest for 3.13 | Host wheel |
|---|---|---|---|---|
| aiosqlite | 0.22.1 | 0.22.1 | 0.22.1 | Yes |
| alembic | 1.20.1 | 1.20.1 | 1.20.1 | Yes |
| any-llm-sdk | 1.40.1 | 1.40.1 | 1.40.1 | Yes |
| bitsandbytes | 0.50.2 | 0.50.2 | 0.50.2 | Yes |
| boto3 | 1.43.118 | 1.43.118 | 1.43.118 | Yes |
| cashews | 7.6.0 | 7.6.0 | 7.6.0 | Yes |
| chonkie | 1.7.0 | 1.7.0 | 1.7.0 | Yes |
| datasketch | 2.0.0 | 2.0.0 | 2.0.0 | Yes |
| dishka | 1.10.1 | 1.10.1 | 1.10.1 | Yes |
| fakeredis | 2.40.0 | 2.40.0 | 2.40.0 | Yes |
| fastapi | 0.161.1 | 0.161.1 | 0.161.1 | Yes |
| fastuuid | 0.14.0 | 0.14.0 | 0.14.0 | Yes |
| flashrank | 0.2.10 | 0.2.10 | 0.2.10 | Yes |
| gliner2 | 2.1.0 | 2.1.0 | 2.1.0 | Yes |
| graspologic-native | 1.3.1 | 1.3.1 | 1.3.1 | Yes |
| greenlet | 3.5.6 | 3.5.6 | 3.5.6 | Yes |
| hatchling | build-isolated | 1.32.4 | 1.32.4 | Yes |
| httpx | 0.28.1 | 0.28.1 | 0.28.1 | Yes |
| huggingface-hub | 2.3.0 | 2.3.0 | 2.3.0 | Yes |
| import-linter | 2.15 | 2.15 | 2.15 | Yes |
| jinja2 | 3.1.6 | 3.1.6 | 3.1.6 | Yes |
| json-repair | 0.64.2 | 0.64.2 | 0.64.2 | Yes |
| ladybug | 0.22.1 | 0.22.1 | 0.22.1 | Yes |
| lancedb | 0.40.0 | 0.40.0 | 0.40.0 | Yes |
| langmem | 0.0.30 | 0.0.30 | 0.0.30 | Yes |
| llama-cpp-python | 0.3.36 | 0.3.36 | 0.3.36 | No |
| llmlingua | 0.2.2 | 0.2.2 | 0.2.2 | Yes |
| markitdown | 0.1.9 | 0.1.9 | 0.1.9 | Yes |
| mkdocs-material | 9.7.7 | 9.7.7 | 9.7.7 | Yes |
| mkdocstrings | 1.0.8 | 1.0.8 | 1.0.8 | Yes |
| model2vec | 0.10.0 | 0.10.0 | 0.10.0 | Yes |
| mypy | 2.4.0 | 2.4.0 | 2.4.0 | Yes |
| neo4j | 6.4.1 | 6.4.1 | 6.4.1 | Yes |
| onnxruntime | 1.31.0 | 1.31.0 | 1.31.0 | Yes |
| opensearch-py | 3.2.0 | 3.2.0 | 3.2.0 | Yes |
| opentelemetry-api | 1.45.1 | 1.45.1 | 1.45.1 | Yes |
| opentelemetry-sdk | 1.45.1 | 1.45.1 | 1.45.1 | Yes |
| orjson | 3.13.0 | 3.13.0 | 3.13.0 | Yes |
| packaging | 26.3 | 26.3 | 26.3 | Yes |
| pgvector | 0.5.1 | 0.5.1 | 0.5.1 | Yes |
| psycopg | 3.3.6 | 3.3.6 | 3.3.6 | Yes |
| pydantic | 2.14.1 | 2.14.1 | 2.14.1 | Yes |
| pydantic-settings | 2.16.1 | 2.16.1 | 2.16.1 | Yes |
| pymongo | 4.19.0 | 4.19.0 | 4.19.0 | Yes |
| pytest | 9.1.1 | 9.1.1 | 9.1.1 | Yes |
| pytest-asyncio | 1.4.0 | 1.4.0 | 1.4.0 | Yes |
| pytest-cov | 7.1.0 | 7.1.0 | 7.1.0 | Yes |
| pyyaml | 6.0.3 | 6.0.3 | 6.0.3 | Yes |
| resonate-sdk | 0.8.1 | 0.8.1 | 0.8.1 | Yes |
| ruff | 0.17.2 | 0.17.2 | 0.17.2 | Yes |
| safetensors | 0.8.0 | 0.8.0 | 0.8.0 | Yes |
| sentence-transformers | 6.2.0 | 6.2.0 | 6.2.0 | Yes |
| sqlalchemy | 2.1.6 | 2.1.6 | 2.1.6 | Yes |
| sqlcipher3 | 0.6.4 | 0.6.4 | 0.6.4 | Yes |
| structlog | 26.1.0 | 26.1.0 | 26.1.0 | Yes |
| tantivy | 0.27.0 | 0.27.0 | 0.27.0 | Yes |
| taskiq | 0.13.0 | 0.13.0 | 0.13.0 | Yes |
| taskiq-redis | 1.2.4 | 1.2.4 | 1.2.4 | Yes |
| temporalio | 1.34.0 | 1.34.0 | 1.34.0 | Yes |
| tokenizers | 0.23.3 | 0.23.3 | 0.23.3 | Yes |
| torch | 2.14.1 | 2.14.1 | 2.14.1 | Yes |
| transformers | 5.20.2 | 5.20.2 | 5.20.2 | Yes |
| typer | 0.27.3 | 0.27.3 | 0.27.3 | Yes |
| types-pyyaml | 6.0.12.20260906 | 6.0.12.20260906 | 6.0.12.20260906 | Yes |
| tzdata | 2026.5 | 2026.5 | 2026.5 | Yes |
| uvicorn | 0.54.0 | 0.54.0 | 0.54.0 | Yes |
| weaviate-client | 4.24.0 | 4.24.0 | 4.24.0 | Yes |
| xxhash | 4.0.2 | 4.0.2 | 4.0.2 | Yes |
| zstandard | 0.25.0 | 0.25.0 | 0.25.0 | Yes |

GLiNER2.5 adoption is assessed separately in the [ingestion plan](GLINER25_INGESTION_PLAN.md).

Provider replacement verification: [RM-123/RM-124 evidence](provider_verification/rm123-rm124-2026-10-23.json).
Hub 2.3.0 offline/cache checks: 75 passed, 2 skipped. Strict mypy passes all 703
source files, import contracts are 18/0, and the final eval failure set contains
187 existing failures against 193 on d9a87db6, with zero additions.
