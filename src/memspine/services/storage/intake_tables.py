"""Current intake ledger and public message identity tables."""

from __future__ import annotations

from sqlalchemy import Column, Integer, MetaData, String, Table, Text

intake_metadata = MetaData()

intake_source = Table(
    "intake_source",
    intake_metadata,
    Column("scope_key", String, primary_key=True),
    Column("source_id", String, primary_key=True),
    Column("state", Text, nullable=False),
)
intake_write = Table(
    "intake_write",
    intake_metadata,
    Column("scope_key", String, primary_key=True),
    Column("write_id", String, primary_key=True),
    Column("source_id", String, nullable=False),
    Column("record_id", String, nullable=True),
    Column("data", Text, nullable=False),
)
intake_intent = Table(
    "intake_intent",
    intake_metadata,
    Column("intent_id", String, primary_key=True),
    Column("scope_key", String, nullable=False),
    Column("namespace", String, nullable=False),
    Column("source_id", String, nullable=False),
    Column("status", String, nullable=False),
    Column("receipt", Text, nullable=False),
    Column("readiness", Text, nullable=False),
    # BI-01: the derivation policy promise is frozen with acceptance.  It is
    # updated only to attach the real record ids after raw application.
    Column("required_derivation", Text, nullable=False),
    Column("apply", Text, nullable=True),
    Column("created_at", String, nullable=False),
)

# Public message identity is deliberately separate from raw records. ``data`` contains only the
# source envelope metadata (including stable input fingerprints), never the message text.
message_identity = Table(
    "intake_message_identity",
    intake_metadata,
    Column("namespace", String, primary_key=True),
    Column("message_id", String, primary_key=True),
    Column("conversation_id", String, nullable=False),
    Column("session_id", String, nullable=False),
    Column("intent_id", String, nullable=True),
    Column("write_id", String, nullable=True),
    Column("record_id", String, nullable=True),
    Column("data", Text, nullable=False),
)
conversation_identity = Table(
    "intake_conversation_identity",
    intake_metadata,
    Column("namespace", String, primary_key=True),
    Column("conversation_id", String, primary_key=True),
    Column("session_id", String, nullable=False),
    Column("intent_id", String, nullable=True),
)

# BI-03: discovery reconciles every unmapped applied intent (RM-142); the mapping
# makes a reset/retry idempotent and is inserted in the outbox transaction.
intake_derivation_discovery = Table(
    "intake_derivation_discovery",
    intake_metadata,
    Column("intent_id", String, primary_key=True),
    Column("stage", String, primary_key=True),
    Column("item_offset", Integer, nullable=False),
    Column("discovered_at", String, nullable=False),
)
intake_derivation_policy = Table(
    "intake_derivation_policy",
    intake_metadata,
    Column("scope_key", String, primary_key=True),
    Column("stage", String, primary_key=True),
    Column("policy_revision", String, nullable=False),
    Column("registry_digest", String, nullable=True),
    Column("activated_at", String, nullable=False),
)

INTAKE_TABLES: tuple[str, ...] = (
    "intake_source",
    "intake_write",
    "intake_intent",
    "intake_message_identity",
    "intake_conversation_identity",
    "intake_derivation_discovery",
    "intake_derivation_policy",
)
