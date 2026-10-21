# GLiNER2.5 structured extraction adapter

`memspine.derive.gliner_structured.GLiNER25StructuredExtractor` is an optional
adapter around the local `gliner2.AutoExtractor` API. It asks the configured
boundary model for typed entity spans, structured record fields, and typed
relation endpoint spans. It returns `RecordCandidate`, `RelationCandidate`,
and `TypedFieldCandidate` values with the SDK type labels, confidence scores,
exact offsets, and system supplied source ownership.

The adapter accepts one source turn per call. A caller builds `SourceContext`
from its already validated owner scope, `TurnRef`, sender ID, and exact text.
GLiNER output cannot supply or override tenant, owner, source ID/version,
sender, trust, status, or record identity. Such fields are rejected by the
schema and result mapper. Every returned span must be an in-range integer
character interval whose text exactly equals the corresponding source slice.
Relation endpoints must also match extracted mentions of the declared endpoint
types from that same source turn.

These values remain untrusted proposals. In particular, an entity mention is
not an asserted fact and a relation label is not evidence that the relation is
true. Candidates carry canonical `Evidence` and may be checked against
caller-authored claim text with `StructuredCandidates.ground_claim`, which uses
the existing `derive.ground.check_grounding` rules. Each candidate collection
retains the exact `SourceContext` used for extraction; grounding rejects a
context that differs by owner scope, source version, sender, or source text.
This prevents a reused source ID from substituting another tenant's text or
identity and keeps citation quotes pinned to the originally extracted bytes.
A supported check is still
only the canonical grounding input; the existing compiler, assessment, and
integration path must make the assertion and write it. This module has no
storage or Engine write method and does not create a second ingestion route.

```python
from memspine.derive.gliner_structured import (
    FieldSchema,
    GLiNER25StructuredExtractor,
    RecordSchema,
    RelationSchema,
    SourceContext,
    StructuredExtractionSchema,
)

schema = StructuredExtractionSchema(
    entity_types=("person", "location"),
    records=(RecordSchema(
        name="move_event",
        fields=(
            FieldSchema(name="subject", type_label="person", required=True),
            FieldSchema(name="destination", type_label="location", required=True),
        ),
    ),),
    relations=(RelationSchema(
        name="moved_to",
        head_type="person",
        tail_type="location",
        description="A person moved to a location",
    ),),
)
model = ...  # caller-owned GLiNER2 extractor, loaded under the caller's model policy
extractor = GLiNER25StructuredExtractor(model, schema)
candidates = extractor.extract(source_context)
```

The caller constructs and owns the model, including selection of a verified
local snapshot, network policy, sharing, cache identity, async execution, and
lifecycle. The adapter does not load models or enforce Hub/offline policy; those
remain outside this candidate-only boundary.

The focused tests validate the documented SDK payload shape using a fake
extractor and exercise malformed labels, ownership fields, source offsets,
confidence values, typed relation endpoints, and canonical grounding. A local
snapshot smoke check is separate evidence; neither test style establishes
held-out extraction quality.

On 2026-10-17, the adapter was also run once against the installed GLiNER2 SDK
and the cached `fastino/gliner2.5-base-v1` snapshot with Hub and Transformers
offline mode enabled. With `entity_types=("person", "location")`, the text
`Alice met Bob in London on Tuesday.` produced three source-aligned mention
candidates: `Alice` at `[0, 5)` (0.998), `Bob` at `[10, 13)` (0.995), and
`London` at `[17, 23)` (0.998). It produced no record or relation candidates.
This confirms local loading, SDK result mapping, and character offsets for this
single input; it does not evaluate extraction quality or structured relation
output. The SDK emitted a Transformers model-architecture compatibility warning
and a PyTorch TorchScript deprecation warning during loading.

On 2026-10-17, two offline Base inference calls exercised the
structured record and relation schema using the fixed invented text
`Mira works for Acme in Paris.` The first call produced SDK output but the
adapter rejected the record's additional outer `confidence` field. The second
call inspected the raw SDK payload and confirmed a nonempty `employment`
record, person/organization/location entities, and `works_for` relation. The
adapter now preserves record and relation candidate scores (while leaving both
unasserted), and a no-inference replay of the captured result mapped one record,
one relation, and three mentions; canonical grounding of the caller-authored
claim returned `supported` for the exact bound source. This validates a single
SDK output shape and adapter replay, not extraction accuracy or truth. The raw
results and exact runtime/schema/source scope are recorded in
[`gliner-structured-real.json`](provider_verification/gliner-structured-real.json)
and [`gliner-structured-real.log`](provider_verification/gliner-structured-real.log).

After that mapper fix, a third authorized offline inference call with the same
model, sentence, schema, and source completed live through the adapter. It
returned one record, one relation, and three typed mentions, preserved the
candidate scores, and kept record/relation `asserted=False`. Canonical grounding
of the caller-authored claim was `supported` against the exact pinned source.
This confirms the live SDK shape maps end-to-end for this one input; it does not
establish held-out quality, relation truth, or calibration.
