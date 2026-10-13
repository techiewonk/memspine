# CPU golden

A deterministic end-to-end run of the engine that guards every pure-move PR of the restructure
(`docs/v2/RESTRUCTURE.md`, section 2). No model, no GPU, no network; about 7 s on a laptop CPU.

## What it covers

`fixtures/conversations.json` holds four short synthetic conversations (invented names only; no benchmark
text) and 14 queries:

- two single-user conversations (Ilse, Bram) with several dated sessions, one fact that changes over time
  (Ilse moves from Utrecht to Rotterdam, written both as chat and as a keyed semantic fact), a list (herbs,
  food), and relative time phrases ("next week", "until Friday");
- one multi-party chat (Noor and Tomasz, one namespace, speaker named in the turn text), queried with and
  without a `viewer`;
- one non-English (German) conversation;
- queries: single fact, current value of a changed fact, as-of read before the change, lists, temporal,
  relative time, multi-party with viewer, non-English, and two questions with no answer.

The harness (`harness.py`) builds the engine with the `core` template, `embedding.provider: hash`,
`read.rerank: off`, no LLM and a temporary SQLite file; ingests with `write_messages` and `write`; runs
`Engine.sleep()` (the model-free offline pass; model stages report `skipped`); then for every query runs
`search` (ordered hits and scores) and `read` in `retrieve` and `compose` modes.

The snapshot (`snapshots/cpu_golden.json`) records:

- per namespace, record counts per memory type after ingest and after the offline pass;
- the status of every offline stage;
- per query: the ordered `search` hit list (stable record label, memory type, status, event time, content
  hash, score rounded to 6 decimals) and, per read mode, the chosen mode, abstention flag, tokens used,
  ordered context records and the sha256 of the composed context text.

Normalisation: uuid4 record ids become labels `r001`, `r002`, ... in ingest order, then by first
appearance for records created by the offline pass. Wall-clock fields (`recorded_at` and similar) are not
snapshotted; event times come from the fixture's explicit dates. Content is stored as a hash prefix, so
the snapshot is compact but any text change shows.

## Running

```
PYTHONPATH=src pytest tests/golden -m golden
```

CI (X-02) and move PRs call the wrapper, which sets `PYTHONPATH`, `-o addopts=""` and the marker (set
`PYTHON=<venv python>` to choose the interpreter; extra arguments go to pytest):

```
tests/golden/run_golden.sh
```

The test runs the scenario twice in fresh temp directories and asserts that the two runs are equal and
that they equal the committed snapshot, printing a unified diff on mismatch. The `golden` marker is
registered in `tests/golden/conftest.py`, not in `pyproject.toml`.

## Moving modules: update the frozen clock in the same PR

The harness freezes time by replacing the `datetime` name inside the modules listed in `_CLOCK_MODULES`
(`tests/golden/harness.py`): today `memspine.core.policies.scoring`, `memspine.core.policies.decay`,
`memspine.engine`, `memspine.derive.pipeline.integrate` and the `memspine.derive.pipeline.stages` modules that call
`datetime.now()` (communities, consolidate, entity_summaries, maintenance, mining, retention, watches). `importlib.import_module` fails loudly when a listed
module no longer exists, but a moved module that still calls `datetime.now()` and is missing from the list
would silently run on the wall clock and make the golden flaky. A move PR must therefore, in the same
commit, rename the entries to the new module paths and add every new module that calls `datetime.now()`
or `datetime.utcnow()` (grep the moved code for `datetime` before finishing). When `engine.py` is split,
each target module that holds a clock call needs its own entry.

## Regenerating

```
PYTHONPATH=src python tests/golden/regen.py
git diff tests/golden/snapshots/cpu_golden.json
```

Regenerate only on purpose, and commit the snapshot diff with the change that caused it.

## When a diff is legitimate

- Pure move or refactor PR: never. Any diff (hit order, a score digit, a context hash, a count) is a
  regression; fix the code.
- Behaviour change built behind a flag that is off by default: no diff is expected.
- A deliberate default change, a new default-on layer or a fixture change: regenerate, review the diff
  line by line, and say in the PR description why each changed query changed.

Changing the fixture or the harness settings (top_k, budget, modes) changes the whole snapshot; do that
in its own PR.

## Known limits

- The hash embedder is lexical in spirit, so scores check plumbing and ordering, not semantic quality.
- LLM-backed stages (extraction, graph, reflect) are off, so they are not covered here.
- Engine log lines carry wall-clock timestamps; they go to stderr and are not part of the snapshot.
