# ADR-027 — `group_id` + `tags` on the universal record (D2)

- **Status:** accepted
- **Date:** 2026-07-10
- **Decision id:** v0.2 D2 (amends D-21 conservative-schema)
- **Phase:** v0.2 Phase D · **Tier:** DF

## Context

Namespaces are the isolation boundary (a security boundary — cross-namespace
reads are firewalled). But callers routinely need a *sub-scoping facet within* a
namespace: "just this conversation", "just this document's chunks", "everything
tagged `infra`". v0.1 had no such facet, so callers overloaded the namespace
(breaking isolation semantics) or filtered client-side after over-fetching.

D-21 keeps the record schema conservative — new columns are added only when a
real need is proven and the change is backward-compatible. Sub-scoping meets
that bar.

## Decision

Two nullable fields on `MemoryRecord`, orthogonal to namespace isolation:

- `group_id: str | None` — groups records within a namespace (a conversation, a
  document, a project). `write_episode` sets it to the shared session id so a
  transcript's turns are one group.
- `tags: list[str]` — free-form labels; a tag filter matches records carrying
  **all** requested tags.

They are a **filter, never a boundary**: two groups in one namespace are not
isolated from each other (same tenant), so this adds no new security surface.

Threading:
- **Storage** — two columns on `memory_records` (`group_id String`,
  `tags LargeBinary` orjson, both nullable; a NULL `tags` reads back `[]`). A
  `(namespace, group_id)` index. Migration `0002` adds them *guarded*: the
  0001 baseline builds from live `schema.py` metadata so a fresh DB already has
  them and 0002 skips; an existing pre-D2 file DB gets them. Both converge.
- **Engine** — `write()`, `write_messages()`, `write_episode()` accept
  `group_id`/`tags`; `retrieve()` and `search()` accept them as filters.
  `list_records(namespace, memory_type, group_id)` filters in SQL; `search`
  applies the group/tags gate in the same candidate loop as the E1
  status/quarantine gate.
- **REST** — `WriteRequest` carries `group_id`/`tags`.

Defaults (`group_id=None`, `tags=[]`, no filter) make every existing write and
read byte-identical — `profile="simple"` is unchanged.

## Consequences

- Positive: conversation/document/project sub-scoping without abusing the
  namespace; a cheap SQL group filter + an all-tags gate; event-sourced (the
  fields ride the WRITE payload, so rebuild reproduces them).
- Negative / cost: a universal-record contract change (every projection carries
  two more columns); tags are an all-match gate only (no OR / negation yet).
- Backward-compat: nullable columns + empty defaults; pre-D2 rows read back
  `group_id=None`, `tags=[]`.

## Alternatives rejected

- **Overload the namespace** — rejected: namespaces are the security boundary;
  sub-scoping through them conflates isolation with filtering.
- **A separate group/tag join table** — rejected for v0.2: two nullable columns
  on the record are simpler, rebuild-trivial, and enough for the filter use
  case; a join table can come later if multi-tag OR/negation queries are needed.
- **Tags as a native vector-store filter** — deferred: the post-retrieval gate
  in `search` is backend-agnostic and correct; native pushdown is an
  optimization, not a contract.
