"""transformers weights connector (RM-113, ``[st]`` extra): Qwen3 and Jina reranker loads.

transformers and torch are imported lazily: building a connector only checks they are
importable (raising :class:`MissingServiceError` naming the ``st`` extra); the weights load on
first use in a worker thread. Shared loads are keyed by model, device and quantisation
(ADR-079 / RM-73).
"""

from __future__ import annotations

import importlib
import threading
from dataclasses import dataclass
from typing import Any

from memspine.clients.weights.shared import SharedWeights, WeightRegistry
from memspine.exceptions import MissingServiceError

__all__ = [
    "QWEN3_PREFIX",
    "QWEN3_SUFFIX",
    "JinaRerankerSpec",
    "Qwen3RerankerSpec",
    "Qwen3Weights",
    "jina_reranker_weights",
    "load_jina_reranker",
    "load_qwen3_reranker",
    "qwen3_reranker_weights",
    "require",
]

#: Qwen3-Reranker prompt frame (model card, "Usage with Transformers"); tokenised at load.
QWEN3_PREFIX = (
    "<|im_start|>system\nJudge whether the Document meets the requirements based on the "
    'Query and the Instruct provided. Note that the answer can only be "yes" or "no".'
    "<|im_end|>\n<|im_start|>user\n"
)
QWEN3_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"


def require(name: str, service: str) -> Any:
    """Import ``name`` or raise :class:`MissingServiceError` naming the ``st`` extra."""
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise MissingServiceError(service, extra="st") from exc


# -- Qwen3-Reranker ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Qwen3RerankerSpec:
    model: str
    device: str | None = None
    #: bitsandbytes quantisation: "4bit" | "8bit" | None
    quant: str | None = None


@dataclass(frozen=True, slots=True)
class Qwen3Weights:
    """Everything one load produces; shared by every holder of the same key."""

    tokenizer: Any
    model: Any
    prefix: list[int]
    suffix: list[int]
    yes_id: int
    no_id: int
    #: HF fast tokenizers are not safe to share across threads: one lock per loaded tokenizer.
    infer_lock: Any


_QWEN3_SERVICE = "services.rerank.qwen3 (transformers + torch)"


def load_qwen3_reranker(spec: Qwen3RerankerSpec) -> Qwen3Weights:
    """Load tokenizer and weights (float32 on CPU: bf16 there is slow and drifts)."""
    transformers = require("transformers", _QWEN3_SERVICE)
    torch = require("torch", _QWEN3_SERVICE)
    tokenizer = transformers.AutoTokenizer.from_pretrained(spec.model, padding_side="left")
    auto_lm = transformers.AutoModelForCausalLM
    if spec.quant is not None:
        # bitsandbytes places the quantised weights on the GPU itself.
        bnb = {"4bit": {"load_in_4bit": True}, "8bit": {"load_in_8bit": True}}[spec.quant]
        cfg = transformers.BitsAndBytesConfig(**bnb, bnb_4bit_compute_dtype=torch.bfloat16)
        model = auto_lm.from_pretrained(
            spec.model, quantization_config=cfg, device_map={"": spec.device or 0}
        ).eval()
    else:
        model = auto_lm.from_pretrained(spec.model).eval()
        if spec.device is None or str(spec.device).startswith("cpu"):
            model = model.float()  # bf16 on CPU: slow and drifts under padding
        if spec.device is not None:
            model = model.to(spec.device)
    return Qwen3Weights(
        tokenizer=tokenizer,
        model=model,
        prefix=list(tokenizer.encode(QWEN3_PREFIX, add_special_tokens=False)),
        suffix=list(tokenizer.encode(QWEN3_SUFFIX, add_special_tokens=False)),
        yes_id=int(tokenizer.convert_tokens_to_ids("yes")),
        no_id=int(tokenizer.convert_tokens_to_ids("no")),
        infer_lock=threading.Lock(),
    )


def qwen3_reranker_weights(
    spec: Qwen3RerankerSpec, *, registry: WeightRegistry | None = None
) -> SharedWeights[Qwen3Weights]:
    require("transformers", _QWEN3_SERVICE)
    require("torch", _QWEN3_SERVICE)
    return SharedWeights(
        "model.reranker.qwen3",
        {"model": spec.model},
        {"device": spec.device, "quant": spec.quant},
        lambda: load_qwen3_reranker(spec),
        registry=registry,
    )


# -- Jina listwise reranker -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JinaRerankerSpec:
    model: str
    device: str | None = None


_JINA_SERVICE = "services.rerank.jina (transformers + torch)"


def load_jina_reranker(spec: JinaRerankerSpec) -> Any:
    """The model ships custom code, so loading needs ``trust_remote_code=True``: a deployer
    who picks ``rerank: jina`` opts into running that repository's Python."""
    transformers = require("transformers", _JINA_SERVICE)
    model = transformers.AutoModel.from_pretrained(spec.model, dtype="auto", trust_remote_code=True)
    if spec.device is not None:
        model = model.to(spec.device)
    return model.eval()


def jina_reranker_weights(
    spec: JinaRerankerSpec, *, registry: WeightRegistry | None = None
) -> SharedWeights[tuple[Any, Any]]:
    """A lease on (model, inference lock)."""
    require("transformers", _JINA_SERVICE)
    require("torch", _JINA_SERVICE)
    return SharedWeights(
        "model.reranker.jina",
        {"model": spec.model},
        {"device": spec.device},
        lambda: (load_jina_reranker(spec), threading.Lock()),
        warm=lambda loaded: loaded[0].rerank("warm-up", ["warm-up"]),
        registry=registry,
    )
