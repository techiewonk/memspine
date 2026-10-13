"""Config area: privacy (F-07 split of the former ``config/schema.py``)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RetentionClassConfig(BaseModel):
    """#48: one retention class. Records of a namespace matching ``namespace`` (a
    glob, ``*`` = any) and of ``memory_type`` (None = any type) expire ``ttl_days``
    after they were recorded."""

    model_config = ConfigDict(extra="forbid")

    namespace: str = "*"
    memory_type: str | None = None
    ttl_days: float = Field(gt=0.0)


class RetentionConfig(BaseModel):
    """#48 retention classes (storage limitation). ``classes`` is checked in order;
    the first class that matches a record sets its TTL. A sleep-cycle stage
    hard-forgets expired records through the ordinary forget path, so legal holds
    and the per-type ``retention`` policy's ``may_delete`` are respected. Empty
    (default): no stage runs and nothing expires."""

    model_config = ConfigDict(extra="forbid")

    classes: list[RetentionClassConfig] = Field(default_factory=list)


class AuditConfig(BaseModel):
    """#49 durable audit, opt-in.

    - ``reads``: every ``search`` / ``assemble`` / ``read`` / ``retrieve`` /
      ``shared_search`` / ``export`` appends a ``memory.read_audit`` event (principal,
      namespace, returned record ids, purpose, time).
    - ``actions``: ``forget``, ``correct``, retention expiry and export append a
      ``memory.audit`` event with the actor and the reason.

    Both kinds carry a hash chain (each event stores the previous event's hash);
    ``Engine.audit_chain_ok()`` validates it. No projector reads them.
    """

    model_config = ConfigDict(extra="forbid")

    reads: bool = False
    actions: bool = False


class ConsentConfig(BaseModel):
    """#50 purpose limitation and the remote-LLM gate, opt-in.

    - ``enforce``: a read returns only records whose purpose set (``consent_tags``,
      set by ``write(..., purposes=[...])``) allows the read's ``purpose``. A record
      with ``*`` allows every purpose. A read without a purpose sees only untagged
      records.
    - ``untagged``: whether records with no purpose are visible to every read
      (``allow``) or to none (``deny``) while ``enforce`` is on.
    - ``remote_llm_max_tier``: text of records whose ``pii_tier`` is above this tier
      is withheld from every prompt sent to a remote LLM provider. None = off.
    - ``local_hosts``: extra ``api_base`` host names that count as local.
    """

    model_config = ConfigDict(extra="forbid")

    enforce: bool = False
    untagged: Literal["allow", "deny"] = "allow"
    remote_llm_max_tier: Literal["none", "low", "high", "regulated"] | None = None
    local_hosts: list[str] = Field(default_factory=list)
