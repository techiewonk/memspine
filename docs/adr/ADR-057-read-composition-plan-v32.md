# ADR-057: Read-path composition fixes from plan v3.2 (raw-turn floor, evidence signal, gates by evidence)

- **Status:** accepted (all keys off by default; defaults change only by a later decision)
- **Date:** 2026-10-07
- **Decision id:** D-81. Plan v3.2 rows W19, W3, F4, F5, T10, F2, N01, N13, N18
  (`paper_spine/evaluation/PLAN_v3.2_ALL_IN_ONE_2026-10-07.md` in the research repo).

## Context

The LoCoMo round of 2026-10-06 closed with every added read block (cards, profile, mined
facts) losing on high-baseline conversations and on quote questions (92 → 68–74), and with
question-shape gates unable to separate the questions blocks help from those they hurt
(learnings L1–L4).

The run traces (2026-10-07, routed v3 vs combo-A on conv-26) show the mechanism:
- the context used about 1.7K of a 4K budget, so the **budget was not binding**;
- 2.5 derived records per question took **search slots**;
- each lost raw-turn hit cost its whole replay window: 39.2 → 32.9 raw turns and 6.5 → 5.6 sessions per question.

Separately:
- PrefEval's stated preferences are mostly first-person evaluative statements the H22 cues miss (11.3% recall);
- relative questions ("what did we discuss last week?") name no span for the temporal leg.

## Decision

Every rule below is read-path only and deterministic: regex or lexical, no model. Each sits behind its own key, off by default.

| Key | Plan | Behaviour |
|---|---|---|
| `read.raw_turn_floor` | W19 | The routed search widens by the derived (non-episodic) records it finds, up to `RAW_TURN_FLOOR_MAX_WIDEN` times, so raw turns keep every search slot they would have without them (all read modes and `assemble`). |
| `read.evidence_signal`, `read.evidence_weak_below` | W3 | `AssembledContext.evidence` (`core/evidence.py`):<br>• top score and spread over the next four;<br>• distinct days in the top five;<br>• the asked answer type (date / number / place / name) and whether a top-three candidate holds one;<br>• `weak`.<br>Reported only: the context is unchanged. Carried through replay, compose and the consent filter. |
| `read.cards_when_weak` | F4 | With `cards: header`, one extra raw-turns-only search. If its W3 signal is not weak, the question reads like a gated one: no cards header, no mined facts. Replaces question-shape gates. |
| `read.verbatim_raw_only` | F5 | `query_shape.is_verbatim` ("what did X say about …", "exact words") → no read header and, with cards, no mined fact. |
| `read.evidence_first` | T10 | In replay, the best hit's window comes first (in time order), then the rest in time order. |
| `read.temporal_relative` | F2 | With `temporal_leg`, a question without an absolute date resolves its first relative phrase against the engine clock (H1 rules, `relative_week`). |
| `read.present_order` | N01 | `relevance` (unchanged), `recorded`, or `recorded_if_shared_key` (time order only when two records share entity + attribute). |
| `read.focused_excerpt` | N13 | A record of six or more lines is shown as its two best-matching lines plus the next line of each (`core/excerpt.py`). Never for a verbatim question. Stored content is unchanged. |
| `read.standing_patterns` | N18 | `narrow` (unchanged) or `wide`: adds first-person evaluative cues to the H22 standing block. |

## Consequences

- Off: byte-identical reads (golden `simple_profile_defaults.json` gains the keys only).
- `cards_when_weak` costs one extra local search per read when on. Its threshold is calibrated on one dataset and confirmed on another (rule U5) before any default change.
- Free measurements so far (no model calls):
  - `is_verbatim` fires on 44 / 1,540 LoCoMo cat 1–4 questions (43 single-hop, 1 multi-hop, 0 open-domain);
  - `standing_patterns: wide` reaches 83.1% of PrefEval explicit preferences (narrow 11.3%), firing on 4.8% of LoCoMo turns (narrow 0.4%). Tuned on PrefEval, so it is confirmed on a second corpus before use.
- W19, F4 and F5 matter only when derived records exist (mining). Their LoCoMo effect needs mined stores, i.e. LLM mining, which this round does not run. Their tests show the mechanism on fixtures.
