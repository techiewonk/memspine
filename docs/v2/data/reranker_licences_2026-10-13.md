# Reranker licence check: permissive fallback for Jina (X-06)

Date: 2026-10-13. Scope: desk research only (VERIFY.md Q6, R-14). No weights downloaded, no benchmarks run, no quality ranking
claimed. Licences were read from the Hugging Face model API metadata (`cardData.license`) and the card README / `config.json`
of each repository on 2026-10-13. Card URLs are `https://huggingface.co/<model id>`.

## 1. The Jina model the engine uses

- Model id: `jinaai/jina-reranker-v3.5` (`DEFAULT_MODEL` in `src/memspine/services/rerank/jina_rerank.py`). Card: https://huggingface.co/jinaai/jina-reranker-v3.5
- Licence identifier on the card: `cc-by-nc-4.0`. Card text: "`jina-reranker-v3.5` is licensed under CC BY-NC 4.0. For commercial usage inquiries, feel free to contact us." (https://jina.ai/contact-sales/).
- 596,836,352 parameters (safetensors metadata), Qwen3-0.6B base, multilingual, loads with `trust_remote_code=True`.
- Consequence: non-commercial. A commercial deployment needs a licence from Jina or must use a fallback below. (This is the card's statement, not legal advice.)

## 2. Candidate fallbacks

"Adapter" = existing adapter in `src/memspine/services/rerank/`. Params from HF safetensors metadata. "Max len" is the model
`config.json` `max_position_embeddings` unless a card states otherwise; the practical limit in an adapter may be lower
(the Qwen3 adapter caps at `max_length=8192`).

| Model id | Licence (card) | Commercial use | Multilingual | Params | Max len | Adapter | CPU feasibility |
|---|---|---|---|---|---|---|---|
| `jinaai/jina-reranker-v3.5` (reference) | cc-by-nc-4.0 | NO (contact Jina) | yes | 597M | 131072 (config) | jina | slow, same class as Qwen3-0.6B |
| `Qwen/Qwen3-Reranker-0.6B` | apache-2.0 | yes | yes, 100+ languages (card) | 596M | 32k (card) | qwen3 (its default model) | feasible but LLM-class cost per pair; `quant` option is for bitsandbytes (GPU) |
| `Qwen/Qwen3-Reranker-4B` | apache-2.0 | yes | yes, 100+ | 4.02B | 32k (card) | qwen3 | impractical on CPU; GPU advised |
| `BAAI/bge-reranker-v2-m3` | apache-2.0 | yes | yes (card tag "multilingual") | 568M | 8194 (config); card examples use 512 | none (needs a CrossEncoder-style adapter; fastembed support UNVERIFIED) | feasible, XLM-R large class, slow-ish |
| `BAAI/bge-reranker-base` | mit | yes | UNVERIFIED | 278M | UNVERIFIED | none verified (fastembed list UNVERIFIED) | feasible |
| `mixedbread-ai/mxbai-rerank-xsmall-v1` | apache-2.0 | yes | UNVERIFIED (believed English) | 71M | 512 (config) | none | good |
| `mixedbread-ai/mxbai-rerank-base-v1` | apache-2.0 | yes | UNVERIFIED (believed English) | 184M | 512 (config) | none | good |
| `mixedbread-ai/mxbai-rerank-large-v1` | apache-2.0 | yes | UNVERIFIED (believed English) | 435M | 512 (config) | none | feasible, slow |
| `mixedbread-ai/mxbai-rerank-base-v2` | apache-2.0 | yes | yes, 100+ languages (card) | 494M | 32768 (config); card says "long-context support" | none (Qwen2 causal-LM based, own API) | feasible, LLM-class |
| `mixedbread-ai/mxbai-rerank-large-v2` | apache-2.0 | yes | yes (card) | 1.54B | 32768 (config) | none | slow on CPU |
| `Alibaba-NLP/gte-multilingual-reranker-base` | apache-2.0 | yes | yes, 70+ languages (card) | 306M | 8192 (card) | none; needs `trust_remote_code=True` (same exposure as Jina) | feasible |
| `cross-encoder/ms-marco-MiniLM-L-6-v2` (HF canonical id `cross-encoder/ms-marco-MiniLM-L6-v2`) | apache-2.0 | yes | no (English MS MARCO) | 22.7M | 512 (config) | fastembed, via the ONNX copy below | good, the small CPU option |
| `Xenova/ms-marco-MiniLM-L-6-v2` (fastembed default, `constants.RERANK_FASTEMBED_MODEL`) | no licence field on the card; `base_model: cross-encoder/ms-marco-MiniLM-L-6-v2` (Apache-2.0). Apache-2.0 by derivation; UNVERIFIED as a statement by the Xenova repo | yes, if derivation holds | no | ~22M (ONNX; HF reports none) | 512 (base) | fastembed | good |
| FlashRank default `ms-marco-TinyBERT-L-2-v2` and other FlashRank models | FlashRank code: Apache-2.0 (repo LICENSE). Model files: HF repo `prithivida/flashrank` is `cc-by-sa-4.0`; FlashRank `Config.py` comment: "THE HF Repo is UNDER CC-BY-SA LICENSE. USE APPROPRIATELY." | code yes; weights allow commercial use but ShareAlike and attribution terms apply (not a plain permissive licence) | default no; `ms-marco-MultiBERT-L-12` multilingual by name only (UNVERIFIED) | TinyBERT-L-2 ~4MB per README | UNVERIFIED | flashrank | excellent |
| `jinaai/jina-reranker-v1-tiny-en` | apache-2.0 | yes | no (en, by name) | 33M | 8192 (config) | fastembed support UNVERIFIED | good |

Also in the repo: the `litellm` adapter for cloud rerank APIs (e.g. Cohere); terms are the provider's, out of scope here.

## 3. Proposal (for the owner)

- **Primary permissive fallback: `Qwen/Qwen3-Reranker-0.6B` (Apache-2.0).** Reasons: same size class and multilingual coverage as
  Jina v3.5; already wired and defaulted in the `qwen3` adapter, so no code is needed; no `trust_remote_code`; 32k context per the card.
  Alternative needing a new adapter: `BAAI/bge-reranker-v2-m3` (Apache-2.0, multilingual, 568M).
- **Small CPU fallback: `cross-encoder/ms-marco-MiniLM-L-6-v2` (Apache-2.0) through the fastembed adapter**, which is already the
  in-core default (`Xenova/ms-marco-MiniLM-L-6-v2`). Reasons: 22M parameters, permissive, already shipped. English only, 512 tokens.
  Do not present FlashRank's bundled weights as the "permissive" CPU choice without a decision on CC-BY-SA-4.0 ShareAlike.
- No quality ranking is claimed here; the choice rests on licence, size, language scope and existing wiring only. Any quality
  comparison is out of scope for X-06.

## 4. Unverified items

- Language scope and (for bge-reranker-base) max length for `BAAI/bge-reranker-base` and the mxbai v1 models (cards not read in detail).
- Whether fastembed's supported-model list includes bge-reranker-v2-m3, bge-reranker-base or jina-reranker-v1-tiny-en (not checked).
- Licence of the Xenova ONNX conversions (no licence field; derived from the Apache-2.0 base model).
- FlashRank per-model size, max length and languages beyond the README tagline; legal effect of CC-BY-SA-4.0 on bundled use.
- Parameter counts for ONNX-only repos (HF reports none).

## Change log

- 2026-10-13: X-06 first version; licences read from the HF model API and cards, no weights fetched.
