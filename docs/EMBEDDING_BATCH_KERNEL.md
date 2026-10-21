# Embedding batch kernel check

The recorded run benchmarks `fastembed==0.9.0` with `BAAI/bge-small-en-v1.5` on the CPU provider, using the local revision in the cache. It embeds fixed invented sets of 32 and 64 texts at batch sizes 1, 8, 32, and 64. Each case has two warmups and five timed trials; ONNX Runtime uses one thread and fastembed's multiprocessing path is disabled. The script sets no global environment itself: the invocation must set both `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`, and fastembed receives `local_files_only=True`.

The shared cache had all five required file blobs at the recorded sizes and its revision ref pointed to `aa8f8b060edb00e03bfdd08813a2949946c8ba55`, but that revision's snapshot directory was empty. A normal offline `TextEmbedding` load therefore failed with a cache-size/snapshot error. The benchmark stages temporary copies of the unique local blobs matching each required metadata size, hashes those staged files, and passes the temporary model directory as `specific_model_path`. It does not modify the shared cache or permit a download. Since this cache layout lacks a snapshot projection and the ONNX blob's name differs from the metadata blob ID, this evidence demonstrates operation from those cached files, not a clean standard-cache load. The evidence JSON records both identifiers and SHA-256 digests.

All cases returned one 384-dimensional vector per input, preserving row order. Across batch sizes, vectors were exactly equal to the batch-size-1 outputs in these trials (maximum absolute difference `0.0`), within the reported `rtol=1e-5`, `atol=1e-6` tolerance. In the recorded run, batch size 64 led on 32 texts at about 311 documents/second, while batch size 32 led on 64 texts at about 276 documents/second. This is a single Windows host, one cached model revision, one CPU/thread configuration, and a small synthetic text set; it does not establish a global optimum, production latency, or retrieval quality. It does not justify changing a provider default. The current fastembed adapter leaves the SDK batch size at its default, so no source/runtime setting was changed based on this result.

Reproduce with the same Python 3.13 environment and cache:

```powershell
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
& 'C:/Users/hembad/AppData/Local/Temp/memspine-ner-runtime-313/Scripts/python.exe' scripts/benchmark_embedding_batches.py `
  --json-out docs/provider_verification/embedding-batch-kernel.json `
  --log-out docs/provider_verification/embedding-batch-kernel.log
```

The machine-readable result and captured log are in [`provider_verification/embedding-batch-kernel.json`](provider_verification/embedding-batch-kernel.json) and [`provider_verification/embedding-batch-kernel.log`](provider_verification/embedding-batch-kernel.log). The benchmark script enforces at most two warmups, five trials, and a 180-second elapsed-time limit.
