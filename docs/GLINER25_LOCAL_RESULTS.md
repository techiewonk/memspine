# Local GLiNER2.5 runtime and bounded comparison

Observed on Windows x64, CPython 3.13.5; `gliner2` 2.1.0, PyTorch 2.14.1,
Transformers 5.20.1. The complete `[ner]` wheel extra installed into a fresh
107-distribution environment and `uv pip check` passed. Inference ran offline
on CPU with two Torch threads. No paid API or GPU inference was used.

A separate four-call Base smoke returned nonempty exact source spans. The
comparison used five fixed invented English texts, three entity labels and SDK
default thresholds, authored before either comparison run. Each model made ten
calls (two per case), below an eleven-call cap. No thresholds or prompts were
tuned after observing the results. A token budget is not relevant to this
encoder test; model calls and input texts are fixed in the script.

| Model | Typed-span precision | Recall | F1 | Warm median ms | Warm sample p95 ms | Process peak working set MiB |
|---|---:|---:|---:|---:|---:|---:|
| Base | 1.000 | 1.000 | 1.000 | 163.7 | 165.2 | 2021 |
| Small | 0.846 | 1.000 | 0.917 | 42.8 | 54.5 | 1125 |

These are acceptance-fixture results, **not a reliable production model ranking**.
Five cases are too few for a quality decision; the process ran on a shared host
and host CPU differed (11.1% before Base cases, 17.9% before Small cases).
Peak working set includes Python, Torch, model loading and inference. Warm
samples include only the second call for each of five varied inputs; the first
model inference is reported separately. Five warm samples are not a steady-state
throughput or tail-latency claim. No confidence interval or statistical significance is claimed.

Small produced two extra typed spans. In the negative case it labeled `meeting`
as an organization even though the span was an exact source substring. This
shows why span grounding alone does not establish semantic correctness or an
asserted fact. The default remains Base; Small stays explicitly selectable and
requires a representative task-quality gate before adoption. Nothing was
automatically enabled in the Engine.

## Artifact identity and replay

Base used the already cached complete snapshot
`ca906247640776a07753514055be9726f9080ead`; it differs from the earlier metadata
survey revision and must not be described as that earlier pin. Small was
explicitly downloaded at `df5910e44bc4ffdb0d95399a83b0ca4516349aa5` (about
304 MB of model/tokenizer assets). Reports record exact revisions and the
smoke records Base config/weight hashes. The SDK's lost Hub-kwargs risk still
exists for repository-ID loading; local-directory loading plus offline mode was
used here. This is not proof that every tokenizer/Hub argument propagates
through the normal Engine configuration.

```powershell
$py = 'C:/Users/hembad/AppData/Local/Temp/memspine-ner-runtime-313/Scripts/python.exe'
& $py scripts/compare_gliner_local.py `
  --model-directory '<existing-complete-local-snapshot>' `
  --report 'docs/provider_verification/ner-custom-comparison.json'
```

See [Base measurements](provider_verification/ner-base-comparison.json),
[Small measurements](provider_verification/ner-small-comparison.json),
[Base smoke](provider_verification/ner-local-runtime.json), and the
[bounded runner](../scripts/compare_gliner_local.py). Model output is never
supplied with these expected span labels; scoring occurs after inference.

PA-108 establishes real optional-runtime feasibility. PA-122 provides a bounded
Base/Small comparison with the limits above. PA-118/119/121/123 shared lifecycle,
Engine ingestion grounding and loader-policy integration remain separate gates;
this SDK benchmark does not close them.

Independent review corrected two measurement-tool defects before final reporting: warm samples now exclude each input’s first call, and span validation rejects negative/out-of-bounds, reversed, empty and boolean offsets. The models were rerun with the corrected code; focused regression tests cover invalid span acceptance.
