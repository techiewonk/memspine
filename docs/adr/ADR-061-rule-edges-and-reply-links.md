# ADR-061: Rule edges (causal, kinship) with a ranked causal walk, and reply-thread links

- **Status:** accepted (every new key off by default; off = byte-identical)
- **Date:** 2026-10-07
- **Decision id:** D-85. Plan v3.2 rows W12 (gap G08) and G27.

## Context

- **G08.** The graph is off in `base`, and its only writer of fact edges is `extract_graph`, which needs an
  LLM `extract_edges` role. Nothing links a turn to the turn that explains it, so a "why" question
  retrieves the turn naming the effect and misses the cause. Kinship and role statements ("my sister
  Ana") stay in raw text.
- **G27.** GMB-style threads answer earlier messages that are many turns away. A replay window of
  neighbouring turns does not reach them, and the engine had no way to record which message a turn
  answers.
- **Constraint.** No model calls, no new dependency.

## Decision

**1. Rule extractors** (`core/rule_edges.py`, pure functions).
- `causal_clauses(text)`: the first causal connective in each sentence splits it into a cause clause
  and an effect clause. "E because / 'cause / since / after / due to C" puts the cause after the
  connective. "C, so E", "as a result", "that's why" put the effect after it. A sentence that opens
  with the connective keeps the cause before its first comma. An intensifier "so" ("so happy") is
  not causal.
- `causal_links(turns)`: the edge always runs effect -> cause.
  - A clause naming an earlier turn links to it. The earlier turn must be one of the previous 40 and
    share the most content words with the clause, with at least 2 of three letters or more.
  - A turn that answers a turn asking for a cause ("What made you pick it?", "Why so shiny?") is
    that turn's cause.
  - A turn opening with "Because" is the previous turn's cause; one opening with "That's why" is its
    effect.
- `kinship_relations(text)`: "my sister Ana", "Ana, my boss", "Ana is my mentor" give
  `(person, relation, owner)`. The owner is the "Name:" speaker, else `user`.

**2. Sleep stage `rule_edges`** (`workers/pipelines.py`), opt-in through
`memories.associative.policies.rule_edges` (`true` or `{causal, kinship, lookback, min_overlap}`).
- It runs right after `extract_graph`, and only when on; the default cycle is unchanged
  (`schedule.RULE_EDGES_STAGE`). Taskiq priority 7.
- Each causal link becomes a `because` LINK event (system writer, reason `rule:<cue>`). Its weight
  is `RULE_EDGE_WEIGHT` (0.5), capped by both endpoints' trust (GP-10).
  - An existing live edge is not re-sent.
  - An endpoint at its link budget (M13.6) skips the edge.
- Each kinship triple becomes the semantic fact "Ana is Caroline's sister". It has entity `Ana`,
  tags `kind:event`, `rel:sister_of` and `dst:caroline`, and the turn as its parent.
  - It goes through the semantic door (`write_fact`; in bare contexts it is screened and appended),
    capped at the turn's trust.
  - It is keyed by `(person, relation, owner)`, so a re-run writes nothing new.
  - With `entity_nodes` on, both names become entity nodes.
- **Requirement.** Associative memory, since LINKs project only with the graph projector.

**3. Read: `read.causal_walk`** (`off` / `why` / `always`) and `read.causal_walk_hops` (1–3,
default 2).
- **Walk.** `AssociativeMemory.rel_walk` is a directed walk over chosen rels with GP-10 admission. It
  starts from the best `CAUSAL_WALK_SEEDS` (5) candidates, after `graph_rerank` and before the cut,
  and follows `because` and `reply_to` edges.
- **Scoring.** A record reached in `h` hops from a seed of relevance `r` scores `r x 0.9^h`. It
  joins the candidates, or lifts one already there that scored less.
- **Gates.** Every addition then passes `_gate_hits`, `hide` and the active date filter, as every
  leg does. The walk never admits a record on an edge alone.
- **Trigger.** `why` fires on questions asking for a cause (`is_why_question`: "Why", "How come",
  "What made / caused / led").

**4. Reply links (G27).**
- **Writing a link.** `write(..., reply_to=<record id>)` names the record a turn answers.
  - In `write_messages`, a message's `reply_to` is a record id or the index of an earlier message
    of the same call.
  - The target must be live in the same namespace; otherwise the write is refused like a missing
    record.
  - The link is stored as a `reply_to:<id>` tag in the WRITE payload, so it is event-sourced.
  - With associative memory on, a `reply_to` LINK (reply -> answered message) is written too. A
    held endpoint or a full budget skips the LINK; the tag still holds.
- **Replay.** `read.reply_links` makes a replayed hit also show the message it answers. That
  message is gated like a replayed neighbour, must be in the same namespace and must fit the budget.
  The causal walk follows `reply_to` LINKs as well.
- **Skipped (optional per the plan).** Alias cues from local-embedding noun-phrase clusters.

## Free measure (no model, gold evidence ids)

LoCoMo-10 (`evals/data/locomo10.json`, 5,882 turns) has 38 category-4 questions that start with
"Why". The rule pass gives 305 `because` edges (`causal_links`, defaults). As the retrieved-likely
turns we take the top-K turns by content-word overlap with the question.

| K | gold in top-K (lexical) | gold reached by a directed ≤2-hop `because` walk from top-K, not already in top-K | gold reachable (top-K ∪ walk) |
|---|---|---|---|
| 5 | 24 / 38 | **4** | 28 / 38 |
| 10 | 26 / 38 | **3** | 29 / 38 |

- **Edge coverage.** A gold turn sits on at least one `because` edge for 19 of the 38 questions.
- **Rule mix.** Most of the useful edges come from the "answer to a why-turn" rule. LoCoMo's cause
  turns usually answer "What made you …?". Connective-only rules alone reached 0 of 38.
- **Status of the result.** This is a reachability bound on gold evidence, not an answer-accuracy
  number.
- **Script.** In the session scratch directory (`measure_w12.py`); it imports the shipped
  `causal_links`.

## Consequences

- **Defaults.** `profile="simple"` and every template are unchanged. The golden gains three read
  keys at their off values.
- **Over-generation.** The rules over-generate: "after" and "since" are also temporal. The walk only
  ranks what it reaches, decayed below its seed, and the search gates still judge every record.
- **Link budget.** Rule `because` edges count against the per-node link budget (12). A turn with many
  causal mentions stops gaining edges rather than evicting caller links.
- **Forging.** A caller can forge a `reply_to:` tag. The expansion only pulls a same-namespace record
  through the replay gate, so a forged tag reaches nothing the caller could not already read.
