# ADR-055: Question-shape gates on the replay read path

- **Status:** accepted (opt-in switches; no default changes)
- **Date:** 2026-10-06
- **Decision id:** improvement plan items A1, A2, B2, B3 (gate), D1 and H12
  (`paper_spine/evaluation/IMPROVEMENT_CODE_PLAN_2026-10-06.md`, sections 1 and 4;
  `IMPROVEMENT_MASTER_TABLE_2026-10-06.md`). Builds on H3 (`query_shape`), G11
  (`aggregate_top_k`), C3' (`temporal_leg`), #29 (happened dates), #30 (list cards), G1b/G3b
  (cards and profile headers) and E3 (count timeline).

## Context

The default LoCoMo configuration reads in `replay` mode (`base.yaml`). Several features only
run in `auto` mode or on the compose path, so they never affected a default run:

- The wider candidate pool for list and count questions (`read.aggregate_top_k`, G11) is
  applied only when `auto` routes a question to compose. In replay, list questions get the same
  top-k search as any lookup.
- The temporal leg matches only `valid_from`, the day a turn was said. A mined fact's event day
  sits in its `happened:` tag and was never matched.

The lead blocks also help one question type and cost others:

- List cards (`mine2` arm) helped multi-hop (+7) and cost single-hop (−5).
- The cards header on date questions showed miner dates, so `cards_skip_temporal` hides it.
  That also hides the event dates those questions need.
- The profile header helped open-domain (+4.2) and cost temporal (−2.5).
- Added blocks pushed raw turns out of a fixed budget. In the graph arm, raw turns reaching the
  reader halved (33.6 to 16.4).

## Decision

Add six opt-in `read.*` switches. Each gates one existing behaviour on the question's shape
(`query_shape.is_aggregation`, `is_count`, `is_temporal`: rules only, no model) or on the
budget. All default off. With every key at its default, `read()` and `assemble()` are
byte-identical: `tests/unit/golden/routed_read_off.json` was recorded before the change, on a
fixture with mined facts, a list card and profile insights, under four read configs.

| Key | Default | Effect when set |
|---|---|---|
| `read.aggregate_in_replay` | `false` | A replay read (or an `auto` read that reaches the replay path) of a list or count question retrieves `aggregate_top_k` candidates. Replay rendering stays; compose is not used. The budget caps the result. |
| `read.list_cards_only_aggregate` | `false` | The cards header drops `list_card` hits unless the question is a list or count question. The filter runs inside the header search, so other cards fill the slots. |
| `read.temporal_leg_event_dates` | `false` | `temporal_leg` also admits a record whose `happened:` label overlaps the query span. Its time is the start of the overlap. Order: distance to the span's middle, then `chrono_key`. |
| `read.cards_temporal` | `skip` | `skip` keeps today's `cards_skip_temporal` behaviour. `event_dates` shows date questions only the cards with a `happened:` tag, rendered `[happened d · said d']`. |
| `read.profile_skip_temporal` | `false` | Date questions get no profile header, plain or packed. |
| `read.lead_budget_share` | `null` | One cap on all lead blocks and the count reserve: `share × budget`. The count reserve is kept first. Whole blocks are then dropped in `constants.LEAD_BLOCK_DROP_ORDER`: profile, entity summaries, graph facts, cards. |

Design choices:

- **Replay, not compose.** `auto`'s compose rendering measured −2.1. A1 widens only the pool,
  and `_assemble_core` plus the replay window keep the budget.
- **Overlap, not containment, for B2.** A month or week label overlaps a day query. The leg is
  one RRF input among several, so recall matters more than precision here.
- **Whole blocks, not lines, for H12.** Each block was already fitted line by line to its own
  share. Dropping a whole block keeps the hide sets consistent: a dropped block's parents
  return to the routed read. It is also deterministic.
- **Count reserve first.** The occurrences block is built from the read's own raw turns, and
  only for count questions. The headers are question-independent extras.
- `cards_skip_temporal` keeps its meaning, so existing configs and published runs reproduce.

## Consequences

- Six new keys. They are in the defaults golden and `docs/USAGE.md`, and are tested in
  `tests/unit/test_routed_read_gates.py`.
- None has been measured on LoCoMo. The routed configuration in the improvement plan (section
  2) turns on A1, A2, B2 and D1. Paid screens decide any default change, which needs its own ADR.
- With `cards_temporal: event_dates` and the cards header shown, the routed read still hides
  every mined fact (the G1b rule). Undated facts therefore leave a date question's context,
  which is the intent: their miner dates were the problem.
- Not done here: making the completeness round reachable from replay (A4/H2), generic
  `read.replay_gates`, and line-level trimming of lead blocks.
