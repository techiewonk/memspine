# ADR-059: Plan v3.2 batch — rule mining, profile, read legs, governance verbs (all off)

- **Status:** accepted (every key off by default; defaults change only by a later decision)
- **Date:** 2026-10-07
- **Decision id:** D-83. Plan v3.2 rows:
  - W5, W5c, W5d, N07, N08: rule mining and the Λ-profile;
  - W6, W7, W8, W9, W10, W11, W13, N25, W14, G25, W16, W1, W3 step 2: read and governance;
  - N03, N04, N05, N06, N12, N21: read legs;
  - G12, G26, G29, G30, G31, G32, G33, G34, N19, E1: verbs and helpers.

  Siblings: ADR-057 (D-81), ADR-058 (D-82), ADR-060 (D-84, procedural), ADR-061 (D-85, rule edges).

## Context

The plan's generic-memory work packages, under the user's constraint of no LLM calls and no paid runs. Each mechanism is either:
- a deterministic rule;
- a lexical score;
- a local embedding/vector query;
- or an optional LLM step that stays off.

## Decision

| Area | Key / verb | Behaviour (all off by default) |
|---|---|---|
| W5 rule mining | `consolidation.miner: rules` (`core/rule_miner.py`) | First-person personal facts through the existing mining path; `state` slots supersede (home, origin, job, employer, relationship, age, education, diet, `favourite_<thing>`, partner / parents, `attitude:<obj>`), `event` slots coexist (likes, dislikes, pets, family, activities, plans). N08 canonical favourite keys; N07 attitude slots ("I no longer like X" supersedes a like). |
| W5 Λ-profile | `read.profile_slots_header`, `read.profile_sensitive` | Header of the current state facts of the named persons (else `user`); `sensitive:*` slots excluded unless allowed. |
| W6 | `conflict.merge_containment` | A restatement contained in the current fact is NOOP, not a supersession. Cardinality many → set slots (W5); the CONFLICT line already exists (`current_state_view` DISPUTED). |
| W7 | `read(as_of=)`, `assemble(as_of=)`, REST `/assemble` | Valid-time view: records begun by then; a fact superseded after `as_of` counts as current; relative phrases resolve against it. |
| W8 | `episodic.policies.subject_tagging`, `read.subject_leg` | `speaker:<name>` tags at write; RRF leg of the named speaker's turns by word overlap. |
| W9 | `read.profile_scope_gate` | Profile header only for personal questions or persons memory knows. |
| W10 | `read.session_cap` | At most N raw-turn hits per session from a 4× wider search. |
| W11 | `read.role_aware` | Recommendation-ledger tag on assistant turns; an assistant leg for "what did you recommend". |
| W13 + N25 | `integrity.corroboration_roots` | Corroboration counts independent lineage roots, and a near-copy of an earlier corroborator's wording does not vouch again. |
| W14 | `verify_forget(probe=)` | Erasure proven on recall (residual near-duplicates block `clean`). |
| G25 | `episodic.policies.forget_detector`, `Engine.forget_requests` | Imperative "forget that" requests tagged; candidates listed; never auto-deleted. |
| W16 | `firewall.sensitive_topics` | Art. 9-style topic tags plus a pii-tier raise; the text is kept. |
| W1 | template `protected` | `base` + implicit parents, skip injected recall, assistant-claim tags. |
| W3 step 2 | `read.evidence_line` | A weak read carries a one-line note. |
| N03 / N04 / N05 / N06 | `read.prf_expansion`, `read.second_round`, `read.sentence_leg`, `read.cluster_expand` | Feedback probe; weak-evidence second search; lexical best-sentence leg; embedding neighbourhoods of the top hits. |
| N12 | `read.rerank_instruction` | Configurable qwen3 reranker instruction. |
| N21 | `read.concentration_filter` | Near-duplicate clusters collapse to one member tagged `concentrated:<n>`. |
| G34 | `read.multi_intent_split` | Multi-part questions become one probe per part. |
| G32 | `read.novelty_exclusions` | "Something new" requests get a header of known items. |
| G33 | `consolidation.auto_watch` | A mined plan with a future date becomes a watch. |
| E1 | `consolidation.mining_cache` | LLM mining at temperature 0, one cached reply per (model, prompt, variant, transcript). |
| G26 / G29 / G30 / G31 / G12 / N19 | `Engine.vet`, `Engine.approve` + `authorize(approval=)`, turn `caption`, `Engine.bulk_read_alerts`, `Engine.rating_profile`, `Engine.session_memories` | Vet a draft against current facts; deny-by-default approvals; captions stored with turns; bulk-read report; behaviour profile; derived records of a session. |

## Consequences

**Golden.** `simple_profile_defaults.json` gains the keys at their off values only.

**Free measurements (local embedder, retrieval-only, no model calls; LoCoMo).** The coverage-as-QA proxy was rejected (M2: ρ = 0.36), so these are retrieval results only.

| Measure | Result |
|---|---|
| Temporal leg | coverage +1.7 (28 won / 2 lost) |
| `aggregate_in_replay` (N02) | multi-hop +4.3 (12 / 0) |
| 8K budget | −1.8 with unchanged context (anomaly, to investigate) |
| Rule miner | 175 facts / 5,882 turns, spot precision ≈ 21 / 25 |
| PrefEval wide standing cues | 83.1% (ADR-057) |
| PersonaMem-v2 masking | 468 / 511 (ADR-058 + W16) |
| PoisonedRAG clusters | 257 / 300 |

Each key needs the U5 check (two independent source corpora) before any default change.
