# Perspective / attribution layer (I39-I55)

Date 2026-10-10. Engine branch `feat/local-qwen-stack`. Code: `src/memspine/core/perspective.py`
(pure rules and the read-side maths), wired in `src/memspine/engine.py`. Comparison with 13 other
engines: `docs/PERSPECTIVE_COMPARISON.md`. Tests: `tests/unit/test_perspective.py`,
`tests/unit/test_perspective_axes.py`, `evals/tests/test_perspective_metadata.py`.

## 1. Audit: what the engine had before this layer

| Concern | What exists | Where | Gap |
|---|---|---|---|
| Owner / namespace | hierarchical namespace, the hard isolation boundary; grants for cross-namespace reads | `core/namespace.py:56` (`grant_allows`), every read filters `record.namespace == ns` (`engine.py` `_gate_hits`) | none; stays the only hard boundary |
| Role / provenance | `SourceInfo.role/channel/principal/parents`; role-based trust matrix | `core/records.py:111-128`, `core/policies/trust.py:28` | role is the chat role only; a named participant is not a role |
| Speaker | `Name:` text prefix parsed with a capitalised regex; opt-in `speaker:<name>` tag | `core/temporal_query.py:321` (`speaker_of`), `engine.py:1450` (`subject_tagging`) | only prefix-shaped text; no addressee, no N-party notion |
| Speaker legs | `speaker_leg`, `speaker_vector_leg`, `comparison_speaker_legs`, I5 `subject_vector_leg` (I / my to user turns, you / your to assistant turns) | `temporal_query.py:327,369,472,426`; `engine.py` `_speaker_vote_leg` | vote only; first person bound to roles only, never to a named asker; no third party |
| Name stripping | `lexical_strip_names` drops speaker names from the BM25 query | `engine.py` `_strip_speaker_names`, `config/schema.py` | derives names by parsing `Name:` from every record; shows speaker is not first-class |
| Entities / graph | `entity`/`attribute` fact key, `person:` view tags, `extract_graph` entities and relations, rule miner owner = speaker prefix else `user` | `core/records.py:188`, `core/fact_views.py:36`, `engine.py` `extract_graph` paths, `core/rule_miner.py:240-266`, `core/rule_edges.py:256` | persons are extracted facts, not a property of every record; relation to the speaker is lost |
| Profile per person | Lambda profile header: slots of each person the question names, else `user` | `engine.py` `_slots_section`; `profile_scope_gate` -> `_applies_to_person` (names / `is_personal`) | "my cousin" counts as personal, so the asker's profile is injected on a question about someone else (OP-Bench subject_confusion) |
| Write path of the harness | `write_messages([{"role": "user", "content": "Name: text"}])` for every turn; no speaker/role metadata | `evals/.../systems/memspine_system.py` `_deposit`; `datasets/locomo.py`, `op_bench.py` (`persona`) | speaker identity and dataset roles never reach the engine; `user`-only role also hides assistant turns from trust / role-aware features |
| Knowledge updates | I17 latest-wins keyed by (role, "Name:" prefix) + word overlap; keyed facts by (entity, attribute) | `core/latest_wins.py:80,118`, `memories/semantic/store.py` conflict ladder | no subject, scope or polarity in the key |
| Stance axes | sensitive lexicon tags (W16); negation only in vetting; temporal tags `happened:` / `said:` | `core/sensitive.py`, `core/vetting.py:21`, `core/event_date.py` | no modality, polarity, scope, certainty or reported-speech axis |
| Decider port | `Decider` (heuristic default, OpenDecider-nano), yes/no tasks | `services/decision/decider.py` (merged at 42f91e0) | no perspective tasks |

Conclusion: identity is parsed ad hoc from text, never stored; there is no addressee, no subject
beyond the speaker, no asker on the read side, and no stance. Everything below is one layer that
fills those in.

## 2. Design: one record-level model, one question-level model

A record belongs to ONE owner (its namespace). Its text has a speaker, an addressee, subjects and a stance.
The layer stores them as tags, never in the content (constants `TAG_*` in `core/perspective.py`).

| Axis | Tag | Set at | Source of truth (in order) |
|---|---|---|---|
| owner | namespace (not a tag) | write door | hard boundary (I19); subject is soft |
| `spk(m)` speaker | `spk:<id>` | write | message `speaker` / `name` > `Name:` text prefix > chat role (`user`/`assistant`/`tool`); unknown = none, treated as the owner `user` |
| addressee | `addr:<id>` | write | message `addressee` > chat counterpart > previous speaker > the one other known human |
| `sub(m)` subject(s) | `sub:<id>`, `sub:<relation>@<anchor>`, `sub:3p` | write | first person = speaker; second person = addressee; `my/your/X's <relation>` = `<relation>@<possessor>` (+ the appositive name); known participant names; unresolved he/she/they = `3p`; elliptical statements by a human speaker are about themselves (I54: kin binds to the SPEAKER) |
| `ask(m)` | `ask:<id>` | write | question / request turns |
| modality (I40) | `mod:fact|plan|wish|hypo|opinion|question|request` | write, per sentence | rules; decider task `is_fact` |
| polarity (I41) | `pol:neg|pos` | write | negation cue, idioms excluded; decider `is_negated` |
| time (I42) | existing `valid_from`, `happened:`, `said:`; latest-wins skips non-facts | exists | linked, not duplicated |
| reported speech (I43) | `rep:<source id>` | write | "my mom said ..." gives `rep:mother@<speaker>`; trust cap = planned (I43 row) |
| certainty (I44) | `cert:hedged` | write | hedge cues; decider `is_hedged` |
| sensitivity (I45) | `sensitive:<category>` (W16 lexicon) | write | grading and the bar belong to I52 |
| scope (I46) | `scope:event|habit|standing` | write | rules; decider `is_standing` |
| plan window (I49) | `due_to:<date>` | write | relative horizon from the mention time; none = soft deadline |
| asker (question) | not stored | read | `read.perspective_asker` > `asker_scope()` context > namespace `context.owner` > `user` (no named participants) > a lone participant > unresolved |

Resolution is deterministic and language-light (English regexes, covered by the I23 language guard
convention). The pluggable port is `refine_with_decider`: the decider answers typed yes/no tasks
(`is_fact`, `is_negated`, `is_standing`, `is_hedged`) and only a confident answer overrides a rule.
Nothing is written to content or logs by the decider path.

### Read time

`resolve_question` gives `asker`, `targets` (subjects with ids / `relation@anchor` keys), `about`
(`self|assistant|participant|third|mixed|none`), question polarity, scope (`trait|event`) and whether
non-facts are asked for. A record's match against the targets is:

- 1.0 the record is about a target (`sub` contains a target key; a target speaking with no other subject);
- 0.6 the target speaks of someone else; 0.5 an unresolved third party; 0.0 about someone / something else;
- neutral when the question is unresolved or the record has no tags.

`read.perspective_mode`: `subject_weight` adds an RRF leg of match-1.0 records (vector order) and
multiplies each candidate's relevance by `1 - w * (1 - match)`; `subject_filter` also drops match-0.0
candidates down to a floor. The other axes (`read.perspective_axes`) multiply by `1 - w`: a record
that is only a plan / wish / hypothetical / question for a fact question; only negated for a positive
question; only a one-off event for a trait question; a sensitive record unless the question touches
the same category. The question asks for the weaker form ("planning", "never", "ever", "asked") and
the penalty is skipped. `read.speaker_vote_mode=perspective` makes the I5 vote this same match
(I5 `subject` mode is its special case). Forensics: `search_forensics()["perspective"]` and the
`perspective` leg in `extra_legs`.

Render: `read.perspective_marker` prefixes `[about: Caroline's cousin]` only where the subject differs
from the speaker, and `[past plan]` where `due_to` is before the as-of / clock date. Content is stored
unchanged.

### The four conversation shapes

| Shape | Speaker | Addressee | "I" | "you" | Third party |
|---|---|---|---|---|---|
| two named speakers (LoCoMo) | `Name:` prefix or message `speaker` | the other speaker | the speaker | the other speaker | `my cousin` = `cousin@<speaker>` |
| user / assistant chat | role | the counterpart | user | assistant (or user in assistant turns) | as above; assistant facts are `sub:assistant` (agent), kept out of user-profile questions |
| N speakers | prefix / `speaker` | previous speaker, else vocative-free unknown | speaker | addressee if known | named participants become subjects |
| single-user notes | none (owner) | none | owner | none | relation phrases; no implicit subject without a person cue |

### Interaction with the rest

- Relevance gating (I29 / I33): an unresolved or third-party question suppresses the asker's profile slots
  (`_slots_section`); the gate decisions are unchanged otherwise.
- Profiles (I50, partial): `_slots_section` uses the question's persons else the resolved asker; a card per
  relation-bound subject is planned.
- Knowledge updates (I17): latest-wins is per subject and ignores question / request / hypothetical turns
  (`core/latest_wins.py`). For keyed facts, `conflict.perspective_key` (I55) makes the key
  (owner, entity, attribute, subject, scope): another subject or scope coexists, a polarity flip contests.
- Isolation (I19): namespace is the only hard boundary; subject and speaker are soft retrieval signals.
  A visibility predicate is I53.

## 3. Implemented (opt-in, defaults byte-identical)

| Key | Where |
|---|---|
| `memories.episodic.policies.perspective` (`heuristic`/`decider`, `axes`, `context`) | `engine.py` `_perspective_options` / `_annotate_perspective`; `write()` and `write_messages()` (`speaker`, `name`, `addressee` message keys) |
| `read.perspective_mode/_weight/_min_keep/_asker/_axes/_marker`, `read.speaker_vote_mode=perspective` | `config/schema.py`; `engine.py` `_question_perspective`, `_perspective_leg`, `_apply_perspective`, `_attributed` |
| `memories.semantic.policies.conflict.perspective_key` | `core/policies/conflict.py`, `memories/semantic/store.py` `_incumbent_for_subject` |
| adapter `perspective_metadata` (on when the config enables the policy) | `evals/memspine_evals/systems/memspine_system.py` `_message`, `set_query_meta` |
| decider tasks `is_fact`, `is_negated`, `is_standing`, `is_hedged` | `services/decision/decider.py` |

## 4. Not yet implemented (exact plans in `evals/analysis/GAP_REGISTER.md`)

I43 hearsay trust cap, I48 inferred provenance, I50 per-subject card, I52 graded sensitivity, I53
participants / viewer / visibility, the user-acknowledgement promotion of assistant claims (I47), mined
facts inheriting `sub:` / `scope:` / `pol:` from their source turn (I55), write-side `person:` graph
integration, optional use of `lexical_strip_names` over the participant set instead of text parsing.

## 5. Screen plan (retrieval-only first, free)

Slices: LoCoMo cat 1-4, cat 5, OP-Bench (especially `irrelevance_hard/subject_confusion`).
Arms (same engine, forensics on): R0 current best; P1 `perspective: heuristic` + `perspective_mode:
subject_weight`, w 0.4; P2 `subject_filter`, min_keep 5; P3 P1 + axes `[subject, polarity, modality]`;
P4 `speaker_vote_mode: perspective` with `list_mode`. Adapter: metadata on (automatic with the policy),
OP-Bench asker = `persona` (already in query meta). Metrics: R@k and evidence hit rate per slice
(LoCoMo), `persona_share` / injection rate on subject_confusion (lower is better), profile-injection
rate, and the paired net-correct count on the 2 development conversations (233 q) before any held-out run.
Adoption rule: macro mean over slices improves beyond noise and no slice loses more than its noise band.
Risk to watch: in LoCoMo a question about one speaker is often answered by the other speaker's turn ("You
researched adoption agencies, right?" is `sub:` the addressee, so it matches 1.0), but the other speaker's
self-statements and reactions get 0.0 or stay neutral; `subject_filter` can therefore lose context turns the
reader needs, which is why `subject_weight` is the first arm and `subject_filter` keeps a floor.
