# Performance alternatives

## JSON encoding and validation

The repository already uses `orjson` on the canonical memory-event fingerprint
path in `src/memspine/store/events.py`, envelope-extra decoding in
`src/memspine/store/envelope.py`, and selected API/store paths. We benchmarked
those formats alongside the remaining JSON round trips in provider change rows.
The reproducible harness is [`scripts/benchmark_json_paths.py`](../scripts/benchmark_json_paths.py);
machine-readable results and a readable run log are in
[`json-path-benchmark.json`](provider_verification/json-path-benchmark.json) and
[`json-path-benchmark.log`](provider_verification/json-path-benchmark.log).

The run used the Python 3.13 dependency-validation environment (CPython 3.13.5,
orjson 3.13.0, Pydantic 2.14.1 / pydantic-core 2.50.1, SQLAlchemy 2.1.5). It
benchmarked a 48-item `OperationRecord` checkpoint, a real `MemoryEvent` payload,
and a `MemoryRecord` converted through `to_envelope`. Each result is the median
of seven repeats of 1,500 calls after one warm-up call; these figures describe
only these inputs and this environment.

On the provider checkpoint, standard `json.dumps` followed by
`OperationRecord.model_validate_json` measured 12,285 operations/second.
`orjson.dumps` followed by the same JSON-mode validation measured 16,187
operations/second. A recursive compatibility guard around the orjson fast path
measured only 7,823 operations/second. That guard is necessary for arbitrary
checkpoint details: orjson maps NaN and infinities to `null`, rejects integers
above its 64-bit range and non-string dictionary keys, rejects lone-surrogate
text, and serializes datetimes that stdlib `json.dumps` rejects. Those differences
are directly recorded in the edge probes. The standard-library compact option
was not faster in this run (12,131 operations/second).

No provider source change was made. The unconditional orjson path changes values
or exception behavior; the compatibility guard erases the measured speedup. The
attempt to replace JSON-mode validation with `model_validate` also fails on the
actual DB-shaped mapping because strict provider models receive enum strings
and ISO timestamp strings. Pydantic recommends `model_validate_json()` in
general, while noting validator-specific exceptions; the provider path already
uses JSON-mode validation. See [Pydantic performance guidance](https://pydantic.dev/docs/validation/latest/concepts/performance/).

Canonical event bytes are especially sensitive because `canonical_payload`
feeds the persisted content fingerprint. For this event sample, orjson's sorted
UTF-8 bytes matched stdlib JSON only when configured with `ensure_ascii=False`,
compact separators, and identical non-finite policy. Stdlib's default ASCII
escaping produced different bytes and hashes for non-ASCII text. The edge probes
also show non-finite and large-integer differences, so this path stays on its
existing encoder. The repository's current canonical path is already the measured
fast choice for its exact input: about 4.18 million calls/second versus 336,413
for the comparable stdlib encoding on this small payload. This is a local
measurement, not a universal library ratio.

For the envelope sample, `AssertionEnvelope.model_dump_json()` measured 130,630
calls/second; wrapping `model_dump(mode="json")` in orjson measured 127,312.
Stdlib encoding was slower at 67,544 calls/second. The existing Pydantic JSON
serializer remains unchanged.

The upstream orjson documentation describes its UTF-8 output, 64-bit integer
range, unsupported non-string key behavior, and conversion of non-finite floats
to `null` ([orjson README](https://github.com/ijl/orjson)). Those are reasons to
test bytes and values at each call site, not to select one encoder globally.
