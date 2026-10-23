# Multiple models and explicit reasoning levels

> **Named profiles were removed by [ADR-082](adr/ADR-082-model-slots.md).**
> `models.profiles` and `models.bindings` are rejected with an error naming the
> new key. Every consumer now states its provider and settings inline in its own
> slot of the `model` block. See [USAGE "Model slots"](USAGE.md#model-slots) for
> the slot shape and the old-key map; `scripts/migrate_config_keys_adr082.py`
> rewrites old configs (it expands each binding into an inline slot).

Each model consumer has one slot: `model.llm.<role>`, `model.extraction.<consumer>`,
`model.decider.<consumer>`, `model.rerank.chain`, `model.embedding`, `model.vision`.
A slot is `{provider, <typed common keys>, settings}`. A consumer without a slot is
off. Declaring a slot does not load weights or, for `semantic` and `prepass`, enable
the feature (see below).

The example's `deployment.id` is a stable application ownership label required by
the shared-service provider contract, including localhost endpoints. It does not
require production hosting or a Temporal cluster. Endpoint models, authentication
and cached checkpoint availability still need to match the local installation
before invoking them.

| Purpose | Slot | Example |
|---|---|---|
| Fast window extraction | `model.llm.window_extract` | small local model |
| Conflict verification | `model.llm.judge` | stronger model, optional `thinking` |
| Answer verification | `model.llm.verify_answer` | stronger model |
| User-facing generation | `model.llm.chat` | independently selected |
| Decision classification (H24 routing) | `model.decider.route` | GLiNER classification checkpoint |
| Semantic entity mentions | `model.extraction.semantic` | GLiNER entity checkpoint |
| Window entity prepass | `model.extraction.prepass` | same checkpoint, or a different one |
| Topic tagging (offline) | `model.extraction.topic` | GLiNER classification, `labels`, `min_confidence` |
| Sensitivity mentions (offline) | `model.extraction.sensitivity` | GLiNER entity checkpoint |

"System 1" and "System 2" describe intended roles, not guaranteed quality or
latency. A small generative model can serve routine work, a specialized GLiNER
classifier can serve classification, and a stronger generative model can serve
verification. Their APIs are different: a GLiNER slot is an extraction or decider
slot and cannot appear under `model.llm` or in an escalation chain.

## Settings, ownership and validation

An LLM slot types `timeout_s` (required), `structured` (`retry_on_error`,
`constrained_retry`), `thinking` (token budget; `judge` / `reflect` only, P-15) and
`escalation` at its root. Model id, endpoint, temperature and generation defaults
go in `settings`, validated by the selected provider; `settings.timeout_seconds`
is rejected (use `timeout_s`). A GLiNER (`gliner2`) slot's settings are strict:
checkpoint `model`, `revision`, CPU/CUDA `device`, `local_files_only`, `tasks` and
`inference_concurrency`. `settings: {}` receives the default Base checkpoint, CPU,
local files only, entities/classification and concurrency one. The slot's model
must expose the task its consumer needs (`semantic`, `prepass`, `sensitivity`:
`entities`; `topic`, `route`, `read`: `classification`). `topic` and
`sensitivity` are offline only and need `settings.local_files_only: true`.
Declare a pinned revision when reproducible artifacts are required.

`semantic` and `prepass` stay switched by
`memories.semantic.policies.entity_extraction: gliner` and `write.prepass: gliner`;
a switch on without its slot is a config error naming the slot. `topic` and
`sensitivity` are enabled by their slot. `read.planner: decision` needs
`model.decider.route`.

GLiNER weights are keyed by the effective settings minus the consumer-only fields
`tasks` and `inference_concurrency` (ADR-082 item 4, ADR-079), that is checkpoint,
revision, device and offline policy. Slots that differ only in those fields share
one load (each keeps its own concurrency bound), and identical weights are shared
by every Engine in the process. Different checkpoints load separately. Set
`model.placement.max_loaded_models` for the resident checkpoint bound per Engine
(default one); an idle checkpoint may be evicted and reloads on its next call, and
a pool whose checkpoints are all busy fails rather than exceeding that bound. LLM
providers are owned by the Engine lifecycle. Sharing a resource never authorizes
sharing namespace-specific extraction results.

Provider factories receive the logical task role and an optional stable
`LLMBuildContext.resource_key`. Each role slot keeps its role-based ownership key.
The ownership key is not the weight key: llama.cpp loads are keyed by the absolute
GGUF path plus native load options (`n_ctx`, `n_gpu_layers`, `n_batch`,
`n_threads`), so two roles on the same file share one native model per process;
the last release closes it.

LLM cache identities include normalized native provider settings; an escalation
identity also includes its ordered targets and effective policy. Changing an
endpoint, generation setting, target or policy invalidates the producer identity.
Secret values are hashed, not exposed in identities. Metrics retain the actual
attempt's adapter identity and count each call under its role; sharing a provider
does not serialize calls merely for usage accounting.

Unknown roles, missing tasks and invalid native settings fail at config load,
before model execution. Unused slots do not justify loading optional SDKs or
weights.

An explicit `model.llm.<role>` slot takes precedence over automatic
`model.placement.tiers_v2` selection. The tier catalogue
(`model.placement.catalogue`) remains the optional placement planner; declaring
slots alone does not turn tier planning on.

## Escalation

`model.llm.<role>.escalation` lists ordered inline `targets`, each itself an LLM
slot (`provider`, `timeout_s`, `settings`) that must differ from the primary. The
primary is the role's own slot. Escalation is absent by default and may trigger
only on selected failure conditions:

```yaml
model:
  llm:
    extract:
      provider: litellm
      timeout_s: 60
      settings: {model: ollama/qwen3.5:9b, api_base: http://localhost:11434}
      escalation:
        triggers: [error, invalid_json]
        max_attempts: 2
        timeout_s: 120
        max_tokens_per_attempt: 2048
        targets:
          - {provider: litellm, timeout_s: 120, settings: {model: ollama/qwen3.5:27b}}
```

| Trigger | Meaning | Does not establish |
|---|---|---|
| `error` | Supported runtime/provider failure | That every programming/configuration error is retryable |
| `empty` | Empty/whitespace output | Semantic answer completeness |
| `invalid_json` | Strict JSON parse failure | Schema validity, factual correctness or calibrated confidence |

Select `invalid_json` only for roles whose configured prompts require JSON;
YAML or free-text outputs are not JSON failures. The example's timeouts and
output limits illustrate configuration and are not quality-tested task budgets.

Each policy requires a total deadline (`timeout_s`) and a per-attempt output cap
(`max_tokens_per_attempt`); `max_attempts` (at least 2 with targets, at most
1 + the number of targets) limits the actual calls within one routed `chat`
invocation. Existing outer structured retries or agent loops are separate
invocations with their own budgets; this policy is not a whole-Engine-operation
call limit. Caller cancellation propagates. Each candidate keeps its own
remote-data consent gate, including a remote fallback behind a local primary.
Failed/invalid outputs must not be silently returned on exhaustion. Generation
limits depend on provider enforcement; they are not proof of remote billing limits
or a bound on hidden reasoning tokens. Input context also consumes tokens; no
global monetary budget is implied.

For semantic review, give `judge`, `verify_answer` or another supported checking
role a slot with the chosen model and enable the corresponding engine feature.
This does not invent a universal confidence score or automatically call a
stronger model on every answer.

See the [configuration example](examples/model_slots.yaml) and
[ADR-082](adr/ADR-082-model-slots.md) (which supersedes the profile mechanism of
[ADR-075](adr/ADR-075-named-model-profiles-and-escalation.md)). The example uses
placeholder local endpoints. Validation is separate from actually loading its
checkpoints or invoking those endpoints.

## Development versus deployment acceptance

The tracker now distinguishes `DEPLOYMENT_CHECK` (actual target/controller and
operational acceptance), `NEEDS_INPUT` (host, quality-call budget or release
destination), and `NEEDS_TOOLCHAIN` (optional native compilation). These remain
pending until evidenced; none is a claim that ordinary local model routing must
wait for production infrastructure. No deployment or paid model calls are part
of this implementation.

## Verified implementation (pre-ADR-082 profile mechanism)

These results were recorded for the ADR-075 named-profile implementation and are
kept as historical evidence.

| Focused group | Result | Evidence |
|---|---|---|
| Configuration, references and example | 78 passed | [Log](provider_verification/model-profiles-config-focused.log) |
| GLiNER ownership, prepass, windows and assembly | 130 passed, 1 intentional skip | [Log](provider_verification/model-profiles-gliner-focused.log) |
| LLM profiles, escalation, native lifecycle, privacy and combinations | 148 passed | [Log](provider_verification/model-profiles-llm-focused.log) |

The documented example is included in both configuration and LLM groups, so
their totals contain one repeated test. The skipped case explicitly requires a
separate real-model inference smoke; no new model-quality claim is made. The
GLiNER group retains 16 existing LanceDB cleanup warnings. Ruff check/format pass
on 21 selected Python files, strict mypy on 15 source files, and generated
defaults/catalog/schema references match. These are focused correctness checks,
not a full-suite run or complete-Engine performance benchmark. No remote model
calls, weight downloads, commits, pushes or production rollout were performed.

See [the hashed final state](provider_verification/model-profiles-final-state.json)
and [the tracker](PROVIDER_TODO.md): PA-130–136 are complete against these local
gates. PA-119/121 subsequently passed the separate [grounded real-model ingestion gate](GLINER_INGESTION_ACCEPTANCE.md); its deterministic generative fixture does not establish LLM quality.
