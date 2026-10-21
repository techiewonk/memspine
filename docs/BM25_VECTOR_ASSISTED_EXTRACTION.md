# BM25 and word-vector-assisted ingestion extraction

Date: 2026-10-18. Non-generative design refinement; no model selected/downloaded or engine implementation changed. Eligible under the accepted optional non-generative model policy. Generative ingestion calls remain zero.

## Conclusion

Yes: BM25 plus word/static/sentence vectors can assist actual extraction, not only final-answer retrieval. Use them to select relevant source context, propose predicate/property labels, and prioritise extraction rules. A trained non-generative classifier or sequence tagger may additionally use vector features for relation labels and token spans. Bare vector similarity alone does not produce a validated subject–predicate–object record.

Word2vec exposes word representations and similarity lookup, not a complete relation extractor. [Official Gensim Word2Vec documentation](https://radimrehurek.com/gensim/models/word2vec.html). BM25S indexes tokenized corpora and returns matching items; it does not by itself establish identity, roles or factual support. [Official BM25S README](https://github.com/xhluca/bm25s).

## Separate two search spaces

| Search space | Indexed content | Result | Rule |
|---|---|---|---|
| Semantic-definition index | Versioned predicate/property descriptions, role constraints, reviewed synthetic paraphrases | Candidate extraction labels/rules | Definitions guide interpretation but never support a personal fact |
| Prior-evidence index | Authorised original messages and grounded facts with original evidence pointers | Cited identity/place/event/date candidates | Enforce scope, source lifecycle and known-time boundary |

Do not mix registry descriptions into the personal-memory corpus. Do not index benchmark QA/gold as extraction examples. Version the definition index with the registry digest; rebuild its projection when definitions change.

## Proposed extraction flow

1. Segment original source with stable offsets; keep negation/modal words and source clocks intact.
2. Run exact rules/NER for candidate actors, objects, places, numeric spans and temporal phrases.
3. BM25 retrieves up to five registry-label candidates; optional vectors retrieve up to five semantic candidates. Union and rank at most eight unique label/rule candidates per segment. These are additional proposed small label-selection limits, not previously measured defaults.
4. Fuse ranks or calibrate a separate classifier; never add incompatible raw BM25/cosine scores directly or use a universal cosine acceptance threshold.
5. Try compatible tested rule/slot extractors: actor, predicate, property, object, event roles, polarity, modality and time. Optional discriminative tagger outputs source spans, not trusted IDs or grounding.
6. Retrieve bounded prior evidence only where bindings are missing. Registry examples are not evidence for those bindings.
7. Compile against typed registry constraints and exact support/binding spans. Validate numbers/units, role direction, source identity and temporal anchors.
8. Persist grounded effects through the canonical fenced gateway; unsupported/ambiguous interpretation becomes an unresolved annotation. Keep raw text searchable.

Existing accepted context/candidate/model/worker limits remain outer caps; label-selection work cannot multiply them without accounting. No per-predicate model call. Batch inference under the explicitly configured provider, pin model/settings/registry versions, reuse source-version-keyed results and isolate user result caches.

## Examples

| Source | What vectors/BM25 may propose | What extraction must validate |
|---|---|---|
| I relocated from Porto to Lisbon | Move-related rule family | Exact origin/destination roles and actual completed assertion |
| I might relocate to Lisbon | Same move candidates | Hypothetical/planned modality; no completed move or current residence |
| I did not attend the class | attended candidate | Negation retained; no positive attendance |
| The studio offers yoga | offered/attended candidates may both rank | Subject is studio; offering is not attendance |
| A friend made this guitar | made/gave candidates | Maker only; gift/ownership cannot be inferred |
| I covered five kilometres on foot | Measurement/walking candidates | Distance slot/unit and actor; unsupported paraphrase stays unresolved if no tested extractor |
| My home country is Sweden | home_country candidate | Speaker/subject and explicit country binding; no nationality inference |

These are intended behaviours, not claims that the current rules already support each paraphrase. Opposites and related verbs can be near each other in embedding space; candidates are never accepted merely because they rank first. Out-of-vocabulary/zero-vector/no-match cases revert to lexical/rule candidates, not guessed facts.

## What current code provides

- `models/encoders/embedding/static_local.py::StaticEmbedder` wraps model2vec static embeddings, normalises outputs and performs inference in a thread. Model2vec is not the same algorithm/library as classic Word2Vec.
- `read/pipeline/rerank.py::_static_embedding_prefilter` already ranks read candidates with static vectors. It is a read-side feature, not a wired ingestion extractor.
- `config/schema/providers.py::StaticEmbeddingSettings` provides explicit static model/dimension settings.
- Existing static model loading uses `StaticModel.from_pretrained`; this can fetch weights. Therefore the accepted no-auto-download ingestion policy is NOT established by merely reusing that loader. Future ingestion integration needs local-artifact/offline enforcement, explicit task binding and observable missing-model behaviour.
- Existing lexical provider ports can retrieve source candidates, but registry-label indexing must be a separate immutable semantic-definition projection with no user data or provider-specific fact writer.

Recommendation: evaluate reusing the configured static embedding capability before adding a new Word2Vec dependency. Compare classic word vectors, current static embeddings or sentence embeddings only where they improve candidate recall/accepted extraction precision at acceptable kernel cost. No model quality or speed superiority has been measured here.

## Validation before activation

Measure predicate-candidate recall@k separately from grounded slot precision, identity/role errors and unresolved rate. Include antonyms, negation, plans, offered/attended, owned/used, same-name people, unknown places and source erasure. Model-unavailable tests must not download weights or call a generative model. Registry labels and negative examples must remain generic; independently authored holdouts are needed after full LoCoMo inspection.

This extends the [semantic structure guide](SEMANTIC_REGISTRY_STRUCTURE_AND_USAGE.md). Proposed extra label-selection caps require normal configuration review before runtime implementation.

## CPU non-generative alternatives review — 2026-10-18

[CPU_NON_GENERATIVE_MODEL_OPTIONS.md](CPU_NON_GENERATIVE_MODEL_OPTIONS.md) compares sparse classifiers, static vectors, fastText, spaCy, small GLiNER extraction, sentence encoders and tiny rerankers with current code/packaging. Recommendations are research candidates, not model switches or measured speedups. Reuse named task profiles; no downloads, training, runtime changes or generative ingestion calls introduced.

## Verified under-250M checkpoint shortlist — 2026-10-18

[MODELS_UNDER_250M.md](MODELS_UNDER_250M.md) records 16 checkpoints with metadata-verified totals below 250M and pinned revisions. Model2Vec/GLiNER2.5 remain preferred families; exact profiles/model switches require kernel and grounding checks. GLiClass Edge, small contextual encoders, tiny rerankers and extractive QA are role-specific alternatives, not mandatory serial stages. Two models with unknown totals were excluded. No weights downloaded or inference performed.
