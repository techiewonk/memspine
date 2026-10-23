"""Lazy HF-cache ONNX sessions and tokenizers; no model downloads at import."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from memspine.clients.weights.onnx_catalogue import OnnxModelSpec, resolve_model_spec, spec_identity
from memspine.clients.weights.shared import SharedWeights, WeightRegistry

WARM_TEXT = "warm-up"


class OnnxModel:
    """Native synchronous inference handle. Tokenizer mutations are serialized per session."""

    def __init__(
        self,
        spec: OnnxModelSpec,
        *,
        threads: int = 0,
        providers: Sequence[str] | None = None,
        local_files_only: bool = False,
    ) -> None:
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        from tokenizers import AddedToken, Tokenizer

        if type(threads) is not int or threads < 0:
            raise ValueError("ONNX threads must be a nonnegative integer")
        self.spec = spec
        self._lock = threading.Lock()

        def download(filename: str) -> str:
            try:
                return str(
                    hf_hub_download(
                        spec.repo,
                        filename,
                        revision=spec.revision,
                        local_files_only=local_files_only,
                    )
                )
            except Exception as exc:
                raise FileNotFoundError(
                    f"ONNX required file {filename!r} unavailable for {spec.repo}@{spec.revision}"
                    f" (local_files_only={local_files_only})"
                ) from exc

        model_path = download(spec.model_file)
        for filename in spec.additional_files:
            download(filename)
        tokenizer_path = download(spec.tokenizer_file)
        config = json.loads(Path(download("tokenizer_config.json")).read_text(encoding="utf-8"))
        self.tokenizer = Tokenizer.from_file(tokenizer_path)
        # Repositories declare additional special tokens beyond tokenizer.json.
        tokens = json.loads(Path(download("special_tokens_map.json")).read_text(encoding="utf-8"))
        for value in tokens.values():
            for token in value if isinstance(value, list) else [value]:
                self.tokenizer.add_special_tokens(
                    [AddedToken(**token) if isinstance(token, dict) else token]
                )
        truncation = self.tokenizer.truncation or {}
        self.tokenizer.enable_truncation(
            max_length=spec.max_length,
            direction=config.get("truncation_side") or truncation.get("direction", "right"),
        )
        padding = self.tokenizer.padding or {}
        pad_token = padding.get("pad_token") or config.get("pad_token")
        pad_id = padding.get("pad_id")
        if pad_id is None and pad_token is not None:
            pad_id = self.tokenizer.token_to_id(pad_token)
        if pad_token is None or pad_id is None:
            raise ValueError(f"ONNX tokenizer {spec.repo} does not declare a valid pad token")
        self.tokenizer.enable_padding(
            direction=config.get("padding_side") or padding.get("direction", "right"),
            pad_id=pad_id,
            pad_token=pad_token,
            pad_type_id=padding.get("pad_type_id", 0),
            pad_to_multiple_of=padding.get("pad_to_multiple_of"),
            length=None,
        )
        selected = list(providers or ["CPUExecutionProvider"])
        unavailable = set(selected) - set(ort.get_available_providers())
        if unavailable:
            raise ValueError(
                f"ONNX execution providers unavailable: {', '.join(sorted(unavailable))}"
            )
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = threads
        self.session = ort.InferenceSession(model_path, sess_options=options, providers=selected)
        self._input_names = {node.name for node in self.session.get_inputs()}

    def _run(
        self, texts: list[str] | list[tuple[str, str]], *, pairs: bool = False
    ) -> tuple[Any, Any]:
        import numpy as np

        with self._lock:
            encoded = self.tokenizer.encode_batch(texts)
        mask = np.array([item.attention_mask for item in encoded], dtype=np.int64)
        ids = np.array([item.ids for item in encoded], dtype=np.int64)
        inputs = {"input_ids": ids}
        if "attention_mask" in self._input_names:
            inputs["attention_mask"] = mask
        if "token_type_ids" in self._input_names:
            inputs["token_type_ids"] = (
                np.array([item.type_ids for item in encoded], dtype=np.int64)
                if pairs
                else np.zeros_like(ids)
            )
        return self.session.run(None, inputs)[0], mask

    def embed(self, texts: list[str], *, batch_size: int = 256) -> Iterable[Any]:
        import numpy as np

        if batch_size < 1:
            raise ValueError("ONNX batch_size must be positive")
        if self.spec.pooling == "score":
            raise ValueError("A score model cannot embed text")
        for offset in range(0, len(texts), batch_size):
            output, mask = self._run(texts[offset : offset + batch_size])
            if output.ndim == 3:
                if self.spec.pooling == "cls":
                    pooled = output[:, 0]
                else:
                    pooled = np.sum(
                        output, axis=1, where=mask[:, :, None].astype(bool), dtype=np.float64
                    ) / np.maximum(mask.sum(axis=1, keepdims=True), 1e-9)
                    # fastembed's normalized mean path normalizes float64 before casting;
                    # its unnormalized mean path casts immediately.
            elif output.ndim == 2:
                pooled = output
            else:
                raise ValueError(f"Unsupported ONNX embedding shape: {output.shape}")
            if self.spec.normalize:
                norm = np.linalg.norm(pooled, axis=1, keepdims=True)
                pooled = pooled / np.maximum(norm, 1e-12)
            pooled = pooled.astype(output.dtype, copy=False)
            if pooled.shape[1] != self.spec.dim:
                raise ValueError("ONNX output dimension differs from model_spec.dim")
            yield from pooled

    def rerank(self, query: str, documents: list[str], *, batch_size: int = 64) -> Iterable[float]:
        if batch_size < 1:
            raise ValueError("ONNX batch_size must be positive")
        if self.spec.pooling != "score":
            raise ValueError("An embedding model cannot rerank pairs")
        for offset in range(0, len(documents), batch_size):
            output, _ = self._run(
                [(query, text) for text in documents[offset : offset + batch_size]], pairs=True
            )
            yield from (float(value) for value in output[:, 0])


def _weights(
    model: str,
    *,
    rerank: bool,
    model_spec: dict[str, Any] | OnnxModelSpec | None = None,
    threads: int = 0,
    providers: Sequence[str] | None = None,
    local_files_only: bool = False,
    registry: WeightRegistry | None = None,
) -> SharedWeights[OnnxModel]:
    spec = resolve_model_spec(model, model_spec)
    if type(threads) is not int or threads < 0:
        raise ValueError("ONNX threads must be a nonnegative integer")
    if providers is not None and (
        not providers
        or isinstance(providers, str)
        or any(not isinstance(provider, str) or not provider for provider in providers)
    ):
        raise ValueError("ONNX providers must be a nonempty sequence of provider names")
    selected = tuple(providers) if providers is not None else ("CPUExecutionProvider",)
    if (spec.pooling == "score") != rerank:
        raise ValueError("ONNX model catalogue task does not match requested embedding/rerank task")
    return SharedWeights(
        "model.rerank.onnx" if rerank else "model.embedding.onnx",
        spec_identity(spec),
        {
            "threads": threads,
            "providers": selected,
            "local_files_only": local_files_only,
        },
        lambda: OnnxModel(
            spec, threads=threads, providers=selected, local_files_only=local_files_only
        ),
        warm=(lambda loaded: list(loaded.rerank(WARM_TEXT, [WARM_TEXT])))
        if rerank
        else (lambda loaded: list(loaded.embed([WARM_TEXT]))),
        registry=registry,
    )


def text_weights(
    model: str,
    *,
    model_spec: dict[str, Any] | OnnxModelSpec | None = None,
    threads: int = 0,
    providers: Sequence[str] | None = None,
    local_files_only: bool = False,
    registry: WeightRegistry | None = None,
) -> SharedWeights[OnnxModel]:
    return _weights(
        model,
        rerank=False,
        model_spec=model_spec,
        threads=threads,
        providers=providers,
        local_files_only=local_files_only,
        registry=registry,
    )


def rerank_weights(
    model: str,
    *,
    model_spec: dict[str, Any] | OnnxModelSpec | None = None,
    threads: int = 0,
    providers: Sequence[str] | None = None,
    local_files_only: bool = False,
    registry: WeightRegistry | None = None,
) -> SharedWeights[OnnxModel]:
    return _weights(
        model,
        rerank=True,
        model_spec=model_spec,
        threads=threads,
        providers=providers,
        local_files_only=local_files_only,
        registry=registry,
    )
