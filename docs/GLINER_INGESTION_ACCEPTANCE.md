# PA-121: real local GLiNER ingestion acceptance

This focused gate runs the canonical `Engine.ingest_messages` and `Engine.flush_windows`
paths with the cached GLiNER2.5 Base snapshot at revision
`ca906247640776a07753514055be9726f9080ead`. It verifies source-message identity and
timestamps, exact source-bound mention spans, typed confidence, the `asserted: false`
mention/fact distinction, grounded window evidence, namespace isolation, and retrieval.
Because canonical message intake stores source turns as episodic records, the gate also
performs and labels one separate `Engine.write(memory_type="semantic")` on the first
ingested message's exact content to exercise persisted semantic mention annotations. That
write points back to the raw turn as its parent; it does not replace or bypass ingestion.

GLiNER is real local SDK inference on CPU, loaded from the complete local snapshot with
`local_files_only: true`. The window fact compiler uses a deterministic, source-quoted
PA-121 LiteLLM fixture with a synthetic `Mira works for Acme in Utrecht.` assertion. This
avoids a generative endpoint and is not evidence about LLM quality. The run is capped at
12 GLiNER calls, 12 fixture calls, and 180 seconds total including the SQLite reopen.
Storage and provider projection files use a private `TemporaryDirectory` and are removed
after the report is written. The engine and pool are stopped through normal lifecycle cleanup.

From the repository root in PowerShell:

```powershell
$env:HF_HUB_OFFLINE = "1"
$env:TRANSFORMERS_OFFLINE = "1"
$env:PYTHONPATH = "src"
$py = "C:/Users/hembad/AppData/Local/Temp/memspine-ner-runtime-313/Scripts/python.exe"
& $py scripts/check_gliner_ingestion.py `
  --model-directory "C:/Users/hembad/.cache/huggingface/hub/models--fastino--gliner2.5-base-v1/snapshots/ca906247640776a07753514055be9726f9080ead" `
  --report docs/provider_verification/gliner-ingestion-real.json `
  --timeout-s 180 2>&1 | Tee-Object docs/provider_verification/gliner-ingestion-real.log
```

The acceptance JSON includes pass/fail statuses and bounded, content-free tap diagnostics. It contains only
the synthetic conversation, source-bound spans, local package versions, and selected
provider identities. `status: passed` requires at least one exact GLiNER mention, one
grounded and retrievable window fact, matching sender/time/source IDs, valid namespace
separation, and no semantic fact with `attribute: mention`.

The final local run passed in 17.0 seconds with Python 3.13.5, gliner2 2.1.0, torch
2.14.1, and transformers 5.20.1. It made six real GLiNER inference calls and one window
fixture call, deposited one supported window fact, validated three mentions on the
window envelope and three on the separate semantic probe, and passed namespace isolation,
retrieval, and SQLite reopen checks. The raw message timestamps, sender IDs/roles, reply
link, source text hashes, and mention source-binding hashes are included in the report.

The gate caught a real coordinate bug during development: the window prompt included a
presentation-only speaker prefix, and compiled evidence offsets were therefore measured
against that rendered line instead of the raw source message. The window compiler now
uses original record content for source spans while keeping the prefix only in the prompt;
the focused wiring regression and this gate verify exact quote offsets against the raw
source. Historical `gliner-ingestion-attempt*.json/.log` files preserve the failed
iterations; `gliner-ingestion-real.json/.log` is the final acceptance result.

This is a one-snapshot behavioral gate, not a held-out quality or CPU-performance
benchmark. It does not establish that all nine memory types or all provider combinations
are covered. The window LLM fixture must be reported as synthetic; it is not a real model
call.
