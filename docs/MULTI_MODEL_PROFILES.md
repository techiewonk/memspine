# Multiple models and explicit reasoning levels

Named profiles separate model selection from engine tasks. A profile owns
`kind`, `provider` and native `settings`; a task binding names that profile.
Profiles and bindings default to empty mappings. Declaring a profile does not
load weights or enable a memory feature.

The example's `runtime.deployment_id` is a stable application ownership label
required by the shared-service provider contract, including localhost endpoints.
It does not require production hosting or a Temporal cluster. Endpoint models,
authentication and cached checkpoint availability still need to match the local
installation before invoking them.

| Purpose | Binding name | Example profile |
|---|---|---|
| Fast window extraction | `window_extract` | `fast` |
| Conflict verification | `judge` | `reasoning` |
| Answer verification | `verify_answer` | `reasoning` |
| User-facing generation | `chat` | Independently selected profile |
| Decision classification | `gliner_decision` | Dedicated classification checkpoint |
| Semantic entity mentions | `gliner_semantic` | Entity extraction checkpoint |
| Window entity prepass | `gliner_prepass` | Same entity profile, or a different one |

“System 1” and “System 2” describe intended roles, not guaranteed quality or
latency. A small generative model can serve routine work, a specialized GLiNER
classifier can serve classification, and a stronger generative model can serve
verification. Their APIs are different: a GLiNER profile cannot be bound to a
chat role or inserted into a generative escalation chain.

## Settings, ownership and validation

LLM profiles use the selected provider's settings contract, including its model,
endpoint, temperature, timeout and generation defaults. GLiNER profiles use
checkpoint, revision, CPU/CUDA device, offline policy, allowed tasks and inference
concurrency. GLiNER `settings: {}` receives the default Base checkpoint, CPU,
local files only, entities/classification and concurrency one. Declare a pinned
revision when reproducible artifacts are required.

Different profile names with identical GLiNER effective settings share one model
pool entry. Different configurations can load separate models. Set
`models.gliner_max_models` explicitly for the desired resident model bound
(default one); a full busy pool fails rather than exceeding that bound. LLM
profile providers are owned by the Engine lifecycle. Sharing a resource never
authorizes sharing namespace-specific extraction results.

Provider factories receive the logical task role and an optional stable
`LLMBuildContext.resource_key`. Named profiles use `llm.profile:<name>` so two
native models serving the same role register distinct owned resources. A shared
profile still constructs and closes its provider once. Inline bindings retain
their role-based ownership key.

Named LLM cache identities include normalized native provider settings; an
escalation identity also includes its ordered candidates and effective policy.
Changing an endpoint, generation setting, target or policy invalidates the
producer identity. Secret values are hashed, not exposed in identities. Metrics
retain the actual attempt's adapter identity and count each call under its role;
sharing a provider does not serialize calls merely for usage accounting.

Unknown role/profile names, incompatible profile kinds, unsupported task
capabilities and invalid native settings fail before model execution. Unused
profiles do not justify loading optional SDKs or weights. The old single
`models.gliner` field and `decision.model` are not accepted aliases.

Existing inline `llm.roles` is still an intentional way to configure a role
directly. It cannot configure the same role as `models.bindings`. Explicit
profile/inline bindings take precedence over automatic `models.tiers_v2`
selection. The tier catalogue remains the optional placement planner; declaring
profiles alone does not turn tier planning on.

## Escalation

`models.escalation.<role>` explicitly names ordered target generative profiles.
The primary comes from the role's named profile binding. Policies are absent
by default and may trigger only on selected failure conditions:

| Trigger | Meaning | Does not establish |
|---|---|---|
| `error` | Supported runtime/provider failure | That every programming/configuration error is retryable |
| `empty` | Empty/whitespace output | Semantic answer completeness |
| `invalid_json` | Strict JSON parse failure | Schema validity, factual correctness or calibrated confidence |

Select `invalid_json` only for roles whose configured prompts require JSON;
YAML or free-text outputs are not JSON failures. The example's timeouts and
output limits illustrate configuration and are not quality-tested task budgets.

Each policy requires a total deadline and per-attempt output setting, and limits
the number of actual calls within one routed `chat` invocation. Existing outer
structured retries or agent loops are separate invocations with their own
budgets; this policy is not a whole-Engine-operation call limit. The `triggers`
field selects the failure conditions in the table above.
Caller cancellation propagates. Each candidate keeps
its own remote-data consent gate, including a remote fallback behind a local
primary. Failed/invalid outputs must not be silently returned on exhaustion.
Generation limits depend on provider enforcement; they are not proof of remote
billing limits or a bound on hidden reasoning tokens. Input context also consumes
tokens; no global monetary budget is implied.

For semantic review, bind `judge`, `verify_answer` or another supported checking
role to the chosen model and enable the corresponding engine feature. This
change does not invent a universal confidence score or automatically call a
stronger model on every answer.

See the [configuration example](examples/model_profiles.yaml) and
[ADR-075](adr/ADR-075-named-model-profiles-and-escalation.md). The example uses
placeholder local endpoints. Validation is separate from actually loading its
checkpoints or invoking those endpoints.

## Development versus deployment acceptance

The tracker now distinguishes `DEPLOYMENT_CHECK` (actual target/controller and
operational acceptance), `NEEDS_INPUT` (host, quality-call budget or release
destination), and `NEEDS_TOOLCHAIN` (optional native compilation). These remain
pending until evidenced; none is a claim that ordinary local model routing must
wait for production infrastructure. No deployment or paid model calls are part
of this implementation.

## Verified implementation

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
