# Portable verification ledger

`scripts/record_verification_run.py` appends one manually supplied gate record
per invocation to `docs/provider_verification/runs.jsonl`. Schema version 2
(ADR-087) writes a `revision` block (`commit`, `tree` = `HEAD^{tree}`, `branch`,
null when detached, and `dirty` from `git status --porcelain`, untracked files
included) and an `env` block (Python, implementation, platform, `uv.lock` and
`pyproject.toml` SHA-256, and `extras` from `--extras`, null when not stated).
It also stores the exact command text, a status, counts, optional artifact
references, a `quarantined_tests` list, and a UTC timestamp. Schema version 1
lines (`source`/`runtime` blocks) remain in the ledger as written.

The writer refuses a dirty tree with a non-zero exit. Git-reported modified,
staged, and non-ignored untracked paths all count as dirty, except the exact
file selected by `--output` when it is inside the repository. That ledger file
may remain uncommitted while chunks of one gate are appended on the same clean
commit. Commit referenced artifacts and all
other changes before recording. `--allow-dirty` writes the record anyway with
`dirty: true` and `diagnostic_only: true`; such a record cannot close a
tracker row.

It opens the ledger in append mode, preserving earlier lines. Each
artifact entry stores its repository-relative path and SHA-256 hash of the file
contents at record time.

The tool records a command; it never executes the command or captures its
output. Counts and results are supplied by the operator. Artifact references
must name existing files inside the repository and are stored as repository-
relative paths. Choose the JSONL output with `--output`; a relative output path
is resolved from the repository root. Its parent directory is created when
needed. Treat the JSONL file as single-writer: concurrent invocations are not
coordinated and can interleave.

For pytest gates, copy each `QUARANTINED ...` line from the quarantine plugin's
terminal summary into a separate `--quarantined` argument, for example
`--quarantined 'QUARANTINED xfailed tests/unit/test_flaky.py::test_case (RM-901)'`.
The JSONL record stores each node ID, outcome, and tracker row in
`quarantined_tests` (an empty list when none are supplied). The recorder checks
the supplied line format and duplicate node IDs; it cannot verify that every
quarantined test that ran was copied. Keep the complete pytest output as a
hashed artifact when recording a gate.

Example from the repository root in PowerShell:

```powershell
$py = 'C:/Users/hembad/AppData/Local/Temp/memspine-dependency-validation-313/Scripts/python.exe'
& $py scripts/record_verification_run.py `
    --gate combined-native-provider-matrix --status passed `
    --command 'python scripts/run_provider_live_contracts.py --docker --report docs/provider_verification/live-provider-refreshed-lock.json' `
    --passed 8 --failed 0 --skipped 0 `
    --artifact docs/provider_verification/live-provider-refreshed-lock.json `
    --evidence 'Refreshed combined native matrix report: 8 passed, no skips'
```

This example is a command shape, not a record of a completed gate. Use `failed`,
`skipped`, or `not_run` when that is the gate outcome. A skipped
or not-run record can include a short `--evidence` explanation. `--artifact`
and `--evidence` can each be repeated. Counts are explicit, non-negative
integers; they are not inferred from status. `passed` requires one or more
passes and no failures; `failed` requires a failure; `skipped` requires one or
more skips and no passes or failures; `not_run` requires all counts to be zero.

The writer refuses several clear credential forms in the supplied gate,
command, evidence text, and artifact references, including credential-bearing
URLs, common secret assignments, bearer strings, and recognizable provider
key prefixes. This is a small heuristic check, not comprehensive secret
detection. Review supplied text before recording it, and do not put secrets in
the command or evidence. The tool does not inspect artifact file contents.

No environment variables, shell history, command output, or other ambient
machine data are collected. The record is a portable evidence index, not proof
that the stated command ran, that the counts are accurate, or that an artifact
will remain available forever. Hashes can detect later changes to referenced
files, but do not prove who produced them or that the recorded command ran.
Keep referenced logs in a durable repository location or another controlled
evidence store before temporary directories are cleaned up.

The ADR-087 phase C baseline requires ruff, strict mypy, the unit, integration,
and combination suites in chunks, and `evals/tests` on one recorded clean
commit. The default pytest selection excludes `perf` timing tests. Live
Temporal, strict native Docker, and RM-16 checks have separate `local-service`
evidence rows. A dirty diagnostic record does not satisfy the baseline gate.
