# ADR-070: current compatible dependencies on Python 3.13

Date: 2026-10-17. Status: accepted by the owner's request to use current
dependencies supporting Python 3.13. Decision register: D-92.

## Decision

Keep Python 3.13 as the development and CI target. Refresh the complete uv lock
and raise direct dependency minimums to the audited stable compatible releases.
Do not select prereleases or yanked release files. Latest means the newest stable
release satisfying the whole dependency graph, not independent versions that
cannot coexist. Preserve optional extras; do not move model inference into core.

Upgrade DBOS to its 3.x line and GLiNER2 to its 2.x line, with the existing runner
and adapter regression gates. Keep Ladybug's storage-format minor cap. Keep
Hugging Face Hub below 2 because current sentence-transformers declares that cap.
All these constraints remain explicit published metadata, not uv-only overrides.

> **Superseded (DBOS item only):** the DBOS 3.x upgrade is superseded by
> [ADR-071](ADR-071-temporal-resonate-orchestration.md), which removed DBOS from active
> code, dependencies and configuration in favour of Temporal + embedded Resonate.

> **Superseded (Hub item, 2026-10-23):** [ADR-084](ADR-084-own-onnx-embedder-and-reranker.md)
> and [ADR-085](ADR-085-own-openai-compatible-client-and-anyllm-extra.md) removed
> the core dependencies that imposed the Hub `<2` cap. The active core/decider
> range is `huggingface-hub>=2.2,<3`; the PA-113 lock selects 2.4.0. The PA-113
> lock also retains the unchanged `opentelemetry-semantic-conventions==0.66b2`
> required exactly by stable OpenTelemetry SDK 1.45.2; no new prerelease was
> selected during the refresh. See the dated dependency report for its audit.

Remove the unused DeepEval development dependency and the Click override it
required. The source and evaluation harness do not import DeepEval; the custom
reader/judge harness remains unchanged. Installing an incompatible package and
forcing its dependency to a forbidden version is not compatibility verification.
Any future DeepEval integration needs its own compatible environment and evidence.

Use a fresh Python 3.13 environment to install and check the refreshed dependency
graph. CI installs the committed lock with `uv sync --locked`, then checks
installed requirements. Public PyPI metadata audits are repeatable through
`scripts/audit_dependencies.py`; they supplement runtime tests, not replace them.

## Evidence boundaries

`Requires-Python` and wheel tags establish publisher metadata and artifact
availability. They do not prove every optional feature, platform or model works.
Source-only native packages need a supported build toolchain and separate tests.
Do not download model weights just to claim an optional inference test passed.

The current package table, verification scope, exceptions and update commands
are recorded in [the dependency report](../PYTHON313_DEPENDENCIES.md).
Earlier provider test/performance reports remain historical evidence for their
original lockfile, not automatic proof for this refreshed environment.

> **PA-113 verification update (2026-11-16):** the isolated full-graph refresh
> passed paired repaired-case, owned-service and corrected current-graph
> benchmark gates documented in the dependency report. The benchmark used the
> same candidate source under both locks and covered current graph walks and
> filtered graph-leg selection. It did not exercise the historical seed-cache
> or full Engine search timing path. Its bounded private-label cleanup does not
> establish that the runtime Neo4j `clear()` can delete a 39,999-node graph in
> one transaction; the original attempt hit a server heap limit.
