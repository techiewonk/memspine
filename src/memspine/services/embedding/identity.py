"""Stable remote vector-space identities shared by adapters and no-load descriptors."""

from __future__ import annotations

import hashlib
import json
from urllib.parse import urlsplit, urlunsplit

ENDPOINT_DEFAULTS = {
    "ollama": "http://localhost:11434",
    "vllm": "http://localhost:8000/v1",
    "lmstudio": "http://localhost:1234/v1",
    "llamacpp_server": "http://localhost:8080/v1",
}


def remote_embedding_id(
    provider: str,
    model: str,
    dim: int,
    *,
    api_base: str | None,
    family: str,
    upstream_provider: str | None = None,
    request_dimensions: bool = False,
    query_input_type: str | None = None,
    document_input_type: str | None = None,
) -> str:
    endpoint = api_base or ENDPOINT_DEFAULTS.get(provider)
    if endpoint is not None:
        parts = urlsplit(endpoint)
        # The connector appends /v1/embeddings to either a server root or its /v1 base.
        endpoint = urlunsplit(
            (
                parts.scheme.lower(),
                parts.netloc.lower(),
                parts.path.rstrip("/").removesuffix("/v1"),
                "",
                "",
            )
        )
    payload = json.dumps(
        {"endpoint": endpoint, "family": family}, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()[:16]
    prefix = f"anyllm:{upstream_provider}" if provider == "anyllm" else provider
    suffix = f"@{dim}" if request_dimensions else ""
    if query_input_type or document_input_type:
        suffix += f"[{document_input_type}|{query_input_type}]"
    return f"{prefix}:{model}#{digest}{suffix}"
