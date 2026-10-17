"""Config area: firewall (F-07 split of the former ``config/schema.py``)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class FirewallSignalsConfig(BaseModel):
    """W2 / N20 / N22 (plan v3.2): which write signals the firewall runs.

    The defaults reproduce the firewall before W2. ``instruction_extended`` widens the
    instruction patterns (ASB / MEM-INV framings); ``semantic_risk`` flags content that
    claims its own authority or binds a future answer (MAPLE-Guard); ``query_anomaly``
    flags a write that sits unusually close to recently asked queries (MemSAD; z-score
    above ``query_anomaly_kappa``). Turning ``instruction``, ``anomaly`` or
    ``minja_bridge`` off is an ablation arm, never a production setting.
    """

    model_config = ConfigDict(extra="forbid")

    instruction: bool = True
    anomaly: bool = True
    minja_bridge: bool = True
    instruction_extended: bool = False
    semantic_risk: bool = False
    query_anomaly: bool = False
    query_anomaly_kappa: float = Field(default=3.0, gt=0.0)
    #: I21 (opt-in): roles whose writes skip the MINJA prefix-repeat / embedding-outlier
    #: signal. Default empty = unchanged. A chat assistant's templated openers share a
    #: 96-char prefix and are otherwise quarantined (measured: 39/40 on a synthetic chat);
    #: ``minja_bridge_exempt_roles: [assistant]`` is the chat-data setting. The exemption
    #: removes the prefix defence for that role (instruction patterns still apply).
    minja_bridge_exempt_roles: list[str] = Field(default_factory=list)
    anomaly_exempt_roles: list[str] = Field(default_factory=list)
    #: I21: shared-prefix length for the bridge signal; None = 96 (unchanged).
    minja_bridge_prefix_chars: int | None = Field(default=None, ge=16)


class FirewallConfig(BaseModel):
    """Memory Firewall switches (E1 + B8). Defaults reproduce the pre-B8 firewall.

    - ``enabled``: ``false`` keeps trust scoring but disables flagging, anomaly
      checks and quarantine: the N1 ablation arm. Never use it in production.
    - ``redact_secrets``: replace cloud keys, tokens, JWTs, private keys,
      ``key=value`` credentials and emails with ``[REDACTED:<kind>]`` at write,
      in content, entity, attribute and tags.
    - ``pii``: the PII pack, ``off`` | ``redact`` | ``tag`` (see the field).
    - ``max_content_chars``: a non-privileged write longer than this is
      quarantined (size anomaly, a common bulk-injection signature).
    - ``protected_keys``: fact keys (``entity`` or ``entity.attribute``) only an
      operator or system source may write; others are quarantined.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    #: W2-04 (LIB-38, G-06): the Memory Firewall at its gates through
    #: ``trust.gates``: gate 1 (write screen, en/de/nl/es signals, MINJA recall
    #: bridge), gate 2 (extraction input filter), gate 3 (derived records: the
    #: POL-2 cap and a hold on a quarantined, unreadable or missing parent),
    #: gate 4 (read admission) and the S4-4 family rule on quarantine release.
    #: ``enabled: false`` still disables it. Off: unchanged.
    gates_v2: bool = False
    redact_secrets: bool = False
    #: #44 PII pack (phone, Luhn-checked card, US SSN, mod-97 IBAN, IPv4/IPv6) over
    #: content, entity, attribute and tags. ``redact`` replaces each match with
    #: ``[REDACTED:<kind>]``; ``tag`` keeps the text, adds ``pii:<kind>`` tags and
    #: raises the record's ``pii_tier`` to at least ``high``; ``off`` does neither.
    pii: Literal["off", "redact", "tag"] = "off"
    #: N23 (plan v3.2): with ``pii`` on, also the cue-anchored kinds that have no
    #: checksum: card numbers after a card cue, bank accounts, passports, driving
    #: licences, licence plates, street addresses. Off: the #44 pack only.
    pii_extended: bool = False
    #: W16 (plan v3.2): tag GDPR art. 9-style sensitive topics (health, religion,
    #: sexual orientation, politics, ethnicity, legal, financial hardship;
    #: ``core/sensitive.py``) as ``sensitive:<topic>`` and raise ``pii_tier`` to at least
    #: ``high``. Text is kept; consent / purpose / remote-LLM tier rules then apply.
    sensitive_topics: bool = False
    max_content_chars: int | None = Field(default=None, ge=1)
    protected_keys: list[str] = Field(default_factory=list)
    #: H21 (G->D self-contamination): ``ingest_messages`` never deposits turns whose role
    #: is listed here (e.g. ``["system", "tool"]``).
    skip_message_roles: list[str] = Field(default_factory=list)
    #: H21: never re-deposit a turn that carries memspine's own assembly markers
    #: (recalled memory echoed back into the conversation).
    skip_injected_recall: bool = False
    #: H21: tag assistant turns ``assistant_claim``: a proposal, not an observed fact.
    tag_assistant_claims: bool = False
    #: I43: a hearsay record (``rep:`` tag from ``memories.episodic.policies.perspective``:
    #: "my mom said X", "I heard X") gets at most this trust, so it ranks below a first-hand
    #: statement and, through the parent cap, so does everything derived from it. None: off.
    hearsay_trust_cap: float | None = Field(default=None, ge=0.0, le=1.0)
    #: W2 / N20 / N22 (plan v3.2): per-signal switches (see FirewallSignalsConfig).
    signals: FirewallSignalsConfig = Field(default_factory=FirewallSignalsConfig)
