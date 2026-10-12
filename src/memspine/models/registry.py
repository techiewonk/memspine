"""Typed registries for the model-side extension kinds (MOD-02): LLM providers, embedders and
rerankers. Built-ins register here at import under the names the config uses today; each factory
imports its adapter lazily, so importing this module loads nothing heavy.

Enabled third-party plug-ins add entries through :func:`memspine.plugins.enable`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from memspine.exceptions import ConfigError
from memspine.plugins import Registry

__all__ = [
    "EMBEDDERS",
    "LLM_PROVIDERS",
    "RERANKERS",
    "build_embedder",
    "llm_provider_name",
]

# --- LLM providers: factory(model, role_config) -> LLMService ---------------------------------

LLMFactory = Callable[[str, Any], Any]

LLM_PROVIDERS: Registry[LLMFactory] = Registry("llm_providers")


def _llamacpp(model: str, role: Any) -> Any:
    from memspine.models.providers.llm.llama_cpp import LlamaCppLLM

    return LlamaCppLLM(model_path=model[len("llamacpp/") :], no_think=role.no_think)


def _litellm(model: str, role: Any) -> Any:
    from memspine.models.providers.llm.litellm_llm import LiteLLMLLM

    return LiteLLMLLM(
        model,
        api_base=role.api_base,
        api_key=role.api_key,
        aws_region=role.aws_region,
        timeout_seconds=role.timeout_seconds,
        no_think=role.no_think,
    )


LLM_PROVIDERS.register("llamacpp", _llamacpp)
LLM_PROVIDERS.register("litellm", _litellm)


def llm_provider_name(model: str) -> str:
    """Route a model id to a provider: ``llamacpp/<path>`` and ``<plugin>/<id>`` for an enabled
    plug-in provider; every other id goes through LiteLLM (unchanged behaviour)."""
    prefix = model.partition("/")[0] if "/" in model else ""
    if prefix == "llamacpp" or (prefix in LLM_PROVIDERS and not LLM_PROVIDERS.is_builtin(prefix)):
        return prefix
    return "litellm"


# --- Embedders: factory(embedding_config) -> EmbeddingService ---------------------------------

EmbedderFactory = Callable[[Any], Any]

EMBEDDERS: Registry[EmbedderFactory] = Registry("embedders")


def _fastembed(cfg: Any) -> Any:
    from memspine.models.encoders.embedding.fastembed_local import FastembedEmbedding

    return FastembedEmbedding(model=cfg.model, query_instruction=cfg.query_instruction)


def _hash(cfg: Any) -> Any:
    from memspine.models.encoders.embedding.hash_local import HashEmbedding

    return HashEmbedding()


def _static(cfg: Any) -> Any:
    # E4 model2vec (ADR-020): a missing [static] extra hard-fails here (D-10) because the
    # deployer chose it as their embedder -- as a mere prefilter stage the engine skip-logs.
    from memspine.models.encoders.embedding.static_local import StaticEmbedder

    return StaticEmbedder(model=cfg.model)


def _litellm_embed(cfg: Any) -> Any:
    if cfg.dim is None:
        raise ConfigError(
            "embedding.dim is required when embedding.provider='litellm' — "
            "a cloud embedder's output dimension is not locally discoverable"
        )
    from memspine.models.encoders.embedding.litellm_embed import LiteLLMEmbedding

    return LiteLLMEmbedding(
        cfg.model,
        cfg.dim,
        api_base=cfg.api_base,
        api_key=cfg.api_key,
        aws_region=cfg.aws_region,
        request_dimensions=cfg.request_dimensions,
        query_input_type=cfg.query_input_type,
        document_input_type=cfg.document_input_type,
    )


def _st(cfg: Any) -> Any:
    if cfg.dim is None:
        raise ConfigError("embedding.dim is required when embedding.provider='st'")
    from memspine.models.encoders.embedding.st_local import SentenceTransformersEmbedding

    return SentenceTransformersEmbedding(
        cfg.model,
        cfg.dim,
        query_instruction=cfg.query_instruction,
        device=cfg.device,
        dtype=cfg.dtype,
        query_prompt_name=cfg.query_prompt_name,
        document_prompt_name=cfg.document_prompt_name,
        trust_remote_code=cfg.trust_remote_code,
    )


# Registration order is the order the "valid: ..." message lists (kept as before).
EMBEDDERS.register("fastembed", _fastembed)
EMBEDDERS.register("hash", _hash)
EMBEDDERS.register("static", _static)
EMBEDDERS.register("litellm", _litellm_embed)
EMBEDDERS.register("st", _st)


def build_embedder(cfg: Any) -> Any:
    """Construct the embedder ``cfg.provider`` names; unknown names keep the old message."""
    factory = EMBEDDERS.find(cfg.provider)
    if factory is None:
        raise ConfigError(
            f"unknown embedding.provider {cfg.provider!r} (valid: {', '.join(EMBEDDERS.names())})"
        )
    return factory(cfg)


# --- Rerankers: spec(RerankSettings) -> Reranker; built-ins register in providers/rerank/factory.py

RERANKERS: Registry[Callable[[Any], Any]] = Registry("rerankers")

# Importing the factory registers the built-in rerankers (it imports RERANKERS from here).
import memspine.models.providers.rerank.factory as _rerank_factory  # noqa: E402,F401
