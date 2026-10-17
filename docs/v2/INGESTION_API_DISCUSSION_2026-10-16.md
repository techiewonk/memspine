# Ingestion API and sender schema: discussion notes

Date: 2026-10-16.

Status: decisions implemented after the owner's subsequent instruction to proceed and complete the consolidation. These chronological notes preserve the discussion; the current implementation map and verification are in INGESTION_SINGLE_PATH_PLAN_2026-10-16.md.

## Current decision snapshot

This snapshot is authoritative over superseded proposals later in these chronological notes.

| Item | Owner decision |
|---|---|
| Implementation | Owner authorised implementation with separate agents, then authorised completion of the single-path consolidation |
| API names | write_messages -> ingest_messages; ingest -> ingest_data |
| Compatibility | Direct breaking rename; no aliases or backward compatibility |
| Ingestion path | One canonical flow; no write.intake_v2 switch or duplicate intake implementation |
| Retry | Identical supplied message ID/data returns stored result; changed data rejected pending explicit revision API |
| Message ID | Use supplied valid message_id; otherwise generate and return it; unique within namespace |
| Conversation ID | Belongs to one session; use supplied valid conversation_id; otherwise inherit reply parent's conversation; each independent root gets a new conversation |
| Session ID | Parent container of conversations; use supplied valid session_id; if missing, create a new session. Do not infer it from replies/conversation IDs |
| Reply | Optional reply_to names a message ID; parent must be stored or earlier in the batch, in the same namespace and conversation; reject unresolved or conflicting references |
| Source time | Keep created_at; remove sent_at; preserve supplied created_at, otherwise generate from engine clock |
| Edit time | Keep optional updated_at for source edit time; edit/revision semantics pending |
| Acceptance time | ingested_at is proposed engine-generated acceptance time, distinct from historical created_at |
| Sender | Need sender identity/name and category for user/assistant/agent separation; exact required fields and enums pending |

Remaining questions include session generation boundary, sender ID requirement, timestamp provenance, retries/revisions, batch error handling, call-level defaults, and receipt schema.

## Owner decisions

| Item | Decision |
|---|---|
| Conversation entry point | Rename `Engine.ingest_messages(...)` to `Engine.ingest_messages(...)` |
| Document/data entry point | Rename existing `Engine.ingest(...)` to `Engine.ingest_data(...)` |
| Compatibility | Direct breaking rename. No backward-compatible aliases, forwarding wrappers or deprecation transition are wanted |
| Implementation timing | Do not implement yet; keep taking notes and discuss the schema first |

The earlier assistant recommendation to retain aliases is superseded by the owner's explicit decision. At implementation time, update repository callers, examples, tests, API documentation and debugging instructions together. External callers will need to adopt the new names. A rename does not by itself expand document ingestion to every possible data format.

## Intended call shape

```python
# Proposed, not implemented.
records = await eng.ingest_messages(
    messages=msgs,
    namespace=ns,
    session_id="S2",
)

# Existing document-ingestion parameters to be reviewed separately.
# eng.ingest_data(...) becomes eng.ingest_data(...).
```

## Sender requirement recorded from the discussion

Each message needs an identifiable sender: a user, assistant or agent, with a name and/or stable ID usable to distinguish and index senders. Test these cases one by one. Do not conflate the sender with the subject mentioned in the message, the session ID, or the authenticated owner of the memory.

## Proposed schema — not yet finalised

| Field | Proposed meaning | Decision still needed |
|---|---|---|
| `id` | Stable source-message ID for retries and references | Required vs generated; uniqueness scope |
| `role` | Conversational role such as user or assistant | Whether agent is a role or represented only by sender kind |
| `sender.id` | Stable sender identifier | Required vs optional; validation and identity scope |
| `sender.kind` | Human, agent or service | Exact enum; owner used user/assistant/agent terminology |
| `sender.name` | Display name | Optionality and handling renamed senders |
| `content` | Original message content | Text-only initial contract vs content parts |
| `timestamp` | Source message time | Missing-time policy and precision |
| `reply_to` | Message being answered | Source-message ID vs internal record ID; cross-session references |

The nested sender schema is an assistant proposal, not an approved implementation contract. Current `speaker`/`name` handling must be mapped deliberately when the new schema is agreed; do not claim the proposed fields already work.

## Open API questions

- Keep the existing return values for the rename, or introduce an ingestion receipt later?
- Keep `ingest_asset()` separate? The assistant recommended this; the owner has not yet explicitly decided.
- What formats should `ingest_data()` initially accept beyond the current document contract, if any?
- Which sender fields are persisted, searchable as exact metadata, or included in embedding text?
- How should same-message-ID retries, edits and conflicting payloads behave?
- How should sender filters interact with authenticated scope and permissions? Claimed sender identity must not confer access.

## Later implementation and verification order

1. Finalise API names and message/sender schema in discussion.
2. Apply direct renames and update all in-repository callers; provide no old-name aliases.
3. Verify one human message, one assistant message and a second distinct agent.
4. Check stored role, stable identity, display name, scope, source time and provenance.
5. Check exact sender filtering and index behaviour independently from extraction.
6. Check retries, edits, reply references, replay and derived evidence attribution.

No engine code changed as part of recording these notes.

## Conversation and reply identity discussion

Owner clarification: use a supplied conversation ID; otherwise support automatic generation. `reply_to` is optional and should help identify relevant reply relationships and separate conversational branches. The owner supplied the three-message example below for finalisation; exact validation policies remain under discussion.

Recommended distinction: a conversation groups messages; a reply edge identifies a parent message. A reply does not on its own establish the conversation's identity. Messages without `reply_to` can still belong to the same conversation. Different branches can exist inside one conversation.

```python
msgs = [
    {
        "id": "msg-001",
        "role": "user",
        "sender": {"id": "user-123", "kind": "human", "name": "Hembad"},
        "content": "I moved from Porto to Lisbon last month.",
        "timestamp": "2026-10-01T10:00:00+05:30",
    },
    {
        "id": "msg-002",
        "role": "assistant",
        "sender": {"id": "assistant-main", "kind": "agent", "name": "Personal Assistant"},
        "content": "What do you like about Lisbon?",
        "timestamp": "2026-10-01T10:00:05+05:30",
        "reply_to": "msg-001",
    },
    {
        "id": "msg-003",
        "role": "assistant",
        "sender": {"id": "agent-travel", "kind": "agent", "name": "Travel Agent"},
        "content": "I can help you explore nearby walking routes.",
        "timestamp": "2026-10-01T10:00:10+05:30",
        "reply_to": "msg-001",
    },
]
```

### Questions submitted to the owner — pending

| Decision | Recommended option | Alternative |
|---|---|---|
| Conversation vs session | **Owner selected: keep conversation_id for the ongoing conversation and session_id for separate visits** | Replacement rejected |
| Auto-generation boundary | **Owner selected: infer missing conversation_id from reply_to**; no-reply batch grouping remains pending | Automatic new conversation per call is not the general rule |
| Reply resolution | **Owner selected: same conversation; already stored parent or earlier item in batch; reject unresolved references** | Pending links rejected |

Additional recommendations, not yet approved: scope message uniqueness and reply lookup by namespace plus conversation ID; preserve caller IDs; reject self-replies; never cross namespace boundaries to resolve a reply; expose generated IDs in the result. If message IDs can be omitted, return generated IDs, and require explicit IDs for targets referenced within the batch. Treat retry identity separately from conversation auto-generation so repeating an unkeyed request does not silently duplicate a conversation.

### Confirmed identity hierarchy

`namespace -> conversation_id -> session_id -> messages`.

The owner selected separate conversation and session IDs. Each message belongs to one session of one conversation. Reply lookup is proposed at conversation scope, permitting a parent from a previous session of that same conversation. A reply does not move the new message into the parent's session. The current repository's use of session IDs in provenance and grouping will require an explicit mapping change; this is not merely a parameter rename.

```python
# Proposed call after the schema discussion is complete.
result = await eng.ingest_messages(
    messages=msgs,
    namespace=ns,
    conversation_id="conv-123",
    session_id="visit-002",
)
```

Still pending: exact automatic conversation generation boundary, unresolved-reply handling, and session lifecycle/default generation. Do not generate a new session for every message call by accident: a visit normally spans multiple calls.

### Subsequent confirmed decisions (supersede pending wording above)

1. Message IDs are unique **within the namespace**, not merely within a conversation. The earlier recommendation for conversation-scoped uniqueness is superseded.
2. If conversation_id is omitted and reply_to exists, resolve the parent within the namespace and inherit its conversation_id.
3. The parent must already be stored or occur earlier in the same batch. Reject unresolved references; do not silently start a new conversation or queue a pending link.
4. A reply parent must belong to the message's conversation. Consequently, when conversation_id is explicit, reject a conflicting parent conversation rather than overriding either identity.
5. A reply may cross session/visit boundaries within the same conversation. It does not by itself determine the new visit's session_id.

Owner selected: when conversation_id is absent, each root message without reply_to starts a separate generated conversation. Its descendants inherit that conversation through reply_to. Multiple roots must not silently be grouped just because they share an ingest call.

Other pending design details: session lifecycle/default generation; whether message IDs may be generated if absent; explicit retry/edit semantics; handling a batch whose replies resolve to different conversations. Validate these relationships before writing to avoid partially accepting an invalid batch.

### Current conversation-resolution rules

| Input | Resolution |
|---|---|
| Explicit conversation_id; no reply_to | Use the supplied conversation |
| Explicit conversation_id; valid reply_to | Require parent conversation to match |
| No conversation_id; valid reply_to | Inherit parent's conversation |
| No conversation_id; no reply_to | Generate a new conversation for this root |
| Unknown, later-in-batch or cross-namespace parent | Reject; no pending link and no fallback conversation |
| Several independent roots in an implicit-conversation batch | Separate generated conversations, one per root and its descendants |

Proposed result implication: return per-message resolved IDs, because an implicit-conversation batch can legitimately contain multiple conversations. Example: m1 root -> C1; m2 replies to m1 -> C1; m3 root -> C2; m4 replies to m3 -> C2. The user's three-message example has a single root msg-001 and therefore one conversation; msg-002 and msg-003 are sibling replies, not separate conversations.

Questions now submitted:
- Missing session_id: recommend inheriting a reply parent's session, generating a session for a root, and requiring an explicit new session_id to start a separate visit; alternative is a new session per conversation per call.
- Message id: recommend requiring caller-provided IDs for reliable retries/references; alternative is generating missing IDs, with explicit IDs required for same-batch reply targets.

These remain discussion notes; no API implementation change has been made.

## Caller-supplied identity and historical timestamps

Owner requirement: use supplied message/session/conversation identifiers and supplied historical timestamps, so old conversations can be imported. The repeated session-ID in the request is interpreted as conversation_id plus session_id, pending correction if intended otherwise. Using supplied values remains subject to validation, namespace uniqueness and reply consistency; never silently overwrite a conflict.

Recommended names (proposal, not yet final):

| Name | Meaning | Origin / missing-value policy |
|---|---|---|
| message_id | Stable source-message identifier; prefer this explicit name over generic id | Caller value preserved. Required vs generated remains pending |
| conversation_id | Ongoing conversation/thread | Caller value preserved; otherwise inherit reply parent's conversation or generate per independent root, as already decided |
| session_id | Visit within a conversation | Caller value preserved; default generation/inheritance remains pending |
| reply_to | Parent source-message ID in the same namespace/conversation | Optional; parent must exist or appear earlier in batch |
| sent_at | When the message was sent in its source conversation | Caller-supplied historical time. Missing time should remain unknown, not silently become ingestion time |
| created_at | Optional original-system record creation time, if different from sent_at | Caller-supplied; omit when unavailable. Not necessary for ordinary chat |
| updated_at | Optional original-system record edit time | Caller-supplied; does not itself authorise an edit or overwrite |
| ingested_at | When this engine accepted the message | Engine-generated; not writable by the source payload |
| role | Conversation role | Caller-supplied, validated against role policy |
| sender.id / sender.kind / sender.name | Stable sender identity / human-agent-service classification / display name | Caller-supplied; exact validation and required fields pending |
| content | Original message body | Text-first proposal; multimodal content-part shape pending |
| metadata | Optional source-specific extension attributes | Proposed bounded, JSON-compatible attributes; not permission or identity overrides |

Owner decision: use created_at and updated_at in the public message payload for original/source-system creation and modification times. Keep ingested_at distinct for engine acceptance. These source timestamps are optional and caller-supplied; they are not ingestion time and do not themselves authorise an overwrite. These are public schema naming proposals, not renames of existing internal recorded_at/valid_from fields. Source message time is also distinct from event time described by a statement, e.g. a message sent today describing last year's move.

Historical import proposals: preserve original timezone offset and precision; accept ISO-8601 source timestamps; retain unknown timezone/date-only precision explicitly rather than inventing UTC/midnight; topologically order replies before ingestion; do not reorder solely by timestamps; preserve identity when replaying imports. Exact timestamp representation and missing-time rules still need approval.

Open batch-shape decision: call-level conversation/session IDs can be defaults, but mixed historical imports need per-message IDs or explicit grouping. Define conflict precedence before implementation; recommend rejecting conflicting call-level and message-level IDs.

Additional questions already submitted and still pending: identical retries as no-op vs rejection; changed same-ID payload via explicit revision vs automatic revision; invalid-batch rejection vs per-message partial acceptance.


### Confirmed timestamp naming

The owner selected `created_at` and `updated_at` instead of the previously proposed source-prefixed names. `created_at` means creation in the originating system; `updated_at` means its last edit there. `sent_at` remains the proposed conversation send time, and `ingested_at` remains engine-generated acceptance time. The exact defaults and whether both sent_at and created_at are needed are still open. No implementation change.


## Latest owner decision: one source message time

This decision supersedes the earlier proposal to keep sent_at separate from created_at.

- Remove `sent_at` from the proposed public message schema.
- Keep `created_at` as the message's original send/creation time, including historical caller-supplied values.
- Keep optional `updated_at` as the original/source-system edit time.
- Keep engine-generated `ingested_at` as acceptance time; it is separate from historical message time.
- Missing-time handling and timestamp precision remain pending. Do not infer ingestion time as original creation time without an explicit policy.
- No compatibility alias for sent_at is proposed; this schema is not implemented yet.

Current timestamp fields:

| Field | Meaning | Set by |
|---|---|---|
| created_at | Original message send/creation time | Caller |
| updated_at | Original message edit time | Caller |
| ingested_at | Engine acceptance time | Engine |


## Owner decisions: missing timestamps and sessions

- If `created_at` is missing, generate it. Proposed implementation meaning: engine acceptance time, with generated/source provenance recorded so imported source time is distinguishable from a fallback. The exact provenance field is not finalised.
- If `session_id` is missing, generate it. This supersedes the earlier recommendation to inherit a reply parent's session by default.
- The user repeated the session-ID decision; do not interpret that repetition as approval to generate message IDs. Missing `message_id` policy remains pending.
- Supplied valid `created_at` and `session_id` values remain preserved.
- Pending generation boundary: recommend one new session per resolved conversation per ingest call, shared by that conversation's messages in the call. Alternative: one session per message. No silent inference of owner approval for either boundary.


## Owner decision: generate missing message IDs

If `message_id` is omitted, generate a namespace-unique message ID and return it in the per-message ingestion result. Preserve supplied valid IDs. This resolves the earlier required-versus-generated question in favour of generation.

A same-batch `reply_to` still needs an explicitly identifiable earlier parent; with the current ID-only reference design, callers must supply the parent's message_id when referencing it in that batch. Cross-call replies can use the generated ID returned by the prior call.

Still pending: generated-session grouping and whether generated created_at values carry explicit supplied/generated provenance. Unkeyed retries also need a separate idempotency policy; generating a new message ID cannot itself recognise a retry.


## Latest owner correction: conversations belong to a session

Authoritative hierarchy: `namespace -> session_id -> conversation_id -> message_id`.

A session contains one or more conversations; each conversation belongs to exactly one session. A conversation contains its root and reply descendants. This supersedes earlier notes describing sessions as visits within a conversation, including the previously allowed cross-session reply interpretation. Do not implement that superseded hierarchy.

Example:

| Session | Conversation | Messages |
|---|---|---|
| S1 | C1 | m1 root; m2 replies to m1 |
| S1 | C2 | m3 root; m4 replies to m3 |
| S2 | C3 | m5 root; m6 replies to m5 |

Already confirmed root grouping and namespace-unique message IDs remain unchanged. A reply inherits its parent's conversation and therefore identifies the session that contains it.

Proposed resolution requiring owner choice: when session_id is omitted and a reply or existing conversation identifies an existing session, reuse that session rather than generate a conflicting one. Generate a new session only when neither source identifies one; group otherwise unassigned roots into one generated session for the call. If an explicit session conflicts with the conversation/parent session, recommend rejecting the reference. The prior pending generated-session grouping options are superseded by this corrected hierarchy.

No code changes; discussion only.


## Latest owner decision: missing session creates a new session

If session_id is supplied, use it after validation. If session_id is missing, create a new session. This supersedes the recommendation to reuse/infer an existing session from reply_to or conversation_id. The session is the parent container for conversations.

Proposed call-level grouping: one generated session for the ingest call, containing that call's new conversations. Multiple independent roots still create separate conversations within that session. Exact handling of mixed per-message supplied sessions is not finalised.

Consequent validation question remains: a missing session creates a new session, so a reply to an old session's message or use of its existing conversation would conflict with one-session ownership. Recommend requiring the caller to supply the existing session_id for continuation and rejecting a conflicting reply/conversation. An explicit cross-session related_to link could be designed separately; do not silently move conversations or infer the old session.

No implementation change.


## Implementation checkpoint and latest steering

Owner authorised agent implementation, then asked to inspect intake duplication and plan next steps. Breaking public renames, canonical gateway dispatch, identity modules and initial contract tests are present in uncommitted changes. Further restructuring is paused. See [INGESTION_SINGLE_PATH_PLAN_2026-10-16.md](INGESTION_SINGLE_PATH_PLAN_2026-10-16.md) for the traced responsibilities, duplication findings, planned consolidation and verification limits. Earlier notes-only statements describe earlier discussion stages.
