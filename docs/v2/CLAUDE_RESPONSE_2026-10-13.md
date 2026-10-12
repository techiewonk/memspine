# Claude's response to the enhancement handoff (2026-10-13)

Answers to the six requests in `CLAUDE_ENHANCEMENT_HANDOFF_2026-10-13.md`. No engine code was changed; no package is
marked Done; no measurement was run.

## 1. Are ER-06, ER-24 and ER-31 consistently reflected?

Now yes. A consistency audit found 33 active statements that contradicted the confirmed amendments, plus ER-29 C
(viewer required), ER-21 B (lift, re-admit, dedupe, cap, then rerank) and ER-27 C (bounded retention), and older
shorthand (L3 default, non-blocking sleep, zero-model wording). All were corrected, and about 10 historical records were
annotated "superseded" rather than rewritten. The audit with every row and replacement is
`data/consistency_audit_2026-10-13.md`. Six further contradictions found during the edit (write.md W-12, retrieve.md
scenario 39 and p95 row, model.md layer defaults in three places, insert.md template layers, offline.md busy option)
were also fixed.

## 2. Package, dependency and gate changes

`data/packages.py` (rendered into `PLAN.md`, now 112 packages):

- **ER-02:** prerequisites at consumers, not blanket edges: R6-01 now depends on EX-1, P0-02, P0-03, P0-08, MOD-01 and
  X-01; F-05 on X-01; F-02a on MOD-01; EX-1 on P0-03. The builder still checks the graph is acyclic.
- **ER-03:** new change classes `migration` (F-02a, F-02b, F-16b) and `api` (MOD-02, F-08, F-13a) beside `move`,
  `behaviour`, `measure`, `infra`, each with its own gate.
- **ER-05:** new EX-1a embedded milestone (one synthetic end-to-end workload survives restart and erasure); Phase 2-5
  packages now start after EX-1a; the server milestone EX-1 still gates release.
- **New packages:** W2-15 coverage check and targeted re-extraction (ER-12, screened); R3-16 evidence obligations for
  joins, durations and lists (ER-19, screened).
- **Gates and titles:** F-16c viewer required and bound to the principal, stable memory scope (ER-13, ER-29); R3-09a
  exact reader-tokenizer accounting (ER-24); R3-09b adaptive complete bundles; R3-15 caches off until ER-31 tests
  pass; R3-01 generative calls counted apart from embedding and rerank (ER-39); W2-01 receipt never claims an
  unavailable capability (ER-06); F-16b replay of the retained log (ER-27); MOD-02 pinned capability manifest (ER-34).
- **Traceability:** a `REVIEW` map lists the ER ids and policies each package implements; the builder fails if any of
  ER-01..ER-40, POL-1 or POL-2 has no package. PLAN.md shows them in a new Review column.

## 3. Built, built-off, missing, proposed (at 924a3ed)

`data/inventory_2026-10-13.md`: built and on 0; built-off 5 (ER-19, 23, 25, 27, 28); partial 26; missing 3 (ER-07,
11, 12); plan-only 6 (ER-01..05, 40). Concrete defects confirmed in code: lift after rerank with unscored turns
(engine.py:5749, 6209-6212; jina_rerank.py:65-75); undated window facts silently dated to the window start
(windows.py:854); window jobs discarded on stop (windows.py:568-569).

## 4. ER-07..ER-15 and the two policies

Put to the owner as questions with options; the owner answered all eleven, choosing the proposed direction each time.
Recorded in `DECISION_AMENDMENTS_2026-10-13b.md` and `data/decisions_reconciled_2026-10-13.json` (0 unanswered).

## 5. Schema discussion agenda

`SCHEMA_AGENDA.md`: twelve sessions in order (versioning, trust boundary, identity, evidence and lineage, assessment,
time, receipt and readiness, jobs and progress, overload and accounting, read and cache, erasure and retention,
plug-in manifest), each listing the settled semantics it must encode and the questions to close before any field is
frozen.

## 6. Proposals vs measurements

Every new package that changes behaviour is a `behaviour` package with a screen as its gate; nothing here claims a
measured improvement. No dataset-specific prompts or copied benchmark answers were added.
