# ADR-035 — Encryption at rest: SQLCipher option, crypto-shredding design

- **Status:** accepted (SQLCipher option) · proposed (crypto-shredding)
- **Date:** 2026-10-05
- **Decision id:** register row to be assigned at merge (master absorb list #52, TS-8)
- **Phase:** P7 · **Tier:** DF

## Context

The SQLite file holds the whole event log and the read model in plaintext. A copied
disk, a backup or a stray `-wal` file gives away every memory. Hard forget (#43)
redacts log payloads, zeroes freed pages (`secure_delete`) and truncates the WAL, but
it cannot reach backups taken before the erasure. Regulated deployments ask for two
things: the file unreadable without a key, and erasure that also reaches backups.

## Decision

1. **`storage.encryption: {mode: none | sqlcipher, key_env: NAME}`**, default `none`.
   With `sqlcipher`, `SQLiteClient` (`clients/sqlite.py`) opens every pooled connection
   through the SQLCipher DB-API driver (`sqlcipher3`, else `pysqlcipher3`) under
   aiosqlite, runs `PRAGMA key` before any other statement and reads
   `sqlite_master` once, so a wrong key fails at `start()` with a `StorageError`. The
   Alembic migration run gets the same keyed connection (`ensure_schema(creator=...)`),
   so no plaintext page is ever written by the migration path.
   - The driver ships in a new extra, **`memspine[encrypt]`** (`sqlcipher3`). It is not
     in `all` (it needs a native SQLCipher build on some platforms). A missing driver
     raises `MissingServiceError("storage.encryption.sqlcipher", extra="encrypt")` (D-10).
   - The key is read **only** from the environment variable named by `key_env`, once
     per `connect()`. It is never a config value, never logged, never on a public
     attribute, and absent from `repr()` and from every error message (it is held in a
     `_Secret` whose `repr` is `<redacted>`). Startup logs `storage.encryption_partial`
     naming the files the option does not cover, and the variable *name* only.
   - `:memory:` and `storage.backend: postgres` are config errors with sqlcipher
     (nothing to encrypt; Postgres has its own at-rest encryption).

2. **Crypto-shredding (proposed, not built): per-namespace field keys.** Design:
   - A key-encryption key (KEK) comes from the secrets port (D-22): env, or AWS
     Secrets Manager / KMS. Each namespace gets a random 256-bit data key (DEK),
     stored wrapped by the KEK in a `namespace_keys` table (`namespace`, `wrapped_dek`,
     `kek_id`, `created_at`, `shredded_at`).
   - The fields that carry user content are sealed with AES-256-GCM under the
     namespace DEK before they reach the log or the read model: event `payload`
     record snapshots (`content`, `entity`, `attribute`, `tags`, `history[].content`,
     `source.doc_path`), `memory_records.content` / `content_zstd`, and FEEDBACK notes.
     Nonce per field; the associated data binds `(namespace, record_id, field)` so a
     sealed value cannot be moved to another record.
   - Fingerprints stay over plaintext but become keyed (HMAC under a per-namespace
     subkey), so dedup and corroboration keep working without leaking content.
   - `erase_namespace(ns)` (and a per-subject variant with per-subject DEKs) deletes
     the wrapped DEK and appends a `memory.forget` event marking the namespace
     shredded. Every copy of that namespace's sealed fields, in the live file **and in
     every backup**, becomes unreadable at once: erasure reaches backups without
     rewriting them.
   - Derived stores must follow: LanceDB vectors and the Tantivy index are rebuilt
     from the log, so they either hold only sealed text plus vectors (vectors leak
     semantics: they must be dropped for the shredded namespace, which a rebuild does)
     or are encrypted by their own volume.
   - Cost: every read decrypts; keyed fingerprints change the dedup key, so existing
     databases need a one-off migration; lexical BM25 over sealed text is impossible
     unless the index itself is encrypted at the volume level.
   This is a larger change touching the write door, the projectors and erasure, which
   other work streams own today; it is recorded here as the agreed design and left as
   future work.

## Consequences

- Positive: opt-in at-rest protection for the system of record with one config block;
  defaults, `template="core"` and the golden snapshot are unchanged; migrations run on
  keyed connections; the key handling is testable (read from one variable, never
  rendered).
- Negative / cost: covers the SQLite file only. **Not encrypted by this option:**
  LanceDB vectors and the Tantivy lexical index (the `storage.data_dir` / beside-the-db
  directories; the lexical index holds record text), disk caches (`cache.backend:
  disk`), a DBOS system database, logs. Use volume encryption for those, or turn
  `read.hybrid` off to avoid the lexical copy. A lost key means a lost database.
  Changing the key (`PRAGMA rekey`) is not wrapped yet.
- Follow-up: crypto-shredding (above); `rekey` as an operator command; Postgres
  guidance (TDE / pgcrypto) in the deployment docs.

## Alternatives rejected

- **Key as a config value (`${secret:...}`)** — the resolved config is printable
  (`memspine config resolve`), so a key in it would leak into terminals and logs. An
  environment variable name keeps the value out of every config surface.
- **Application-level encryption of the whole file (e.g. encrypting on close)** —
  WAL pages, crash states and concurrent readers would see plaintext.
- **SQLite's SEE** — proprietary licence.
