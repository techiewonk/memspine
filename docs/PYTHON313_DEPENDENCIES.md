# Python 3.13 dependency compatibility and freshness

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

## Current dependency policy

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

## Intentional exceptions and limitations

| Package / area | Decision | Reason / evidence |
|---|---|---|
| Hugging Face Hub | 2.3.0; core and decider permit `<3` | The former fastembed/litellm `<2` blockers were removed by ADR-084/085. Universal lock and installed consistency checks pass; offline/cache and regression evidence is recorded with the provider replacement verification. |
| Ladybug | Latest 0.21.2 within `<0.22` | Preserve the agreed graph storage-format compatibility boundary |
| DBOS | Upgrade to 3.3.0, `<4` | Fresh-environment durable resume and runner parity passed; existing whole-pipeline recovery semantics remain |
| GLiNER2 | Upgrade to 2.1.0 with `[local]`, `<3` | Publisher metadata permits 3.13 and Transformers 5.x; weights/inference were not run |
| GLiNER2 base-only package | Base-only import reached a torch import | An isolated base-only smoke check failed without torch; our selected `[local]` includes torch. This is not proof of the full local-inference stack |
| llama-cpp-python | Latest 0.3.36 remains optional | No matching Windows x64/CPython 3.13 wheel found on PyPI; native toolchain build/runtime validation remains open |
| DeepEval | Remove unused development dependency | No source/harness imports; its Click `<8.4` conflicts with Hub's Click `>=8.4.2`. Removing it also removes the override; no dependency is forcibly ignored |
| Native/model extras | Metadata and lock resolution are separate from runtime proof | GPU, model quality, SQLCipher/native builds and every optional platform were not exercised |

## Verification

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

## Repeatable commands

Run from the repository using current uv (the isolated tool prefix below also
works when the workstation's global uv is older):

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

## Complete direct/build package table

Latest means stable non-yanked release files whose `Requires-Python` includes
3.13. Wheel availability is for the audit host only. Build-isolated Hatchling
is not an application lock entry. The raw JSON records sources, file names,
query time and manifest/lock hashes: [dependency-audit.json](provider_verification/dependency-audit.json).

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
| ladybug | 0.21.2 | 0.21.2 | 0.21.2 | Yes |
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
