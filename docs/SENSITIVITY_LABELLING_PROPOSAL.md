# Sensitivity labelling proposal (RM-98) and the layer it specifies (RM-99)

Status: accepted by the owner decisions of 2026-10-22 (RM-52, RM-86, RM-87, RM-98, RM-99).
Implemented in `src/memspine/derive/sensitivity.py` and wired into `compile_edges`
(`derive/pipeline/stages/edge_compiler_v2.py`). Tests: `tests/unit/derive/test_sensitivity.py`,
`tests/unit/services/graph/test_qualifier_exclusions.py`,
`tests/unit/wiring/test_rm99_sensitivity_edges.py`.

## 1. Why

RM-52 added five optional sensitivity labels that can only tighten handling policy. RM-86
and RM-87 need edges to carry *sets* of these labels and a record of whether anyone looked,
so that a filter can exclude sensitive edges without treating "unknown" as "safe". This
document reviews how four established classification tools label sensitive data, then
fixes the label set, detectors, confidence model, states, versioning, offline constraints
and evaluation fixtures for memspine.

## 2. What the established tools do

### Microsoft Purview

- **Labels form a two-level hierarchy with an order.** A parent label (now a label group)
  holds sublabels. Order is priority: least restrictive first, most restrictive last. An
  item carries exactly one sensitivity label. When several auto-labelling rules match, the
  last (most restrictive) label wins. Microsoft advises at most five top-level labels with
  at most five sublabels each.
  <https://learn.microsoft.com/en-us/purview/sensitivity-labels>
- **Sensitive information types (SITs).** A pattern has a primary element (a regex with
  or without checksum validation, a keyword list, a dictionary or a function), supporting
  elements, a confidence level and a proximity window in characters (250 in examples).
  Confidence is low 65, medium 75 and high 85. A supporting element counts only when it
  falls entirely inside the window.
  <https://learn.microsoft.com/en-us/purview/sit-sensitive-information-type-learn-about>
- **Exact data match** checks hashed lists of real values to cut false positives, and
  **trainable classifiers** are whole-document ML classifiers for content that patterns
  cannot describe.
  <https://learn.microsoft.com/en-us/purview/sit-learn-about-exact-data-match-based-sits>,
  <https://learn.microsoft.com/en-us/purview/trainable-classifiers-learn-about>

### Google Cloud Sensitive Data Protection (DLP)

- **Likelihood is an ordinal scale:** VERY_UNLIKELY < UNLIKELY < POSSIBLE < LIKELY <
  VERY_LIKELY. A passing checksum without supporting context is only POSSIBLE.
  `min_likelihood` (default POSSIBLE, settable per infoType) drops weaker findings.
  <https://docs.cloud.google.com/sensitive-data-protection/docs/likelihood>
- **Detectors.** Built-in infoTypes plus custom ones: dictionaries, large stored
  dictionaries, regexes and others.
  <https://docs.cloud.google.com/sensitive-data-protection/docs/creating-custom-infotypes>
- **Hotword rules** raise or set likelihood when a context regex appears within a
  character window.
  <https://docs.cloud.google.com/sensitive-data-protection/docs/creating-custom-infotypes-likelihood>
  **Exclusion rules** (a dictionary, a regex, another infoType, or a hotword) remove known
  false positives.
  <https://docs.cloud.google.com/sensitive-data-protection/docs/creating-custom-infotypes-rules>
- **Each infoType has a default sensitivity:** high (cards, national IDs), moderate
  (email, phone) or low.
  <https://docs.cloud.google.com/sensitive-data-protection/docs/sensitivity-risk-calculation>

### Microsoft Presidio

- **Recognizers by entity.** EMAIL_ADDRESS, PHONE_NUMBER, CREDIT_CARD (Luhn), IBAN_CODE
  (checksum), US_SSN, LOCATION, MEDICAL_LICENSE and others. Medical-condition and
  medication entities come from an optional transformers NER model.
  <https://presidio.dataprivacystack.org/supported_entities/>
- **PatternRecognizer** combines a regex or deny-list with validation and context. In the
  source, a passing `validate_result` raises the score to the maximum and a failing one
  drops the match; a credit-card regex starts at a weak score before Luhn.
  <https://presidio.dataprivacystack.org/analyzer/adding_recognizers/>,
  <https://github.com/microsoft/presidio/blob/main/presidio-analyzer/presidio_analyzer/pattern_recognizer.py>
- **Context words** near a hit raise its score (factor 0.35, floor 0.4).
  <https://presidio.dataprivacystack.org/tutorial/06_context/>
  A `score_threshold` and **allow lists** suppress false positives.
  <https://presidio.dataprivacystack.org/tutorial/13_allow_list/>
- **A GLiNER recognizer** maps free-text zero-shot labels onto Presidio entity types.
  <https://presidio.dataprivacystack.org/samples/python/gliner/>

### AWS Macie

- **Managed data identifiers** fall into three categories: credentials, financial
  information, and personal information (PHI and PII).
  <https://docs.aws.amazon.com/macie/latest/user/managed-data-identifiers.html>
- **Custom data identifiers** are a regex plus keywords that must appear within a maximum
  match distance (default 50 characters) and **ignore words**.
  <https://docs.aws.amazon.com/macie/latest/user/cdis-options.html>
  **Allow lists** suppress known values.
  <https://docs.aws.amazon.com/macie/latest/user/allow-lists.html>
- **Severity** is low, medium or high per type and occurrence count; the highest type in a
  finding wins. <https://docs.aws.amazon.com/macie/latest/user/findings-severity.html>
  A bucket **sensitivity score** reserves separate values for "classification error" and
  "not yet analyzed" rather than folding them into "not sensitive".
  <https://docs.aws.amazon.com/macie/latest/user/discovery-scoring-s3.html>

### GLiNER PII checkpoints (for the opt-in detector)

- `urchade/gliner_multi_pii-v1` (Apache-2.0) is zero-shot. Its labels include phone
  number, email, address, credit card number, bank account number, passport number, date of
  birth, medication, medical condition, postal code and others.
  <https://huggingface.co/urchade/gliner_multi_pii-v1>
- `knowledgator/gliner-pii-base-v1.0` groups its labels into personal, contact, financial,
  healthcare and identification documents.
  <https://huggingface.co/knowledgator/gliner-pii-base-v1.0>
- `nvidia/gliner-PII` was evaluated at threshold 0.3; its usage example uses 0.5.
  <https://huggingface.co/nvidia/gliner-PII>

### Patterns these tools share

1. Checksums gate the confident band; a bare regex is weak evidence.
2. Context (hotwords or keywords) within a bounded window raises confidence.
3. Confidence is banded (Purview low/medium/high, Google's five-step likelihood) rather than
   left as an unexplained float.
4. Allow, ignore and exclusion lists are separate from detectors.
5. "Not analysed" and "error" are distinct from "not sensitive" (Macie's score).
6. Several detections combine to the strictest result (Purview label order, Macie severity).

## 3. Recommendation for memspine

### 3.1 Label set and hierarchy

Keep the RM-52 five labels, flat: **health, financial, contact, identity, location**.
**No sub-labels** for now. Each label already maps onto one policy row in
`semantics/sensitivity.py`, and labels combine pointwise-strictest (`tighten`). A sub-label
would only be justified if it changed handling, and none does today. The five labels line up
with the shared groupings above: health is PHI/healthcare; financial is financial
information; contact covers email and phone; identity covers identification documents and
national IDs; location covers addresses and postal codes.

Unlike Purview, an edge carries a **set** of labels. One claim can be both health and
location, and a set loses nothing that the strictest-combination rule needs. Ordering lives
in the policy combinator, not in the label.

### 3.2 Detector types and order

The detectors run in this fixed order. All findings are unioned, because labels only
tighten.

| # | Detector id | Input | Labels | Confidence |
|---|---|---|---|---|
| 1 | `vocabulary` | predicate name and property key against the `sensitivity` declared in every packaged pack | any | 1.0 |
| 2a | `pattern:iban` | ISO 13616 mod-97 checked | financial | 0.95 |
| 2a | `pattern:payment_card` | Luhn checked, or `card` cue | financial | 0.9 |
| 2a | `pattern:bank_account` | cue-anchored ("account number") | financial | 0.8 |
| 2a | `pattern:email` | address shape | contact | 0.95 |
| 2a | `pattern:phone` | E.164, phone cue, or phone grouping | contact | 0.75 |
| 2a | `pattern:us_ssn`, `pattern:passport`, `pattern:driving_licence` | shape or cue-anchored | identity | 0.85 |
| 2a | `pattern:licence_plate` | cue-anchored | identity | 0.6 |
| 2a | `pattern:street_address` | street shape or `address:` cue | location | 0.8 |
| 2a | `pattern:postcode` | full UK postcode shape, or code after a zip/postcode cue | location | 0.65 |
| 2a | `pattern:<kind>` | the write door's `[REDACTED:<kind>]` markers | as the kind | as the kind |
| 2b | `terms:health` | small medical-term list (below) | health | 0.7 |
| 2b | `terms:financial` | W16 first-person financial cues (`core/sensitive.py`) | financial | 0.6 |
| 2c | `write_tags` | `sensitive:<cat>` / `sensc:<cat>` tags from the write door | health, financial, location | 0.7 |
| 3 | `model:gliner2:<model>@<rev>` | opt-in GLiNER2 entity pass | any (label map in code) | model score |

The pattern detectors reuse the firewall's identifier pack (`trust/redaction.py`), so the
write door and the labeller cannot drift apart. No new dependency is added.

**Medical-term list** (`HEALTH_TERMS`, about 27 entries, kept small on purpose). It holds
unambiguous condition, treatment and medication words (diabetes, asthma, epilepsy,
hypertension, migraine, chemotherapy, dialysis, insulin, HIV, ADHD, coeliac disease,
arthritis, eczema, antidepressant, metformin, inhaler, EpiPen, pregnancy, allergy) and cue
phrases ("diagnosed with", "was prescribed", "my doctor/therapist"). Ambiguous words are
anchored to a medical context. "Cancer" alone is also a star sign, so only "has cancer",
"breast cancer" and similar count. "Depression" alone is also economic, so only "clinical",
"postpartum" or "major depression" count. Figurative phrases ("not brain surgery") are left
out. A negated mention ("I don't have diabetes") is still health information and is
labelled.

**Opt-in model.** The model detector is bound through `models.bindings.gliner_sensitivity`
(a GLiNER profile with task `entities`). It is off unless bound and never a generative model.
Its zero-shot labels are: medical condition, medication, symptom, phone number, email
address, bank account number, credit card number, passport number, national identification
number, date of birth, street address and postal code. It returns label and score only;
mention text never leaves the call. A malformed model output fails closed: the check becomes
incomplete.

### 3.3 Confidence model

- Per label, confidence is the **maximum** over the detectors that fired. The detector ids
  are kept alongside it (`detectors`), so a reader can see why.
- The bands follow the shared practice: 1.0 for a declared vocabulary default, 0.9 and
  above for checksummed or unambiguous shapes, 0.75 to 0.85 for structured identifiers and
  cue-anchored values, 0.6 to 0.7 for lexicon cues, write-door tags and shape-only codes.
- `min_confidence` (default 0.5) drops weaker hits. Every deterministic detector scores at
  least 0.6, so the threshold only affects model hits. A model hit with no score is kept at
  0.5, because labels only tighten.
- Confidence is stored on the edge (`sensitivity_confidence`). Filters do not threshold on
  it today; a label present is a label applied.

### 3.4 Three states

Every edge records one of three states (`EdgeQualifiers.sensitivity` plus
`sensitivity_checked`):

| State | `sensitivity` | `sensitivity_checked` | Meaning |
|---|---|---|---|
| labels found | non-empty set | `True` | complete check; these labels apply |
| checked_none | empty set | `True` | complete check found nothing. Not a proof that the claim is harmless (sensitive content can appear under any predicate, RM-52) |
| incomplete | set (maybe empty) | `False` | an enabled detector failed or did not run; the labels found are kept, absence is unknown |
| not checked | `None` | `None` | no labeller ran |

"Incomplete" is a variant of not checked, following Macie, which keeps "classification
error" apart from "not sensitive". For filters it behaves as not checked, except that a
found excluded label still drops the edge.

**Filter rules** (`QualifierFilter`, RM-86):

- **`sensitivities` (allow-set):** passes when the edge carries any listed label.
  Checked_none and not-checked edges fail.
- **`exclude_sensitivities`:** a labelled match is dropped. Checked_none, and a complete
  check without an excluded label, pass. A not-checked or incomplete edge follows
  `unchecked_sensitivity` (`"fail"` by default, the safe choice, or `"pass"`).
- **The other exclusions:** `exclude_claim_states`, `exclude_lifecycle_statuses`,
  `exclude_assertion_modalities`, `exclude_topical_domains` and `exclude_origin_types`.
  A known listed value drops the edge; an unknown value passes. Combine an exclusion with
  its allow-set to also require a known value. Unknown still fails every allow-set.
- **Erased and erasure-pending edges** never pass (RM-83).

### 3.5 Labeller identity and version

Every result records the labeller id `memspine.sensitivity` and a version string
`rules-<RULES_VERSION>+vocab-<12-hex digest of the default table>[+<model id>][+min-<threshold>]`.
The version changes whenever the labels could change: a rule edit bumps `RULES_VERSION`, a
vocabulary edit changes the digest, binding a model appends its id.

Re-running `compile_edges` is the backfill. It relabels every live compiled edge and
appends LINK events only for edges whose stored qualifiers changed, so an unchanged version
re-projects idempotently.

### 3.6 Offline-only constraints

- The deterministic detectors are pure Python regexes and checksums: no network, no model,
  no new dependency.
- A GLiNER sensitivity binding must set `local_files_only: true`. The config validator and
  the detector both refuse otherwise. Weights load through the shared GLiNER pool
  (`models/runtime/gliner_runtime.py`), which validates a complete local snapshot before
  constructing the SDK.
- No generative model and no paid or remote API is used for labelling.
- Edge properties hold labels, confidences and detector ids only, never the matched text.

### 3.7 Evaluation fixtures

The fixtures live in `tests/unit/derive/test_sensitivity.py` and act as the RM-98
evaluation set.

- **Positives per detector:** every vocabulary default (including proposed-pack health and
  financial predicates); email, international and parenthesised phones; a valid IBAN; a
  Luhn-valid card; bank account, SSN and passport numbers; a street address; UK and cued
  postcodes; redaction markers; medical terms including a negated mention; financial cues;
  write-door tags.
- **False-positive guards:** a Luhn-failing card number, a mod-97-failing IBAN, a version
  string, an IP address (deliberately unmapped), an ISO date, a room number, a road name
  ("M25"), a secret, "Cancer" as a star sign, "the Great Depression", "not brain surgery",
  "prescribed reading", ordinary anxiety and an ordinary payment.
- **Documented known false positive:** "allergic to Monday meetings" is labelled health.
  This is accepted, because labels only tighten.
- **Documented known miss:** "heart murmur" is outside the term list. The `has_condition`
  vocabulary default or the opt-in model covers it.
- **States:** labelled with several labels and confidences, checked_none, not checked,
  incomplete after a model failure, malformed model output, and determinism.
- **Exclusion semantics** (`test_qualifier_exclusions.py`) and **compiler integration**
  (`test_rm99_sensitivity_edges.py`): labels on compiled edges, idempotent re-projection,
  exclusion and allow filters on real compiled edges, the opt-in model path, and the
  offline-only configuration check.

## 4. What was not adopted, and why

- **Purview's single label per item and its sub-label hierarchy.** Memspine needs several
  labels per claim (RM-87), and strictest-combination already orders policy. Sub-labels
  changed no handling.
- **Exact data match and dictionaries of real values.** These need a store of the very
  secrets being protected. Out of scope for a memory engine, and a liability.
- **Trainable classifiers and document-level ML.** Labels are per claim, not per document,
  and a training loop contradicts deterministic defaults. The opt-in GLiNER pass is the
  only model.
- **Proximity-window context boosting (Purview, Google hotwords, Presidio context, Macie
  keywords).** Claim texts are short (one fact), so the whole value is the window. The cue
  is folded into the pattern instead ("passport number: X", "zip code 94110").
- **User-editable allow and ignore lists.** Not needed while the term list is tiny.
  Proposed as a follow-up if false positives show up in practice; they would run last, as in
  Presidio, Macie and Google.
- **Occurrence-count severity (Macie).** One sensitive value in a claim already applies the
  policy. Counting does not tighten further.
- **IP addresses, secrets and home-directory user names as labels.** IPs in agent memory are
  mostly infrastructure. Secrets are the firewall's redaction job (`firewall.pii`), not a
  sensitivity label. Neither fits the five labels.
- **GDPR special categories beyond health (religion, politics, ethnicity, sexual
  orientation) and legal matters.** The write door tags these already (W16/I52 `sensitive:`
  and `sensc:` tags), but the owner-selected label set has no label for them. Mapping them
  to `identity` would blur that label. A future label extension needs its own decision.
- **Remote or paid DLP APIs** (Google DLP, Macie). These break the offline constraint and
  cost money.
- **Aliasing free-form relation names onto vocabulary predicates** (for example `lives_in`
  to `resides_in`). Defaults apply only to exact vocabulary names; guessing an alias would
  invent semantics.
