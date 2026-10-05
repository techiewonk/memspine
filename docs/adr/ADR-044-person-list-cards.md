# ADR-044: Person-level list cards over mined event facts

- **Status:** accepted
- **Date:** 2026-10-06
- **Decision id:** master absorb list #30 (SimpleMem synthesis), with #28 (multi-view fact
  fields, SM-3) as its input and #32 (reflexion ablation, GR-16) in the same change. Builds on
  C6' mining, G1a event facts, G1b cards header and M7 erasure (ADR-014, #43).

## Context

LoCoMo multi-hop (cat 1) fails mostly on list and count questions ("What activities does Melanie
partake in?"): 23% of the wrong answers had only part of the gold evidence in context. The items
sit in event facts mined from different sessions, and K=10 retrieval cannot cover all of them.
Nothing aggregated them.

## Decision

1. **Multi-view fields (#28).** `ExtractedFact` gains optional `persons`, `location` and `topic`,
   validated by the #31 guards (placeholders and reasoning dropped, at most 8 persons, 80 chars).
   With `consolidation.mine_multiview` the deposit stores them as tags `person:<v>`, `loc:<v>`,
   `topic:<v>` (NFKC, case-folded, whitespace collapsed; `core/fact_views.py`). The statement is
   stored as mined. The prompt is a new variant, `extract@session4`; earlier variants are unchanged.
2. **List cards (#30).** With `consolidation.list_cards`, a step at the end of `mine_facts` groups
   each namespace's live, unflagged event facts by (person, class) and keeps one derived semantic
   record per group of at least 2: `"<Person> — <class>: item (YYYY-MM), ..."`.
   - person = `person:` tags, else the entity; class = `topic:` tag, else a non-generic miner
     attribute, else one `extract@classes` call per person batch. Labels are cached in the log as
     `list_classes` MARKER events, so a fact is classed once.
   - parents = the facts, so a hard forget cascades; trust is capped at the least trusted fact
     (E1) and, under integrity, at their view trust. Role `assistant`, channel `list_card`, tags
     `list_card` (reserved: callers cannot set it), `atomic_fact`, `listkey:<group>`,
     `listfp:<members+text>`.
   - An unchanged fingerprint writes nothing. A changed one archives the old card
     (DECAY_TRANSITION) and writes a new one, under the namespace lock, only if all its facts are
     still live. A group that falls below the floor has its card archived.
   - The cards header (`read.cards: header`) shows a list card whole, with no said date, within
     `cards_budget_share`.
3. **MINJA exemption, narrow.** A re-derived card repeats its predecessors' prefix by construction,
   and the MINJA bridge-prefix check quarantined it. For that one write, contents of earlier cards
   of the same group (same `listkey:`, engine-only tag) are left out of the recent-contents
   comparison. All other firewall checks apply unchanged.
4. **Reflexion ablation (#32).** `memories.semantic.policies.write.reflexion` (default `true`). When
   `false`, the C2/C3 edge extractor runs one round whatever `extract_graph.max_rounds` says.

## Consequences

- Defaults are unchanged: all three switches are off (reflexion on), golden updated at those values.
- A list card costs one write per changed group per sleep and no model call unless a class is
  missing. A soft-forgotten fact drops out of its card at the next sleep.
- Read legs can prefilter on the view tags; that is wave3/read's work, not this change.
- With `max_rounds: 1` (the default) reflexion has nothing to skip. The ablation matters only for
  configurations that set more rounds.
