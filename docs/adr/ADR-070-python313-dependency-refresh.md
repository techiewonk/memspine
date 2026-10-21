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
