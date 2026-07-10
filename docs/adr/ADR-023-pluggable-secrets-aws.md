# ADR-023 — Pluggable secrets resolver + AWS Secrets Manager

**Status:** accepted · **Date:** 2026-07-10 · **Register:** implements D-22 (two-phase bootstrap secrets tier) · **Phase:** services/* completion — Phase 3

## Context

Secrets resolve in **bootstrap phase 1**, *before* `MemspineConfig` exists — the
config loader needs them to expand `${SECRET}` references, so they cannot
themselves be selected by config (the chicken-and-egg the D-22 two-phase
bootstrap avoids). The only backend was `EnvSecrets` (process env + a minimal
`.env` parser). Deployments on AWS had no first-class way to source secrets from
**AWS Secrets Manager**; the `[aws]` extra was reserved but unused for secrets.

## Decision

1. **`SecretsService` gains two more implementations** (still sync — phase 1 owns
   no event loop):
   - `services/secrets/aws.py` `AwsSecrets` (`[aws]`, boto3): reads one
     **JSON-bundle secret** (`MEMSPINE_SECRETS_AWS_SECRET_ID`, default
     `"memspine"`) once and caches it — a whole config's secrets for one API
     call — then **falls back** to `GetSecretValue(SecretId=name)` per name, with
     a negative cache for misses. Region from `AWS_REGION` / `AWS_DEFAULT_REGION`.
     A resolver **never crashes bootstrap**: any AWS error (not-found, denied,
     transient) is treated as "absent" so a lower tier or a real missing-config
     error surfaces the gap where it belongs.
   - `services/secrets/chained.py` `ChainedSecrets(*backends)`: first non-`None`
     wins; order encodes precedence.
2. **Bootstrap selector is an env var, not config.** `engine._build_secrets()`
   reads **`MEMSPINE_SECRETS_BACKEND`**:
   - `env` (default) → `EnvSecrets` (zero-cloud, unchanged behavior).
   - `aws` → `ChainedSecrets(EnvSecrets(.env), AwsSecrets())` — env/.env **first**,
     then AWS, so local dev never needs credentials and a locally-set value
     always overrides the remote one.
   - anything else → `ConfigError` naming the valid set.
3. **boto3 is import-guarded** in `AwsSecrets.__init__` → `MissingServiceError("secrets:aws", extra="aws")` (D-10). `boto3.client()` opens no
   connection (creds/region checked on first call), so construction never blocks
   or needs network at start.

## Consequences

- Positive: AWS deployments source secrets from Secrets Manager with one bundle
  call; the resolver chain means the same config runs locally (env/.env) and in
  the cloud unchanged.
- Neutral: default (`env`) behavior byte-identical; no new core dep (boto3 stays
  `[aws]`).
- Security: `AwsSecrets` swallows AWS errors by design (returns absent). This is
  correct for a *resolver* — the consumer decides whether a missing secret is
  fatal — but means a misconfigured IAM policy reads as "secret absent", not
  "access denied". Documented here so the failure mode is not surprising.

## Alternatives rejected

- **Select the backend in config** — impossible: secrets resolve before config
  exists (D-22). The env var is the only pre-config channel.
- **litellm/AWS Secrets Manager integration** — litellm's secret-manager support
  is Enterprise/proxy-only and resolves *litellm's* keys, not memspine's config
  secrets; out of scope (see the Phase-4 litellm ADR).
- **Per-name only (no bundle)** — one API call per secret; the JSON bundle is the
  common, cheap deployment shape, with per-name as the fallback.
- **python-dotenv / a secrets SDK in core** — violates D-03 slim core; the
  `.env` parser stays minimal and boto3 stays behind `[aws]`.
