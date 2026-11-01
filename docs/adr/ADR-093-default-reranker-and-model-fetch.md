# ADR-093: MiniLM cross-encoder as the default reranker, with an explicit model fetch

**Status:** Accepted
**Date:** 2026-11-01
**Decider:** Repository owner (decision rounds 1-8, 2026-11-01)
**Register:** D-117

## Context

The base and simple templates ship no reranker (`model.rerank.chain: []`). RS-09 asked
for a default. RM-44 found no documented weight download for CI or users.

## Decision

- The base and simple templates default to the onnx MiniLM-L6 cross-encoder from the
  pinned certified catalogue (ADR-084). `core` stays bare.
- `memspine models fetch [--template NAME]` downloads the pinned catalogue entries for a
  template, with SHA-256 verification. CI caches the result.
- Missing weights at start-up raise `MissingServiceError` naming the fetch command (D-10).
  With `strict_services: false`, reranking is skipped with a warning. There is no automatic
  download during a request.
- RM-25/RM-26/RS-06 measure the CPU envelope on the reference host (ADR-096): threads,
  batch sizes and int8 vs fp32 parity (rank agreement, cosine delta). Defaults are then set
  from that data.

## Consequences

Default reads gain about 20 to 60 ms of CPU cost per read. The latency budgets are
re-measured with the reranker on.
