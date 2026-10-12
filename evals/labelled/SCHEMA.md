# Labelled sets (X-05)

Hand-written synthetic sets used to measure derivation quality for W-17 (grounding), W-19 (entity
resolution), O-12 and O-13 (contradiction and near-duplicate handling) and O-17 (global questions).
Every name is invented. The text is original and contains nothing from LoCoMo, LongMemEval, MAB,
ConvoMem, PrefEval or OP-Bench. No model, network or credential is needed to read the files.

Each file is `<set>.v1.jsonl`. Line 1 is a header, every further line is one record (UTF-8, `\n`).

```json
{"set": "grounding", "version": 1, "schema": "grounding/1", "description": "..."}
```

`version` is an integer. A change to any record or label means a new file (`.v2.jsonl`), never an
edit in place. `loader.content_hash(set)` returns a SHA-256 over the canonical JSON of the header
and records, for manifests. `loader.py` holds typed dataclass loaders (`load_grounding`,
`load_entity_pairs`, `load_contradictions`, `load_global_questions`).

Shared vocabulary follows the schema sessions of 2026-10-13 (S4-1 evidence roles, S5-1 assessment
fields, S5-2 supersede rule, S6-2 weak dates).

* `modality`: asserted, negated, hypothetical, planned, reported, question.
* `binding`: bound, partly, unbound.
* `source_trust`: a coarse two-tier stand-in for the system-set trust. `default` = user, operator or
  system families; `low` = tool, web or assistant-proposed families. `default` ranks above `low`;
  equal tiers count as "at least as trusted".

## grounding.v1.jsonl (W-17)

One candidate extracted assertion against a short snippet. 62 items.

| Field | Meaning |
|---|---|
| `id` | `gr-NNN`, unique. |
| `lang` | Snippet language: en, de, nl, es, ja. |
| `category` | What the item mainly probes: plain, number, modality, binding, polarity, name, qualifier. |
| `source_time` | ISO date the snippet was said, or null. Present when a relative date needs it (S6-2). |
| `turns` | List of `{turn_id, speaker, text}`; ids are `t1`, `t2`, ... within the item. |
| `assertion` | The candidate assertion (English). |
| `claimed_modality` | Modality the candidate asserts for its content. Supported only if the source shows the same modality. |
| `label` | `supported`: all of the assertion is in the evidence. `unsupported`: a number, unit, qualifier, name, polarity, modality or binding is wrong or absent. `partly`: one element is supported and another is not. |
| `failure` | null for supported; otherwise the element that fails: number, unit, polarity, name, qualifier, modality, binding. |
| `support_turns` | Turn ids with role `support`; empty for unsupported. |
| `binding_turns` | Turn ids with role `binding` (the context line that resolves a pronoun or reference). Must also be cited. |
| `binding_basis` | `none`; `turn` (a context line in `binding_turns`); `source_time` (relative date resolved against `source_time`, no turn needed). |
| `span` | `{turn_id, start, end, text}`: character offsets of the supporting span (`text == turn.text[start:end]`); for `partly` it covers the supported part only. Null for unsupported. |
| `unsupported_part` | For `partly`, the element of the assertion with no support; else null. |

Distribution: label 21 supported, 21 unsupported, 20 partly (bounds: 25% to 42% per label).
Claimed modality: asserted 45, planned 8, negated 3, hypothetical 3, reported 2, question 1.
Binding: 5 context-line items, 3 relative-date items, plus 6 unsupported items where the binding
cannot be resolved. Languages: en 30, de 12, nl 9, es 8, ja 3.

## entity_pairs.v1.jsonl (W-19, O-11)

A pair of mentions, each with a type and one evidence line (O-11 shows one line per side). 80 pairs.

| Field | Meaning |
|---|---|
| `id` | `ep-NNN`. |
| `category` | `alias` (nickname, initials, transliteration), `homonym` (same string, different or same referent decided by context and type), `numeric` (units, versions, house numbers), `cross_language` (exonyms). |
| `subcategory` | alias: nickname, initials, transliteration. homonym: person, place, type (different entity types). numeric: unit_number, version, line, house_number, model. cross_language: exonym. |
| `label` | `same` or `different`. A `different` homonym pair must stay unmerged. |
| `a`, `b` | `{mention, type, context}`. |
| `languages` | Language codes of mention `a` and `b`. |
| `note` | Optional reason. |

Distribution: same 41, different 39. By category (same/different): alias 17/11, homonym 4/12,
numeric 8/8, cross_language 12/8. Languages seen in mentions: en, de, nl, es, pl, da, pt, fr, ja,
cs, tr, it.

## contradictions.v1.jsonl (O-12, O-13, W-21)

An existing assertion list for one subject and attribute, one incoming assertion, and the expected
integration outcome under S5-2 plus the negation rule decided on 2026-10-13. 44 items.

| Field | Meaning |
|---|---|
| `id` | `ct-NNN`. |
| `lang` | Language of the assertion texts. |
| `attribute` | Attribute name, e.g. residence, hobby. |
| `cardinality` | `one` or `many` (a schema property of the attribute). |
| `stage` | `same_key` (same predicate) or `cross_key` (different predicate, same subject; the O-12 stage 2 case). |
| `existing` | Assertions already stored, oldest first (`e1`, ...). |
| `incoming` | The new assertion (`new`). |
| `expected` | `supersede`: asserted, bound, trust at least the current value's, cardinality one. `coexist`: cardinality many, or a non-overlapping past interval. `retire`: a bound, negated incoming assertion ("no longer ...", `value` = `none`) at cardinality one with trust at least the current value's; the current value's interval is closed and no new value is created. `contest`: a cardinality-one conflict (asserted or negated, bound) from a lower-trust newer assertion; both kept. `tentative`: hypothetical (including hypothetical negation), planned, reported or not-fully-bound incoming; kept as tentative next to the current value. |
| `reason` | `cardinality`, `trust`, `modality`, `binding`, `time`. |
| `note` | Optional. |

Each assertion carries `{id, text, subject, predicate, value, modality, binding, source_trust,
source_family, source_time}`.

Distribution: supersede 12, coexist 10, contest 10, tentative 10, retire 2 (15% to 35% bound per
outcome, retire at least 2). Reasons: cardinality 20, trust 11, modality 10, binding 2, time 1.
Stage: same_key 33, cross_key 11. Languages: en 20, de 8, nl 7, es 5, ja 4. Four items carry a
negated or hypothetical-negated incoming assertion (2 retire, 1 contest, 1 tentative).

## global_questions.v1.jsonl (O-12, O-17)

A corpus and its questions in one file; `kind` separates them. 10 conversations, 20 questions.

Conversation record: `{id (c01..c10), kind: "conversation", lang, speaker, date, turns}`; turn ids
are globally unique (`c01-t1`). Languages: en 3, de 2, nl 2, es, ja and pt 1 each.

Question record:

| Field | Meaning |
|---|---|
| `id` | `gq-NNN`. |
| `kind` | `question`. |
| `question` | The global or aggregate question. |
| `question_type` | `set` 11, `aggregate` 5, `list` 3, `count` 1. |
| `expected_conversations` | Conversation ids that must be reached to answer completely. |
| `answer_elements` | The expected answer items; every element should appear in a correct answer. |
| `expected_count` | The expected number (of items or people) when the question asks for one, else null. |
| `note` | Traps, e.g. a planned trip is not a hobby; a wished-for pet is not a pet. |

At least four questions need five or more conversations. Several contain deliberate near-misses
(c01 plans a bike trip, c10 only wishes for a cat) to measure precision as well as recall.

## Balance and checks

`evals/tests/test_labelled_sets.py` enforces: header and integer version, unique ids, closed label
vocabularies, referenced turn and conversation ids exist, span offsets match, outcome rules hold
(for example every `supersede` item has asserted + bound + trust at least current), label balance
bounds (grounding 25% to 42% per label; pairs same share 45% to 55%; contradictions 15% to 35% per
outcome), at least four languages per set, no line containing a token of the gold deny-list from
`tests/unit/test_prompt_gold_lint.py`, and no URL, address or key-like string.
