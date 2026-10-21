# Python and platform evidence matrix

This matrix separates workflow configuration from runs actually observed during
this task. A workflow file describes an available gate; it does not prove that a
GitHub Actions run executed or passed. Evidence is current to 2026-10-17.

| Python / OS | CI configured | CI execution observed here | Installed-wheel evidence | Provider-extra evidence |
|---|---|---|---|---|
| CPython 3.13 / Ubuntu latest | `.github/workflows/ci.yml` configures the locked dev check, gates, evals and Temporal orchestration jobs. `.github/workflows/provider-native.yml` configures a manual `workflow_dispatch` job that installs cache, Postgres, Neo4j, OpenSearch and Weaviate extras, then runs disposable Docker contracts. | No GitHub Actions run was inspected in this task; configured workflows are not reported as executed or passing. | Not checked in PA-042. | The manual native job is configured, but no remote CI execution was observed. |
| CPython 3.13 / Windows latest | `.github/workflows/ci.yml` configures the locked dev check and Temporal orchestration job. | No GitHub Actions run was inspected in this task. | Local PA-042: CPython 3.13.5 x64; wheel installed into a fresh venv and Engine write/search/shutdown passed. | Local PA-043: `[opensearch]`, `[weaviate]`, and `[postgres]` installed independently; dependency checks and adapter imports passed. No backend service was contacted. |
| CPython 3.13 / macOS | No CI job identified. | Not observed. | Not checked. | Not checked. |
| CPython 3.14+ / any OS | No CI job identified; package metadata declares `requires-python >=3.13`. | Not observed. | Not checked. | Not checked. |
| Other Python implementations or CPU architectures | No CI job identified. | Not observed. | Not checked. | Not checked. |

## What the local wheel check establishes

The Windows wheel run used the supplied `memspine-0.0.1-py3-none-any.whl` with
Python 3.13.5. The installed package path resolved to the venv's `site-packages`,
`PYTHONPATH` was cleared, and the process ran from `%TEMP%`. It started an Engine
using in-memory SQLite and the deterministic hash embedder, wrote a record,
searched for it, verified the returned ID, and stopped the Engine. `uv pip check`
reported all 96 distributions compatible.

Each remote optional extra resolved and installed with the same wheel in a
separate clean venv. Installed adapter imports succeeded for OpenSearch
(`opensearch-py 3.2.0`), Weaviate (`weaviate-client 4.23.1`, lexical and vector),
and Postgres (`psycopg 3.3.6`, `pgvector 0.5.1`, storage and vector). These are
packaging/import checks only. They do not prove connection, migrations, queries,
service compatibility, failover or production readiness.

The full commands, exact installed distributions, wheel/source hashes and run
limits are recorded in [installed-wheel-checks.json](provider_verification/installed-wheel-checks.json)
and [installed-wheel-checks.log](provider_verification/installed-wheel-checks.log).

## Expanded optional local-runtime coverage (PA-111)

The fresh `[ner]` installed-wheel environment contains 107 compatible distributions.
`gliner2` 2.1.0, Torch 2.14.1 and Transformers 5.20.1 actually loaded complete
GLiNER2.5 Base and Small snapshots and ran CPU entity inference on Python 3.13.5
Windows x64. See [runtime/results](GLINER25_LOCAL_RESULTS.md). This upgrades NER
from metadata-only feasibility to observed SDK inference; it does not prove every
Engine extraction task, device or model revision. Other heavy extras such as
`[compress]`, `[st]` and `[llmlocal]` remain unvalidated as complete fresh runtime
installations here. Ladybug/community availability in the development environment
is distinct from independent clean-extra packaging evidence. macOS, Linux model
inference and Python 3.14 remain unobserved.
