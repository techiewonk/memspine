"""OpenDecider-nano inference, ported to memspine (no ``opendecider`` package).

Derived from OpenDecider (https://github.com/manjunathshiva/opendecider, tag v0.8.1, commit
4fa58ce on main at the time of the port), ``opendecider/nano.py``, ``prompt.py`` (``nano_ids``,
``text_ids``) and ``questions.py`` (``options``). Copyright 2026 Manjunath Janardhan,
licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0).
Changes: only the inference path of the nano model is kept (no server, MCP, framework
integrations, guardrails or remote backends); the typed-question layer is reduced to what
memspine needs (``noul`` yes/no); length-bucketed batching, ``torch.inference_mode``, thread
control, an ONNX Runtime backend, a process-wide cache and a snapshot loader are added. See the
NOTICE file in the repository root.

The model (``manjunathshiva/opendecider-nano``, Apache-2.0, ~395M parameters) is a bidirectional
ModernBERT encoder (``jhu-clsp/ettin-encoder-400m``, fully fine-tuned) plus an MLP head
(Linear-GELU-LayerNorm-Linear, ``head.safetensors``). One question is encoded as

    [CLS] question: <instr> [SEP] [MASK] opt1: d1 [MASK] opt2: d2 ... [SEP] input: <state> [SEP]

The hidden state at each ``[MASK]`` marker goes through the head to one logit per option, and
the softmax over a question's options is its probability distribution. There is no separate
temperature: calibration is in the weights. The state is truncated first (``max_len`` 2,048
tokens in total), so every option survives. Weights are stored in bf16 and run in fp32 by
default, as the model was evaluated.
"""

from __future__ import annotations

import json
import math
import os
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_MODEL",
    "ONNX_REPO",
    "NanoModel",
    "NanoOnnx",
    "load_nano",
    "nano_ids",
    "noul_options",
    "physical_cores",
]

DEFAULT_MODEL = "manjunathshiva/opendecider-nano"
ONNX_REPO = "manjunathshiva/opendecider-nano-ONNX"
#: The published 8-bit weight-only build (MatMulNBits, block 32): ``logits`` [batch, tokens].
_ONNX_FILE = "onnx/model_q8.onnx"
_FILES = (
    "config.json",
    "model.safetensors",
    "head.safetensors",
    "opendecider.json",
    "tokenizer.json",
    "tokenizer_config.json",
)

# A fast tokenizer keeps ``split_special_tokens`` as state for its next encode, so two threads
# sharing one could swap it mid-prompt: every encode holds this lock.
_ENCODING = threading.RLock()


def noul_options(criteria: Mapping[str, str] | None = None) -> dict[str, str]:
    """A yes/no ("noul") question's options, as the model was trained: yes before no."""
    crit = criteria or {}
    return {"yes": crit.get("true", "Yes"), "no": crit.get("false", "No")}


def physical_cores() -> int:
    """Physical CPU cores (the default for intra-op threads)."""
    try:
        import psutil

        n = psutil.cpu_count(logical=False)
        if n:
            return int(n)
    except ImportError:
        pass
    return max(1, (os.cpu_count() or 2) // 2)


def _text_ids(tok: Any, text: str) -> list[int]:
    """``text`` as token ids with no special tokens; special-token text is read as plain text."""
    with _ENCODING:
        return list(tok.encode(text, add_special_tokens=False, split_special_tokens=True))


def nano_ids(
    tok: Any, state: Any, instructions: str, options: Mapping[str, str | None], max_len: int
) -> tuple[list[int], bool]:
    """The model's input ids and whether the state was shortened to fit ``max_len``."""
    st = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
    q = _text_ids(tok, f"question: {instructions}")
    opts: list[int] = []
    for k, v in options.items():
        opts += [tok.mask_token_id, *_text_ids(tok, f" {k}: {v}" if v and v != k else f" {k}")]
    s = _text_ids(tok, f"input: {st}")
    room = max(max_len - len(q) - len(opts) - 4, 0)
    ids = [
        tok.cls_token_id,
        *q,
        tok.sep_token_id,
        *opts,
        tok.sep_token_id,
        *s[:room],
        tok.sep_token_id,
    ]
    return ids, len(s) > room


class _Base:
    """Tokenisation, length-bucketed batching and the softmax over [MASK] logits.
    Subclasses implement ``_forward(rows)`` -> the logits at each row's [MASK] markers."""

    tok: Any
    max_len: int
    mask_id: int
    #: micro-batch cap (see ``decide_many``)
    max_batch = 16

    def _forward(self, rows: list[list[int]]) -> list[list[float]]:
        raise NotImplementedError

    def decide_many(
        self,
        items: Sequence[tuple[Any, str, Mapping[str, str | None]]],
        info: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, float]]:
        """``[(state, instructions, options)]`` -> ``[{option: probability}]``.

        Inputs are sorted by token length and cut into micro-batches of at most ``max_batch``
        rows, so a short question is not padded to a long neighbour; results come back in the
        input order. ``info`` (if given) receives ``{"input_tokens", "truncated"}`` per item."""
        if not items:
            return []
        built = [nano_ids(self.tok, s, ins, opts, self.max_len) for s, ins, opts in items]
        if info is not None:
            info.extend({"input_tokens": len(b), "truncated": tr} for b, tr in built)
        order = sorted(range(len(items)), key=lambda i: len(built[i][0]))
        out: list[dict[str, float] | None] = [None] * len(items)
        for start in range(0, len(order), self.max_batch):
            chunk = order[start : start + self.max_batch]
            logits = self._forward([built[i][0] for i in chunk])
            for i, row_logits in zip(chunk, logits, strict=True):
                opts = items[i][2]
                if len(row_logits) != len(opts):  # anything else would mis-assign probabilities
                    raise RuntimeError(
                        f"expected {len(opts)} option markers, found {len(row_logits)}"
                    )
                top = max(row_logits)
                exps = [math.exp(x - top) for x in row_logits]
                total = sum(exps)
                out[i] = dict(zip(opts, (e / total for e in exps), strict=True))
        return [o for o in out if o is not None]

    def yes_probability(
        self, states: Sequence[str], instructions: str, criteria: Mapping[str, str] | None = None
    ) -> list[float]:
        """P(yes) of one yes/no question about each state."""
        options = noul_options(criteria)
        return [p["yes"] for p in self.decide_many([(s, instructions, options) for s in states])]


class NanoModel(_Base):
    """PyTorch backend: encoder + head under ``torch.inference_mode``; on CPU ``threads``
    intra-op threads (default: the physical cores)."""

    def __init__(
        self,
        path: str | Path,
        device: str = "cpu",
        dtype: str = "float32",
        threads: int | None = None,
    ) -> None:
        import torch
        from safetensors.torch import load_file
        from torch import nn
        from transformers import AutoModel, AutoTokenizer

        path = Path(path)
        meta = json.loads((path / "opendecider.json").read_text(encoding="utf-8"))
        if meta.get("kind") != "nano":
            raise ValueError(f"{path} is not an opendecider-nano checkpoint: {meta.get('kind')!r}")
        self._torch = torch
        if device == "cpu":
            torch.set_num_threads(threads or physical_cores())
        self.tok = AutoTokenizer.from_pretrained(path)
        dt = getattr(torch, dtype)
        try:
            self.enc = AutoModel.from_pretrained(path, dtype=dt)
        except TypeError:  # transformers < 4.56 names it torch_dtype
            self.enc = AutoModel.from_pretrained(path, torch_dtype=dt)
        d = self.enc.config.hidden_size
        self.head = nn.Sequential(nn.Linear(d, d), nn.GELU(), nn.LayerNorm(d), nn.Linear(d, 1))
        self.head.load_state_dict(
            {k: v.float() for k, v in load_file(str(path / "head.safetensors")).items()}
        )
        self.head.to(dt)
        self.enc.to(device).eval()
        self.head.to(device).eval()
        self.device = device
        self.max_len = int(meta.get("max_len", 2048))
        self.mask_id = self.tok.mask_token_id
        self._lock = threading.Lock()

    def _forward(self, rows: list[list[int]]) -> list[list[float]]:
        torch = self._torch
        longest, pad = max(len(r) for r in rows), self.tok.pad_token_id
        x = torch.tensor([r + [pad] * (longest - len(r)) for r in rows], device=self.device)
        att = torch.tensor(
            [[1] * len(r) + [0] * (longest - len(r)) for r in rows], device=self.device
        )
        out: list[list[float]] = []
        with self._lock, torch.inference_mode():
            h = self.enc(input_ids=x, attention_mask=att).last_hidden_state
            for r, row in enumerate(rows):
                pos = torch.tensor(
                    [i for i, t in enumerate(row) if t == self.mask_id], device=self.device
                )
                out.append(self.head(h[r, pos]).squeeze(-1).float().tolist())
        return out


class NanoOnnx(_Base):
    """ONNX Runtime backend over the published 8-bit weight-only build
    (``manjunathshiva/opendecider-nano-ONNX``, ``onnx/model_q8.onnx``, 570 MB), CPU provider,
    graph optimisation ALL, ``threads`` intra-op threads. The graph returns one logit per
    token; the answer reads the [MASK] positions. Needs ``onnxruntime``."""

    def __init__(self, path: str | Path, onnx_file: str | Path, threads: int | None = None) -> None:
        import numpy as np
        import onnxruntime as ort
        from transformers import AutoTokenizer

        path = Path(path)
        meta = json.loads((path / "opendecider.json").read_text(encoding="utf-8"))
        self._np = np
        self.tok = AutoTokenizer.from_pretrained(path)
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads or physical_cores()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(
            str(onnx_file), opts, providers=["CPUExecutionProvider"]
        )
        self.max_len = int(meta.get("max_len", 2048))
        self.mask_id = self.tok.mask_token_id

    def _forward(self, rows: list[list[int]]) -> list[list[float]]:
        np = self._np
        longest, pad = max(len(r) for r in rows), self.tok.pad_token_id
        ids = np.array([r + [pad] * (longest - len(r)) for r in rows], dtype=np.int64)
        att = np.array([[1] * len(r) + [0] * (longest - len(r)) for r in rows], dtype=np.int64)
        logits = self.session.run(["logits"], {"input_ids": ids, "attention_mask": att})[0]
        return [
            [float(logits[r, i]) for i, t in enumerate(row) if t == self.mask_id]
            for r, row in enumerate(rows)
        ]


_MODELS: dict[tuple[Any, ...], _Base] = {}
_ERRORS: dict[tuple[Any, ...], Exception] = {}
_LOCK = threading.Lock()


def _snapshot(name: str) -> Path:
    path = Path(name)
    if path.is_dir():
        return path
    from huggingface_hub import snapshot_download

    return Path(snapshot_download(name, allow_patterns=list(_FILES)))


def load_nano(
    name: str = DEFAULT_MODEL,
    device: str = "cpu",
    dtype: str = "float32",
    *,
    backend: str = "torch",
    threads: int | None = None,
) -> _Base:
    """The model for ``name`` (a Hub id or a local folder), loaded once per process and
    (device, dtype, backend, threads). A failed load is cached, so a broken install is not
    retried on every read. ``backend="onnx"`` uses the published 8-bit ONNX build on CPU.
    Raises ``MissingServiceError(extra="decider")`` when the dependencies are absent."""
    if backend not in ("torch", "onnx"):
        raise ValueError(f"decider backend must be 'torch' or 'onnx', got {backend!r}")
    key = (name, device, dtype, backend, threads)
    with _LOCK:
        if key in _MODELS:
            return _MODELS[key]
        if key in _ERRORS:
            raise _ERRORS[key]
        try:
            try:
                import safetensors  # noqa: F401
                import torch
                import transformers  # noqa: F401

                if backend == "onnx":
                    import onnxruntime  # noqa: F401
            except ImportError as exc:
                from memspine.exceptions import MissingServiceError

                raise MissingServiceError("opendecider decider", extra="decider") from exc
            path = _snapshot(name)
            if backend == "onnx":
                from huggingface_hub import hf_hub_download

                _MODELS[key] = NanoOnnx(path, hf_hub_download(ONNX_REPO, _ONNX_FILE), threads)
            else:
                dev = device
                if device == "auto":
                    dev = "cuda" if torch.cuda.is_available() else "cpu"
                _MODELS[key] = NanoModel(path, dev, dtype, threads)
        except Exception as exc:
            _ERRORS[key] = exc
            raise
        return _MODELS[key]
