# ADR-058: Per-signal firewall switches, extended instruction and semantic-risk patterns, query-history anomaly, extended PII pack

- **Status:** accepted (new signals off by default; the secret-pattern additions are a bug fix)
- **Date:** 2026-10-07
- **Decision id:** D-82. Plan v3.2 rows W2, N20, N22, N23.

## Context

Free measurements against local benchmark data (2026-10-07, no model calls):

- **ASB.** The base `instruction_shaped()` caught none of ASB's three wrapper templates:
  - naive "You must strictly adhere to … and do NOT use other tools";
  - escape-character;
  - fake-completion "Answer: task complete."

  It caught nothing in MEM-INV's forged "SYSTEM UPDATE: Ignore the previous sentence".
- **No ablation switches.** The firewall had no per-signal switches for ablation.
- **AgentPoison / MemSAD.** AgentPoison-style planted memories are optimised to sit close to the attacker's trigger queries (MemSAD). MAPLE-Guard adds self-claimed authority and answer binding as write signals.
- **PersonaMem-v2.** With `redact_secrets` and `pii: redact`, 198 of 511 sensitive snippets were masked. Misses:
  - every `sk-` provider key (`LLM_API_KEY=sk-…`);
  - URL credentials;
  - card numbers that fail Luhn;
  - bank accounts, passports, licences, plates and addresses.

## Decision

**1. `firewall.signals`** (`FirewallSignalsConfig` → `core.firewall.FirewallSignals`).
- These signals stay on by default:
  - `instruction` (base patterns);
  - `anomaly` (embedding outlier);
  - `minja_bridge` (bridge prefix).
- New signals, all off:
  - `instruction_extended` (W2): the base patterns plus imperative "must / please strictly adhere / follow", "do not use other tools", "ignore the previous sentence / message / context", "Answer: task complete", overrides, prompt-reveal requests, chat-template tokens, all-caps "SYSTEM UPDATE". First-person preferences ("I strictly follow a vegan diet") are excluded;
  - `semantic_risk` (N20): self-claimed authority and answer binding. A hit sets `instruction_flag` with reason `semantic_risk:<kind>`. It quarantines only untrusted origins, as instruction framing does;
  - `query_anomaly` (N22): `QueryHistory` keeps the last 64 query vectors per namespace and a baseline of 256 write scores. A write whose best cosine to recent queries exceeds mean + `query_anomaly_kappa`·σ (once 20 baseline scores exist) is anomalous. In memory; a warm-up, never a gate on its own.

**2. Secret patterns** (bug fix, under the existing `redact_secrets`): `llm_api_key` (`sk-`, `sk-proj-`, `sk-ant-api03-`), `google_api_key`, `stripe_key`, `url_credential`, and env-style `*_API_KEY=` / `*_TOKEN=` assignments.

**3. `firewall.pii_extended`** (N23, off). With `pii` on, cue-anchored kinds that have no checksum. Only the value is masked, never the cue:
- `payment_card` (after a card cue);
- `bank_account`;
- `passport`;
- `driving_licence`;
- `licence_plate`;
- `street_address` (a street shape, or an `Address:` cue).

## Consequences

**ASB framed attack instructions** (400 each, naive / escape / fake-completion), using `instruction_extended`:

| Template | Base patterns | `instruction_extended` |
|---|---|---|
| naive | 0% | 100% |
| escape-character | 0% | 100% |
| fake-completion | 0% | 100% |
| context-ignoring | 100% | 100% |
| combined | 100% | 100% |

**What content patterns still miss** (expected):
- raw attack instructions without a wrapper: 0 / 400;
- PoisonedRAG passages: 0 / 1,500.

Declarative poisons are contained by trust, provenance and the anomaly signals, not by content.

**False flags:**
- PrefEval stated preferences: 0 / 1,018;
- LoCoMo turns: 1 / 5,882 (unchanged from the base patterns);
- `semantic_risk` on LoCoMo turns: 0 / 5,882.

**PersonaMem-v2 sensitive snippets masked:**

| Setting | Masked |
|---|---|
| Before | 198 / 511 |
| Secret fixes | 246 / 511 |
| With `pii_extended` | 409 / 511 (80.0%) |

`pii_extended` raises no extra kinds on LoCoMo turns. The remaining misses are sensitive topics (health, beliefs), not identifiers; they belong to TP-W16.

The secret-pattern additions change what an operator with `redact_secrets: true` gets masked (more, never less). This is recorded here as a correctness fix.
