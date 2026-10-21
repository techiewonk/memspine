# Shared GLiNER runtime ownership

`GLiNERModelPool` is an explicitly constructed, engine-owned optional resource
in `memspine.models.runtime.gliner_runtime`. It does not introduce an ingress
path or make GLiNER a core dependency. Semantic NER, decision classification and
prepass consumers resolve their own named model profile and receive a lease
from the same Engine pool. No module-global model cache is created.

The pool accepts the canonical `GlinerSettings` structurally: `model`,
`revision`, `device`, `local_files_only`, `tasks` and `inference_concurrency`.
Its key contains the selected checkpoint, exact revision, device, local-only
policy, normalized task set and inference concurrency. Capacity is finite (one
model by default), configured by `models.gliner_max_models`. When every slot is
leased or running, a request for a different key raises `GLiNERPoolFullError`
rather than loading beyond the configured resource bound. An idle model may be
evicted before admitting a different key.

Profiles and role bindings are explicit. Each active GLiNER consumer must bind
to a profile with `kind: gliner`, `provider: gliner2`, and the task it needs.
The profile settings are validated as `GlinerSettings` without importing the
SDK or loading weights. The default model and task values apply inside an
explicit profile with empty settings:

```yaml
models:
  gliner_max_models: 2
  profiles:
    shared-entities:
      kind: gliner
      provider: gliner2
      settings:
        model: fastino/gliner2.5-base-v1
        tasks: [entities]
    decisions:
      kind: gliner
      provider: gliner2
      settings:
        model: fastino/gliner2.5-base-v1
        tasks: [classification]
  bindings:
    gliner_semantic: shared-entities
    gliner_prepass: shared-entities
    gliner_decision: decisions
```

Different profile names with identical effective settings share one model
because the pool keys on resolved settings. Different checkpoints or inference
policies use separate entries, subject to the configured capacity. The model
weights are shared; extraction results and user data remain owned by the
consumer and its namespace-scoped caches.

Callers acquire and inject leases explicitly:

```python
from memspine.models.profiles import resolve_gliner_settings

pool = GLiNERModelPool()
settings = resolve_gliner_settings(config, "gliner_semantic")
lease = await pool.acquire(settings)
try:
    result = await lease.call(lambda model: model.extract_entities(text, labels))
finally:
    await lease.aclose()
await pool.aclose()
```

The callback must be synchronous and should contain the complete SDK operation
(for example schema construction plus extraction). The runtime runs it in a
worker thread behind the configured asynchronous semaphore. Cancellation of a
waiting coroutine does not stop the underlying thread; the pool retains the
active-call count until that thread exits. Pool close rejects new work, drains
in-flight loads and calls, then calls a model `close()` or `dispose()` hook if
the SDK model provides one.
The entry's model reference is cleared after disposal, even when callers retain
old lease objects, so a closed engine does not retain checkpoint memory through
stale handles.

The default loader lazily imports the optional GLiNER and Hugging Face packages.
It resolves a Hub snapshot using both `revision` and `local_files_only`, or accepts
an existing local snapshot directory (an explicit revision must match its
directory name). Before model construction it verifies the root and encoder
configs, supported `span` or `boundary` architecture, at least one supported
weight filename and tokenizer vocabulary files. It then calls
`AutoExtractor.from_pretrained` on the resolved local directory.
This matters because the inspected `AutoExtractor` dispatch path does not pass
Hub kwargs through to selected model weight loading. No global Hub/offline
environment variables are mutated. A pinned revision is resolved exactly; a
missing local revision with `local_files_only=True` fails instead of silently
falling back to a different revision. A checkpoint's architecture remains
authoritative; unsupported architectures fail rather than falling back between
span and boundary.

The focused pool and adapter tests use synchronized fake loaders/models and
prove profile resolution, identical-settings sharing across named profiles,
distinct-profile checkpoint selection, bounded capacity/calls, failed-load
retry behavior, off-loop
callbacks, close/drain and cancellation handling. The kernel benchmark compares
load counts for three independent clients and a shared fake pool; it does not
measure GLiNER inference speed, quality, memory footprint, or the production
checkpoint loader. Evidence is recorded in
`provider_verification/gliner-ownership-kernel.json` and `.log`. A separate
offline smoke loaded and closed the already-cached Base snapshot once, with no
inference; its warnings and scope are recorded in
`provider_verification/gliner-ownership-local-load.json` and `.log`.

Runtime wiring and model settings live in the caller/config layer. An active
consumer needs an explicit profile binding; settings alone do not enable a
feature. The pool does not validate the semantic meaning or provenance of
extracted candidates.
