# ADR-063: Gaps plan 2026-10-08 (N30–N65): retrieval, ranking, context and answer-prompt arms

- **Status:** accepted (every key off by default; defaults change only by a later decision)
- **Date:** 2026-10-08
- **Decision id:** D-87. Research repo:
  - `paper_spine/evaluation/GAPS_PLAN_N30_N57_2026-10-08.md` (waves A–D);
  - `sota/S6_…`, `sota/S7_…` (sources).

## Context

The code-level studies of the leading memory systems found mechanisms memspine lacked:
- S6: Hindsight, MemMachine, Dakera, Backboard, EverMemOS, Mem0, Zep, ByteRover;
- S7: EverMemOS, Dakera, Hindsight re-scan.

The free LoCoMo screens showed two things:
- **Time and ordering help.** The temporal leg gained +1.7 and is now on in `base` (ADR-062).
- **Arms that add candidates displace raw turns and lose.** N03 lost 9.8, W10 6.1, N06 3.0.

The plan builds every gap without a model on the read path, behind its own key, to be screened before any default changes (rule U5).

## Decision

### Wave A: time and text matching

| Key | Item | Behaviour |
|---|---|---|
| `embedding.query_instruction` | N64 | fastembed only. A text prepended to queries, never documents. The BGE instruction is `constants.BGE_QUERY_INSTRUCTION`. fastembed's `query_embed` does not add it. Query vectors are cached and erased under an instruction-keyed key. |
| `read.lexical_analyzer: english` | N58 | Tantivy stop words + Snowball stemmer, applied to both the index and the query. |
| `read.lexical_dates` | N30 (write side) | Date words of the said day and the days a record names, in the BM25 index. |
| `read.temporal_infer_year` | N45 | A date with no year takes the latest year on or before the newest record or the as-of time. |
| `read.temporal_soft` | N44 | Empty leg slots are filled with the nearest records outside the span. It never displaces an in-span record. |
| `read.temporal_rank: overlap` | N61 | In-span records ranked by content words shared with the question. |
| `read.temporal_leg_mentions` | N30 (read side) | A turn enters the leg by the dates its text names, resolved against its own time. |

The two lexical variants (`english`, `dates`) build their own index directory under their own projector name (`lexical:<variant>`). Turning one on therefore rebuilds that index from the event log and never touches the default index.

### Wave B: ranking and probes

| Key | Item | Behaviour |
|---|---|---|
| `read.rerank_blend`, `read.rerank_gate` | N41 | Blend the reranker score with the retrieval score; keep the retrieval order when the reranker is not confident. |
| `read.rerank_context` | N42 | The reranker sees episodic-session neighbours. |
| `read.entity_leg` | N59 | Proper nouns and years beyond the speakers, matched in raw turns. |
| `read.statement_probe` | N63 | The question rewritten by rules into a statement. |
| `read.leg_weights_by_shape` | N62 | Leg weights per question shape, over named legs. |
| `read.short_query_lexical_weight` | N33 | BM25 weight for questions of at most 5 content words. |
| `read.speaker_probe` | N40 | "Name: core terms" as a vector probe. |

### Wave C/D: fusion, expansion, context

| Key | Item | Behaviour |
|---|---|---|
| `read.fusion: minmax` | N52 | Min-max score fusion instead of RRF. |
| `read.cohesion_leg` | N43 | Records said within 5 minutes of the first-pass top hits. |
| `read.entity_expand_leg` | N32, N53 | Names of the top hits, followed; names in more than 5% of records are damped. |
| `read.maxsim_leg` | N60 | Best-sentence cosine over first-pass candidates. Candidate sentences are embedded and cached at read. |
| `read.session_digest` | N31 | Extractive per-session header. It does not hide its turns from the read. |
| `read.facts_to_sources` | N54 | A mined fact yields its slot to its source turns. |
| `read.type_quotas` | N55 | A maximum number of records per memory type. |

### Harness (`evals/`, not shipped)

| Item | Change |
|---|---|
| N34 | `--memspine-as-of-question-date` |
| N51 | Order-invariance test |
| N57 | Vendor judge suites and `memspine_evals.rejudge` |
| N46, N56, N65 | QA prompts `dated_planned`, `dated_noabstain`, `evermemos_cot`. EverMemOS's 7-step prompt is verbatim; `final_answer` v3 reads its "STEP 7: FINAL ANSWER" heading. |

### Addendum (2026-10-08): context practices from a production memory service

| Key | Item | Behaviour |
|---|---|---|
| `read.recent_exchanges` | C2 | A recent-conversation header, without the in-flight question. |
| `read.leg_min_scores` | C6 | Per-leg score floors before fusion. |
| `read.section_captions` | C7 | A caption on the retrieved part. |
| `read.followup_probe` | C1 | Rules: follow-up questions also search with the previous turn. |

All four are off.

## Consequences

- **Defaults:** with every key off, reads are unchanged. The simple-profile golden only gained the new keys at their off values.
- **Erasure:** two read-side caches are keyed by text: N60 sentence vectors and N30 date spans. `_purge_caches` drops both, so an erased text leaves no derived trace in process memory.
- **Measurement:** each key is screened retrieval-only on LoCoMo against its batch's own baseline. A key becomes a default only after a second corpus agrees (U5), and any QA claim needs a paid run.
- **Answer-extractor bump:** the extractor moved to v3, so the `describe()` of the dated3 reader changed. This is recorded; there is no silent comparability break.
