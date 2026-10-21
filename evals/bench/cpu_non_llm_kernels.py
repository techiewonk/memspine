"""Small synthetic CPU kernel probe. Not LoCoMo or a best-in-class leaderboard."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import sys
import threading
import time
from collections import Counter
from pathlib import Path

LABELS = [
    "The speaker states where they currently live or reside.",
    "The speaker states their workplace location, not their home.",
    "The speaker describes a completed visit or trip to a place.",
    "The speaker plans or intends a future move, not a completed relocation.",
    "Someone gave or received an item as a gift.",
    "The speaker read or is reading a book or novel.",
]
TEXTS = [
    ("My home is in Lisbon now.", 0),
    ("I currently reside in Porto.", 0),
    ("I live in a flat in Bristol.", 0),
    ("I have made York my permanent home.", 0),
    ("My office is in Lisbon.", 1),
    ("I work at a clinic in Porto.", 1),
    ("My workplace is in Bristol although I live elsewhere.", 1),
    ("The company employs me at its York office.", 1),
    ("I visited Lisbon during my holiday.", 2),
    ("I spent last weekend sightseeing in Porto.", 2),
    ("My trip to Bristol finished yesterday.", 2),
    ("I went to York for a short vacation.", 2),
    ("I plan to move to Lisbon next month.", 3),
    ("I might relocate to Porto if I find a job.", 3),
    ("I intend to settle in Bristol after graduation.", 3),
    ("Moving to York is a goal, not something I have done.", 3),
    ("I gave Anita my guitar as a present.", 4),
    ("My brother gifted me a watch.", 4),
    ("I received a birthday present from a friend.", 4),
    ("I bought a painting as a gift for my mother.", 4),
    ("I finished reading a novel yesterday.", 5),
    ("I am halfway through this book.", 5),
    ("I read the biography during my holiday.", 5),
    ("I spent the evening reading a mystery story.", 5),
]
NER = [
    (
        "Sofia moved from Porto to Lisbon.",
        [("Sofia", "person"), ("Porto", "location"), ("Lisbon", "location")],
    ),
    (
        "Anita works for Acme in Bristol.",
        [("Anita", "person"), ("Acme", "organization"), ("Bristol", "location")],
    ),
    (
        "David visited Rome with Elena.",
        [("David", "person"), ("Rome", "location"), ("Elena", "person")],
    ),
    (
        "Maya received a gift from Oliver in York.",
        [("Maya", "person"), ("Oliver", "person"), ("York", "location")],
    ),
    (
        "Nora works for Microsoft in London.",
        [("Nora", "person"), ("Microsoft", "organization"), ("London", "location")],
    ),
    ("Leo met Priya in Paris.", [("Leo", "person"), ("Priya", "person"), ("Paris", "location")]),
    (
        "Sara lives in Madrid and works for Google.",
        [("Sara", "person"), ("Madrid", "location"), ("Google", "organization")],
    ),
    (
        "Daniel travelled from Berlin to Vienna.",
        [("Daniel", "person"), ("Berlin", "location"), ("Vienna", "location")],
    ),
]
RERANK = [
    (
        "Where does Sofia live?",
        [
            "Sofia lives in Lisbon.",
            "Sofia visited Porto.",
            "Anita lives in Rome.",
            "Sofia works in York.",
        ],
        0,
    ),
    (
        "Where does Anita work?",
        [
            "Anita lives in Bristol.",
            "Anita works in York.",
            "Sofia works in Paris.",
            "Anita visited Lisbon.",
        ],
        1,
    ),
    (
        "Who gave Maya the guitar?",
        [
            "Maya gave David a watch.",
            "Oliver made a guitar.",
            "Anita gave Maya the guitar.",
            "Maya plans to buy a guitar.",
        ],
        2,
    ),
    (
        "Did David attend the class?",
        [
            "David was invited to the class.",
            "David offers a class.",
            "Elena attended the class.",
            "David attended the class yesterday.",
        ],
        3,
    ),
    (
        "What does Nora plan to do?",
        [
            "Nora moved to Paris last year.",
            "Nora plans to move to Rome.",
            "Leo plans to visit Rome.",
            "Nora did not attend a class.",
        ],
        1,
    ),
    (
        "What is the colour of the guitar?",
        [
            "The blue sofa is next to the guitar.",
            "The guitar belongs to Maya.",
            "The guitar is red.",
            "The watch is red.",
        ],
        2,
    ),
    (
        "When did the studio open?",
        [
            "The studio opened in September.",
            "The studio was planned in March.",
            "Another studio opened in May.",
            "The studio offers yoga.",
        ],
        0,
    ),
    (
        "How far did Leo walk?",
        [
            "Leo walked for two hours.",
            "Priya walked five kilometres.",
            "Leo plans to walk ten kilometres.",
            "Leo walked three kilometres.",
        ],
        3,
    ),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--kind",
        choices=["static", "encoder", "gliner", "gliclass", "reranker", "bm25"],
        required=True,
    )
    ap.add_argument("--model", default="bm25")
    ap.add_argument("--path")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--deps")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--label-style", choices=["descriptive", "short"], default="descriptive")
    a = ap.parse_args()
    global LABELS
    if a.label_style == "short":
        LABELS = [
            "residence",
            "workplace",
            "completed visit",
            "planned relocation",
            "gift",
            "reading",
        ]
    if a.deps:
        sys.path.insert(0, a.deps)
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    os.environ["OMP_NUM_THREADS"] = str(a.threads)
    import numpy as np
    import psutil

    proc = psutil.Process()
    peak = [proc.memory_info().rss]
    done = threading.Event()

    def sample():
        while not done.wait(0.01):
            peak[0] = max(peak[0], proc.memory_info().rss)

    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    result = {
        "model": a.model,
        "kind": a.kind,
        "threads": a.threads,
        "label_style": a.label_style,
        "python": sys.version,
        "fixture_sha256": hashlib.sha256(
            json.dumps([LABELS, TEXTS, NER, RERANK]).encode()
        ).hexdigest(),
        "limitations": [
            "Tiny synthetic diagnostic, not standard benchmark or held-out quality estimate.",
            "No generative model; CPU only; each model in a separate process.",
            "Latency includes tokenization/model/scoring for the indicated kernel; warmed weights.",
            "Memory is sampled process RSS, not whole-system memory or guaranteed OS peak.",
        ],
    }

    def probe(fn, items, tag):
        for item in items[:2]:
            fn(item)
        times = []
        outputs = []
        for repeat in range(a.repeats):
            for item in items:
                t = time.perf_counter()
                value = fn(item)
                times.append((time.perf_counter() - t) * 1000)
                if repeat == 0:
                    outputs.append(value)
        result[tag] = {
            "n_items": len(items),
            "repeats": a.repeats,
            "warm_p50_ms": statistics.median(times),
            "warm_p95_ms": float(np.percentile(times, 95)),
            "outputs": outputs,
        }
        return outputs

    try:
        t = time.perf_counter()
        if a.kind == "bm25":
            import re

            tok = lambda s: re.findall(r"\w+", s.lower())
            docs = [Counter(tok(x)) for x in LABELS]
            avg = sum(sum(d.values()) for d in docs) / len(docs)

            def classify(text):
                scores = []
                for d in docs:
                    score = 0
                    for word in set(tok(text)):
                        tf = d[word]
                        df = sum(word in x for x in docs)
                        if tf:
                            score += (
                                math.log(1 + (len(docs) - df + 0.5) / (df + 0.5))
                                * tf
                                * 2.2
                                / (tf + 1.2 * (0.25 + 0.75 * sum(d.values()) / avg))
                            )
                    scores.append(score)
                return int(np.argmax(scores))
        elif a.kind == "static":
            from model2vec import StaticModel

            m = StaticModel.from_pretrained(a.path)
            label_vectors = m.encode(LABELS, use_multiprocessing=False)
            label_vectors = label_vectors / np.maximum(
                np.linalg.norm(label_vectors, axis=1, keepdims=True), 1e-12
            )

            def classify(text):
                v = m.encode([text], use_multiprocessing=False)[0]
                v = v / max(float(np.linalg.norm(v)), 1e-12)
                return int(np.argmax(label_vectors @ v))
        else:
            import torch

            torch.set_num_threads(a.threads)
            torch.set_num_interop_threads(1)
            if a.kind in ("encoder", "reranker"):
                from transformers import (
                    AutoModel,
                    AutoModelForSequenceClassification,
                    AutoTokenizer,
                )

                tokenizer = AutoTokenizer.from_pretrained(a.path, local_files_only=True)
                m = (
                    (AutoModel if a.kind == "encoder" else AutoModelForSequenceClassification)
                    .from_pretrained(a.path, local_files_only=True)
                    .to("cpu")
                    .eval()
                )
                result["parameter_count"] = sum(p.numel() for p in m.parameters())

                def encode(texts):
                    x = tokenizer(
                        texts, padding=True, truncation=True, max_length=256, return_tensors="pt"
                    )
                    with torch.inference_mode():
                        y = m(**x).last_hidden_state
                        if "bge" in a.model:
                            y = y[:, 0]
                        else:
                            y = (y * x["attention_mask"].unsqueeze(-1)).sum(1) / x[
                                "attention_mask"
                            ].sum(1, keepdim=True)
                        return torch.nn.functional.normalize(y, p=2, dim=1).numpy()

                if a.kind == "encoder":
                    label_vectors = encode(LABELS)

                    def classify(text):
                        prefix = (
                            "Represent this sentence for searching relevant passages: "
                            if "bge" in a.model
                            else ""
                        )
                        return int(np.argmax(label_vectors @ encode([prefix + text])[0]))
                else:

                    def rank(case):
                        query, docs, _ = case
                        x = tokenizer(
                            [query] * len(docs),
                            docs,
                            padding=True,
                            truncation=True,
                            max_length=256,
                            return_tensors="pt",
                        )
                        with torch.inference_mode():
                            scores = m(**x).logits.reshape(-1).tolist()
                        return {"best": int(np.argmax(scores)), "scores": scores}
            elif a.kind == "gliner":
                from gliner2 import AutoExtractor

                m = AutoExtractor.from_pretrained(a.path, map_location="cpu")
                m.eval()
                result["parameter_count"] = sum(p.numel() for p in m.parameters())

                def classify(text):
                    with torch.inference_mode():
                        out = m.classify_text(text, {"route": LABELS}, include_confidence=True)
                    value = out.get("route", out)
                    if isinstance(value, dict):
                        value = value.get("label", value.get("text"))
                    if isinstance(value, list):
                        value = value[0].get("label") if isinstance(value[0], dict) else value[0]
                    if value not in LABELS:
                        raise ValueError("Unrecognised classifier output " + str(out)[:400])
                    return LABELS.index(value)

                def ner(case):
                    text, _ = case
                    with torch.inference_mode():
                        out = m.extract_entities(
                            text,
                            ["person", "location", "organization"],
                            include_spans=True,
                            include_confidence=True,
                        )
                    values = []
                    for kind, entities in out.get("entities", {}).items():
                        for e in entities:
                            if not isinstance(e, dict):
                                raise ValueError("NER returned no span")
                            start, end = e.get("start"), e.get("end")
                            surface = e.get("text")
                            if (
                                not isinstance(start, int)
                                or not isinstance(end, int)
                                or text[start:end] != surface
                            ):
                                raise ValueError("Invalid raw span")
                            values.append([surface, kind, start, end])
                    return values
            elif a.kind == "gliclass":
                from gliclass import GLiClassModel, ZeroShotClassificationPipeline
                from transformers import AutoTokenizer

                m = GLiClassModel.from_pretrained(a.path, local_files_only=True).to("cpu").eval()
                tokenizer = AutoTokenizer.from_pretrained(a.path, local_files_only=True)
                pipe = ZeroShotClassificationPipeline(
                    m, tokenizer, classification_type="single-label", device="cpu"
                )
                result["parameter_count"] = sum(p.numel() for p in m.parameters())

                def classify(text):
                    with torch.inference_mode():
                        out = pipe(text, LABELS, threshold=0.0)[0]
                    best = max(out, key=lambda x: x["score"])
                    return LABELS.index(best["label"])

        result["load_and_label_setup_ms"] = (time.perf_counter() - t) * 1000
        if "m" in locals() and hasattr(m, "parameters"):
            result["parameter_dtypes"] = dict(Counter(str(p.dtype) for p in m.parameters()))
        import importlib.metadata

        result["package_versions"] = {}
        for package in (
            "numpy",
            "torch",
            "transformers",
            "model2vec",
            "gliner2",
            "gliclass",
            "scikit-learn",
        ):
            try:
                result["package_versions"][package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                pass
        if a.kind == "reranker":
            outputs = probe(rank, RERANK, "rerank")
            result["rerank"]["top1_correct"] = sum(
                x["best"] == c[2] for x, c in zip(outputs, RERANK)
            )
        else:
            outputs = probe(classify, [x[0] for x in TEXTS], "routing")
            result["routing"]["top1_correct"] = sum(x == c[1] for x, c in zip(outputs, TEXTS))
            if a.kind in ("static", "encoder"):
                # Mirrors a precomputed document index, not a query-document cross-encoder.
                def embed(texts):
                    if a.kind == "encoder":
                        return encode(texts)
                    v = m.encode(texts, use_multiprocessing=False)
                    return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-12)

                started = time.perf_counter()
                doc_vectors = {c[0]: embed(c[1]) for c in RERANK}
                result["passage_vector_setup_ms"] = (time.perf_counter() - started) * 1000

                def similarity(case):
                    query = case[0]
                    prefix = (
                        "Represent this sentence for searching relevant passages: "
                        if "bge" in a.model
                        else ""
                    )
                    scores = doc_vectors[query] @ embed([prefix + query])[0]
                    if not np.isfinite(scores).all():
                        raise ValueError("Nonfinite similarity scores")
                    return {"best": int(np.argmax(scores)), "scores": scores.tolist()}

                outputs = probe(similarity, RERANK, "cached_similarity")
                result["cached_similarity"]["top1_correct"] = sum(
                    x["best"] == c[2] for x, c in zip(outputs, RERANK)
                )
            if a.kind == "gliner":
                outputs = probe(ner, NER, "ner")
                tp = fp = fn = 0
                for out, (_, expected) in zip(outputs, NER):
                    got = {(x[0], x[1]) for x in out}
                    gold = set(expected)
                    tp += len(got & gold)
                    fp += len(got - gold)
                    fn += len(gold - got)
                result["ner"].update(
                    tp=tp, fp=fp, fn=fn, micro_f1=2 * tp / (2 * tp + fp + fn) if tp + fp + fn else 0
                )
        result["status"] = "ok"
    except Exception as e:
        import traceback

        result.update(
            status="failed",
            error_type=type(e).__name__,
            error=str(e)[:1500],
            traceback=traceback.format_exc()[-3000:],
        )
    finally:
        done.set()
        thread.join()
        result["sampled_peak_rss_mb"] = peak[0] / 1024**2
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    k: v
                    for k, v in result.items()
                    if k not in ("routing", "ner", "rerank", "traceback")
                },
                indent=2,
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
