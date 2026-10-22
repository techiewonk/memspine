# GLiNER2.5 feasibility for MemSpine ingestion

> Later runtime evidence: [local Base/Small results](GLINER25_LOCAL_RESULTS.md).
> The initial source-only survey below is historical; real offline SDK inference
> now passes on Python 3.13. Engine lifecycle/grounding integration remains separate.
>
> 2026-10-18 architecture update: [named model profiles](MULTI_MODEL_PROFILES.md)
> supersede the single `models.gliner` field without an alias. Task bindings select
> GLiNER profiles; identical effective settings share the Engine-owned runtime.
> [ADR-076](adr/ADR-076-grounded-gliner-mention-ingestion.md) defines grounded
> unasserted annotations. See [window grounding](GLINER_GROUNDING_WINDOW.md) and
> [real ingestion acceptance](GLINER_INGESTION_ACCEPTANCE.md) for the current
> implementation and gate results. The dated survey below preserves historical
> observations; its old field names and pending statuses are not current config.

Feasibility was checked 2026-10-17 against the published `gliner2` 2.1.0
source, MemSpine's adapters and four revision-pinned Hugging Face checkpoint
configs. The loader/default changes recorded below were implemented the same
day. Owner scope: focus on ingestion; unrelated evaluation migrations are
deferred. No model weights were downloaded and no inference benchmark was run.

## Conclusion

GLiNER2.5 is feasible as an optional local extraction stage. The package remains
`gliner2[local]>=2.1.0,<3`; **2.5 names the model architecture/family**, not a
required `gliner2==2.5` package release. Existing dependency metadata permits
Python 3.13. Full local model loading/inference on our Python 3.13 stack remains
the next runtime gate; upstream's README explicitly lists its tests as 3.10–3.12.

Use `AutoExtractor.from_pretrained(...)`. In the checked SDK, `auto.py` reads
`config.json`, resolves `architecture`, and dispatches to the matching class.
The old `GLiNER2` entry point is span-only. Setting only a new checkpoint name
in the current code would therefore fail for the boundary models.
Sources: [upstream](https://github.com/fastino-ai/GLiNER2),
[published package](https://pypi.org/project/gliner2/).

## Model choices

Counts below are the Hub's reported safetensors parameter totals, not RAM or
latency measurements. Each config was downloaded at its recorded commit.

| Model | Architecture | Reported parameters | Suggested use |
|---|---|---:|---|
| [fastino/gliner2.5-base-v1](https://huggingface.co/fastino/gliner2.5-base-v1/blob/54785d51df8d86a0d8f2eb212fec15871aad2c4d/config.json) | boundary | 193,581,591 | Recommended first extraction quality baseline |
| [fastino/GLiNER2.5-Decide](https://huggingface.co/fastino/GLiNER2.5-Decide/blob/9d1bfb848cd16d93ee56ccbc59cd92bf0f284d90/config.json) | span | 486,444,053 | Dedicated classification candidate; do not confuse it with the boundary extractor |
| [fastino/gliner2.5-multi-v1](https://huggingface.co/fastino/gliner2.5-multi-v1/blob/cf5593a5d45e3bbf204b9df621b13b1c0cf25ee3/config.json) | boundary | 287,355,159 | Multilingual conversations; evaluate target languages |
| [fastino/gliner2.5-small-v1](https://huggingface.co/fastino/gliner2.5-small-v1/blob/df5910e44bc4ffdb0d95399a83b0ca4516349aa5/config.json) | boundary | 73,881,879 | Speed/cost comparison; measure recall before choosing |

The three boundary configs enable record extraction, relations and abstention.
That establishes configured capability, not trained accuracy for our schemas.
The English Decide checkpoint is span architecture despite its 2.5 name. Its
Hub parameter total also differs from the overview README's size label; use the
pinned artifact metadata when planning storage. Do not load multiple models
by default merely because each supports a different task.

## Current code gaps and recommended changes

| Area | What code does today | Required/recommended change | Status |
|---|---|---|---|
| Semantic NER loader | `memories/semantic/entities.py` imports `GLiNER2` directly | Use the architecture-dispatching `AutoExtractor` | Implemented 2026-10-17; fake SDK test only |
| Decision loader | `services/decider/gliner2.py` prefers `GLiNER2` before `AutoExtractor` | Use `AutoExtractor` directly; no legacy-loader shim needed | Implemented 2026-10-17; fake SDK test only |
| Checkpoint settings | NER has a module constant; decision has an independent config default | Centralize configurable model/revision/device/task settings through the agreed provider approach | Planned design work |
| Current defaults | Both paths name `fastino/gliner2-base-v1` | Select `fastino/gliner2.5-base-v1` for the proposed baseline; retain explicit model choice | Implemented for decision, semantic NER, and prepass on 2026-10-17 |
| NER result | Converts entity names into facts with `attribute="mention"` | Keep mentions separate from asserted facts and typed relationships | Required before richer deposits |
| Graph seed result | Decision `entities()` returns names only | Preserve source message, type, confidence and exact spans for ingestion evidence | New output mapping needed |
| Structured extraction | Existing GLiNER adapter does not request records/relations | Add explicit schemas and convert results to engine envelopes | Separate capability; not enabled by a model swap |
| Provenance | Names alone cannot justify a graph assertion | Validate offsets against exact source text, speaker, namespace and message IDs | Required before persistence |
| Time and polarity | A detected phrase does not normalize relative dates or establish truth | Use existing temporal/grounding rules; preserve negation, assertion owner and uncertainty | Existing engine responsibilities remain |
| Model lifecycle | Semantic extractor loads in its constructor; decision path caches lazily | One owned model instance per effective configuration, bounded/off-thread loading and inference | Recommended engineering work |
| Hub loader options | The checked AutoExtractor splits Hub kwargs but forwards only model kwargs to the selected class; boundary weight/config loading reads its own Hub kwargs | Verify revision/offline propagation; prefer a verified complete local snapshot for deterministic loading until resolved | Source-inspected risk; runtime check pending |
| Caching | Richer output/schema changes producer identity | Include checkpoint revision, task schema/version and relevant settings in cache identity | Required for cache correctness |
| Performance | No local 2.5 measurements yet | Measure model load, first/warm inference, memory and extraction coverage | Pending runtime check |

The three paths currently share the same default, but an explicit `decision.model`
override does not change the semantic NER or prepass model. Provider-wide settings
and shared model ownership remain separate work. Entities are candidates, not
automatically trusted facts; do not use a source span as proof of a relation the
text never states.

## Implementation status — 2026-10-17

The decision adapter, semantic NER loader and prepass finder now resolve only
`gliner2.AutoExtractor`; no `GLiNER2` legacy-loader fallback remains. Their
default checkpoint is `fastino/gliner2.5-base-v1`, with the decision schema
default and generated config references refreshed. The semantic and prepass
constructors still accept an explicit model id.

Focused tests use fake `gliner2` modules and verify loader selection, the shared
default and explicit model override. The checks passed without downloading model
weights or running inference. They do not establish that the boundary checkpoint
supports every decision/classification call shape, that the local stack loads on
Python 3.13, or that extraction quality meets MemSpine's needs. No real-model
smoke test, held-out quality benchmark, central model ownership, or richer
record/relation/provenance integration is claimed.

The inspected `gliner2` 2.1.0 `AutoExtractor` path appears not to forward
`revision` and `local_files_only` through to the boundary model's weight loader.
The adapters do not claim pinned or offline weight loading; deterministic
revision use still requires a verified complete local snapshot or an upstream
loader fix and an offline integration test.

## Proposed ingestion placement

```text
Validate and resolve sender/session/conversation metadata
  -> persist accepted raw message
  -> durable extraction job / bounded same-conversation window
  -> shared GLiNER2.5 model: requested entity/record/relation schema
  -> normalize and validate spans + source ownership
  -> existing grounding / temporal / conflict integration
  -> facts, graph projection and search indexes
  -> optional existing LLM enrichment for unresolved information
```

> **Superseded for the target ingestion profile (D-99 / ADR-077).** The selected background
> ingestion profile is non-generative: unresolved information stays unresolved, and no
> generative call or hidden fallback is permitted there. The final "optional LLM enrichment"
> step above predates that decision; any future generative enhancement needs a separate
> explicit policy. See [the background ingestion plan](BACKGROUND_INGESTION_PLAN.md).

The stage does not replace raw persistence or become another ingestion API.
It can reduce generative extraction work, but it still consumes inference compute.
Do not promise a latency gain or an LLM-call reduction until measured. Cross-turn
inference, implicit preferences and temporal reasoning may still need the LLM.
Batch within explicit conversation boundaries and preserve each message's offsets;
a merged text span must map back to its source message unambiguously.

## Staged implementation and focused checks

| Stage | Work | Ingestion-focused acceptance gate |
|---|---|---|
| 1 | Choose Base; keep Small/Multilingual configurable | One explicit checkpoint/revision and settings record |
| 2 | Replace both loading paths with AutoExtractor; share model ownership | Stub boundary checkpoint chooses correct loader; loading errors are clear; model loads once |
| 3 | Connect entity output to ingestion | Historical timestamps, sender IDs, reply/conversation ownership and exact spans survive |
| 4 | Add structured records and typed relations separately | Reject unsupported subjects, wrong offsets, cross-tenant endpoints and ungrounded relations |
| 5 | Run real local inference on Python 3.13 | Model loads; one synthetic message and one multi-message window return validated envelopes |
| 6 | Compare Base versus Small | Fixed synthetic held-out corpus; precision/recall, p50/p95, peak memory and model-call counts |

Scope tests to the changed ingestion/adapter contracts. Broad old evaluation-arm
migration remains a separate workstream, per the owner's instruction.

## Illustrative SDK use (not a new Engine configuration API)

```python
from gliner2 import AutoExtractor

extractor = AutoExtractor.from_pretrained(
    "fastino/gliner2.5-base-v1",
)
result = extractor.extract_entities(
    "Mira works at Acme in Porto.",
    ["person", "organization", "location"],
    include_confidence=True,
    include_spans=True,
)
# Validate source spans and ownership before converting this into engine records.
```

The config evidence is revision-pinned; the simple usage snippet above is not
a production revision-pinning recipe. The checked AutoExtractor appears to drop
Hub options before delegating weight loading. Verify the complete config/weight/
tokenizer revision path or use a verified complete local snapshot. Inference
exposes confidence/span options in the checked runtime source. This snippet is source-verified but not
executed against model weights in this review. Do not call it from an asyncio
request path without the engine's bounded model-work execution.

## Verification scope

- Read the SDK loader, public class exports, schema builder and inference runtime.
- Read both MemSpine adapters, configuration and semantic-extractor wiring.
- Used `hf models info` and downloaded only four `config.json` files at their SHAs.
- No installation of the heavy local stack, model inference, production call,
  checkpoint conversion or broad unrelated test run was performed in this review.
- Evidence: [gliner25-feasibility.json](provider_verification/gliner25-feasibility.json)
  records revisions, public metadata, config hashes and checked SDK source hashes.
