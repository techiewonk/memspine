"""CPU-only ranking/packing experiment; synthetic messages, never an engine run."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import os
import random
import statistics
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from cpu_non_llm_kernels import RERANK

ROOT = Path(__file__).resolve().parents[2]
MODELS = [
    ("bm25", "bm25"),
    ("static", "minishlab/potion-base-8M"),
    ("static", "minishlab/potion-retrieval-32M"),
    ("encoder", "sentence-transformers/all-MiniLM-L6-v2"),
    ("cross", "cross-encoder/ms-marco-TinyBERT-L2-v2"),
    ("cross", "cross-encoder/ms-marco-MiniLM-L-6-v2"),
]
REVISIONS = {
    "minishlab/potion-base-8M": "bf8b056651a2c21b8d2565580b8569da283cab23",
    "minishlab/potion-retrieval-32M": "6fc8051fab2a1e0ee76689cf08c853792ac285e7",
    "sentence-transformers/all-MiniLM-L6-v2": "1110a243fdf4706b3f48f1d95db1a4f5529b4d41",
    "cross-encoder/ms-marco-TinyBERT-L2-v2": "81d1926f67cb8eee2c2be17ca9f793c7c3bd20cc",
    "cross-encoder/ms-marco-MiniLM-L-6-v2": "c5ee24cb16019beea0893ab7796b1df96625c6b8",
}


def snapshot(name):
    return (
        Path.home()
        / ".cache/huggingface/hub"
        / ("models--" + name.replace("/", "--"))
        / "snapshots"
        / REVISIONS[name]
    )


def fixtures(tokenizer):
    base = [(q, docs, [answer], {}) for q, docs, answer in RERANK]
    base += [
        (
            "Which country did Sofia move from?",
            [
                "Sofia moved from her home country to Lisbon.",
                "Sofia identifies her home country as Sweden.",
                "Sofia visited Norway.",
                "Anita moved from Italy.",
            ],
            [0, 1],
            {0: [1]},
        ),
        (
            "When did Maya attend the festival?",
            [
                "Maya: I attended the festival last year.",
                "This Maya message was created on 2026-06-12 at 10:00 UTC.",
                "Maya plans to attend another festival in 2027.",
                "Oliver attended a festival in 2023.",
            ],
            [0, 1],
            {0: [1]},
        ),
        (
            "Did David attend the workshop?",
            [
                "David received an invitation to the workshop.",
                "David replied: I did not attend the workshop.",
                "Elena attended the workshop.",
                "David plans to attend next year.",
            ],
            [0, 1],
            {0: [1], 1: [0]},
        ),
        (
            "Where does Anita currently live after her correction?",
            [
                "Anita: I live in Porto.",
                "Anita correction to my earlier residence statement: I meant Lisbon, not Porto.",
                "The correction was posted by Anita on 2026-10-01 at 10:00 UTC.",
                "Anita works in Rome.",
            ],
            [0, 1, 2],
            {0: [1, 2], 1: [0, 2]},
        ),
        (
            "How long did Jon take to open his studio?",
            [
                "Jon began preparing his studio on 2024-01-01 at 10:00 UTC.",
                "Jon opened that studio on 2025-01-01 at 10:00 UTC.",
                "Jon planned a different studio in 2020.",
                "Anita opened a studio in March.",
            ],
            [0, 1],
            {0: [1], 1: [0]},
        ),
        (
            "Which city have both Nora and Leo visited?",
            [
                "Nora visited Vienna during a holiday.",
                "Leo visited Vienna during a work trip.",
                "Nora plans to visit Rome.",
                "Leo lives in Paris.",
            ],
            [0, 1],
            {0: [1], 1: [0]},
        ),
        (
            "How many distinct recorded hikes did Priya complete?",
            [
                "Priya completed hike event H1 in May.",
                "Priya completed hike event H2 in June.",
                "Priya completed hike event H3 in July.",
                "Priya retold completed hike event H1; this is not a new hike.",
            ],
            [0, 1, 2, 3],
            {0: [1, 2, 3], 1: [0, 2, 3], 2: [0, 1, 3]},
        ),
        (
            "Who gave Maya the red guitar?",
            [
                "Maya: My brother gave me the red guitar.",
                "Maya: Oliver is my brother.",
                "Anita gave Maya a blue guitar.",
                "Maya gave David a red watch.",
            ],
            [0, 1],
            {0: [1]},
        ),
    ]
    filler = (
        "After finishing the usual tasks, we talked about the afternoon and checked the notes. "
        "The weather stayed mild and the room was quiet. There was time for a break before dinner. "
    ) * 40
    filler_ids = tokenizer.encode(filler, add_special_tokens=False)
    distractors = [
        "Sofia plans a visit to Lisbon but has not moved there.",
        "Anita discussed a workplace in York without accepting a position.",
        "Maya asked whether a guitar could be bought as a gift.",
        "David offers workshops but this note does not report attendance.",
        "Nora considered Rome as a holiday option rather than a residence.",
        "Leo planned a long walk; its completion and distance were not recorded.",
        "The studio opening invitation gives a date for another owner.",
        "Priya mentioned the same hike again, without identifying a new event.",
    ]
    result = []
    for length in (64, 128, 256):
        for case_id, (query, cores, required, links) in enumerate(base):
            cores = list(cores) + [
                distractors[i % len(distractors)]
                + f" This note belongs to separate record {i + 10}."
                for i in range(44)
            ]
            docs = []
            for i, core in enumerate(cores):
                core_n = len(tokenizer.encode(core, add_special_tokens=False))
                padding = tokenizer.decode(
                    filler_ids[: max(0, length - core_n)], skip_special_tokens=True
                )
                text = core + " " + padding if (i + case_id) % 2 else padding + " " + core
                start = text.index(core)
                docs.append(
                    {
                        "id": f"m{i:02}",
                        "text": text,
                        "core": core,
                        "span": [start, start + len(core)],
                        "dependencies": [f"m{j:02}" for j in links.get(i, [])],
                    }
                )
            random.Random(8100 + case_id).shuffle(docs)
            result.append(
                {
                    "case": case_id,
                    "length": length,
                    "query": query,
                    "documents": docs,
                    "required": [f"m{i:02}" for i in required],
                    "multi": case_id >= 8,
                }
            )
    return result


def load_fit():
    # Run the actual pure loop in isolation; do not import/boot engine services.
    p = ROOT / "src/memspine/read/tiers.py"
    tree = ast.parse(p.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "fit_units")
    code = ast.Module(
        body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            node,
        ],
        type_ignores=[],
    )
    namespace = {"unit_tokens": lambda b, counter=None: b.tokens}
    exec(compile(ast.fix_missing_locations(code), str(p), "exec"), namespace)
    return namespace["fit_units"], hashlib.sha256(
        ast.get_source_segment(p.read_text(encoding="utf-8"), node).encode()
    ).hexdigest()


def pack(case, ranked_ids, budget, mode, tokenizer, fit):
    docs = {d["id"]: d for d in case["documents"]}

    def render(ids):
        return "\n".join(
            f"[source={i};session=S1;created_at=2026-10-01T10:00:00Z]\n"
            + (docs[i]["core"] if mode == "linked_annotated_spans" else docs[i]["text"])
            for i in sorted(ids)
        )

    def cost(ids):
        return len(tokenizer.encode(render(ids), add_special_tokens=False))

    units = []
    for key in ranked_ids[:12]:
        ids = {key}
        if mode != "flat_full":
            queue = [key]
            while queue:
                for parent in docs[queue.pop()]["dependencies"]:
                    if parent not in ids:
                        ids.add(parent)
                        queue.append(parent)
        # Independent bundles repeat copied excerpts: no free cross-bundle dedup assumed.
        units.append(SimpleNamespace(record_id=key, tokens=cost(ids), ids=ids))
    kept, _ = fit(units, budget)
    used = sum(x.tokens for x in kept)
    assert used <= budget
    selected = set().union(*(u.ids for u in kept)) if kept else set()
    gold = set(case["required"])
    partial = 0
    for i in selected:
        if any(parent not in selected for parent in docs[i]["dependencies"]):
            partial += 1
    return {
        "used_tokens": used,
        "kept_bundles": len(kept),
        "retained": len(selected & gold),
        "required": len(gold),
        "complete": gold <= selected,
        "partial_dependency_records": partial,
        "selected_ids": sorted(selected),
    }


def stats(values):
    values = sorted(values)
    return {
        "p50_ms": statistics.median(values),
        "p95_ms": values[min(len(values) - 1, math.ceil(0.95 * len(values)) - 1)],
    }


def worker(a):
    sys.path.insert(0, a.deps)
    import numpy as np
    import psutil
    import torch
    from transformers import AutoModel, AutoModelForSequenceClassification, AutoTokenizer

    torch.set_num_threads(4)
    torch.set_num_interop_threads(1)
    process = psutil.Process()
    peak = [process.memory_info().rss]
    done = threading.Event()

    def sample():
        while not done.wait(0.01):
            peak[0] = max(peak[0], process.memory_info().rss)

    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    counter = AutoTokenizer.from_pretrained(
        snapshot("cross-encoder/ms-marco-TinyBERT-L2-v2"), local_files_only=True
    )
    cases = fixtures(counter)
    fit, fit_hash = load_fit()
    result = {
        "model": a.worker,
        "kind": a.kind,
        "fixture_sha256": hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest(),
        "revision": REVISIONS.get(a.worker),
        "fit_units_source_sha256": fit_hash,
        "threads": 4,
        "counter": "TinyBERT BERT tokenizer, pinned snapshot; exact rendered evidence tokens, not a reader-independent budget",
        "rows": [],
        "raw_pair_max_tokens": 0,
        "truncated_pairs": 0,
    }
    started = time.perf_counter()
    if a.kind == "static":
        from model2vec import StaticModel

        model = StaticModel.from_pretrained(snapshot(a.worker))

        def encode(texts):
            v = model.encode(texts, use_multiprocessing=False)
            return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-12)
    elif a.kind in ("encoder", "cross"):
        tokenizer = AutoTokenizer.from_pretrained(snapshot(a.worker), local_files_only=True)
        model = (
            (AutoModel if a.kind == "encoder" else AutoModelForSequenceClassification)
            .from_pretrained(snapshot(a.worker), local_files_only=True)
            .eval()
            .to("cpu")
        )
        result["parameters"] = sum(p.numel() for p in model.parameters())

        def encode(texts):
            chunks = []
            for offset in range(0, len(texts), 16):
                x = tokenizer(
                    texts[offset : offset + 16],
                    padding=True,
                    truncation=True,
                    max_length=512,
                    return_tensors="pt",
                )
                with torch.inference_mode():
                    y = model(**x).last_hidden_state
                    v = (y * x["attention_mask"].unsqueeze(-1)).sum(1) / x["attention_mask"].sum(
                        1, keepdim=True
                    )
                    chunks.append(torch.nn.functional.normalize(v, p=2, dim=1).numpy())
            return np.concatenate(chunks)

    result["model_setup_ms"] = (time.perf_counter() - started) * 1000
    import re

    tokenize = lambda x: re.findall(r"\w+", x.lower())
    for case in cases:
        docs = case["documents"]
        texts = [x["text"] for x in docs]
        query = case["query"]
        indexing = time.perf_counter()
        if a.kind in ("static", "encoder"):
            vectors = encode(texts)
        elif a.kind == "bm25":
            counts = [Counter(tokenize(t)) for t in texts]
            lengths = [sum(d.values()) for d in counts]
            avg = sum(lengths) / 48
            idf = {
                w: math.log(
                    1
                    + (48 - sum(w in d for d in counts) + 0.5) / (sum(w in d for d in counts) + 0.5)
                )
                for w in set(tokenize(query))
            }
        indexing_ms = (time.perf_counter() - indexing) * 1000
        if a.kind == "cross":
            pair_lengths = [
                len(tokenizer.encode(query, t, add_special_tokens=True, truncation=False))
                for t in texts
            ]
            result["raw_pair_max_tokens"] = max(result["raw_pair_max_tokens"], max(pair_lengths))
            result["truncated_pairs"] += sum(n > 512 for n in pair_lengths)

        # rank() is defined and called within this iteration only, so the loop
        # variables it reads are never stale (B023 false positive).
        def rank():
            if a.kind in ("static", "encoder"):
                scores = vectors @ encode([query])[0]
            elif a.kind == "bm25":
                scores = np.array(
                    [
                        sum(
                            idf[w] * d[w] * 2.2 / (d[w] + 1.2 * (0.25 + 0.75 * n / avg))
                            for w in idf
                            if d[w]
                        )
                        for d, n in zip(counts, lengths)
                    ]
                )
            else:
                pieces = []
                for start in range(0, 48, 16):
                    chunk = texts[start : start + 16]
                    x = tokenizer(
                        [query] * len(chunk),
                        chunk,
                        padding=True,
                        truncation=True,
                        max_length=512,
                        return_tensors="pt",
                    )
                    with torch.inference_mode():
                        pieces.extend(model(**x).logits.reshape(-1).tolist())
                scores = np.array(pieces)
            assert np.isfinite(scores).all()
            return [
                docs[i]["id"]
                for i in sorted(range(48), key=lambda j: (-float(scores[j]), docs[j]["id"]))
            ]

        rank()
        timings = []
        for _ in range(3):
            t = time.perf_counter()
            ranking = rank()
            timings.append((time.perf_counter() - t) * 1000)
        gold = set(case["required"])
        ranked_set = set(ranking[:12])
        row = {
            "case": case["case"],
            "length": case["length"],
            "multi": case["multi"],
            "query": query,
            "rank_latency": stats(timings),
            "rank_samples_ms": timings,
            "index_setup_ms": indexing_ms,
            "doc_token_min": min(len(counter.encode(t, add_special_tokens=False)) for t in texts),
            "doc_token_max": max(len(counter.encode(t, add_special_tokens=False)) for t in texts),
            "first_relevant_reciprocal_rank": 1
            / (next(i for i, d in enumerate(ranking) if d in gold) + 1),
            "all_required_top12": gold <= ranked_set,
            "relevant_top1": ranking[0] in gold,
            "required": sorted(gold),
            "ranking": ranking,
            "packs": [],
        }
        for budget in (2048, 3072):
            for mode in ("flat_full", "linked_full", "linked_annotated_spans"):
                # Timer is over 30 replayed packing calls, excluding model ranking but including counting.
                samples = []
                for _ in range(30):
                    t = time.perf_counter()
                    packed = pack(case, ranking, budget, mode, counter, fit)
                    samples.append((time.perf_counter() - t) * 1000)
                row["packs"].append({"budget": budget, "mode": mode, **stats(samples), **packed})
        result["rows"].append(row)
        a.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(a.worker, case["length"], case["case"], "done", flush=True)
    done.set()
    thread.join()
    result["sampled_peak_rss_mib"] = peak[0] / 1024**2
    result["status"] = "ok"
    result["python"] = sys.version
    import importlib.metadata

    result["packages"] = {
        p: importlib.metadata.version(p) for p in ("numpy", "torch", "transformers", "model2vec")
    }
    a.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--deps", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--worker")
    ap.add_argument("--kind")
    a = ap.parse_args()
    if a.worker:
        worker(a)
        return
    a.out.mkdir(parents=True, exist_ok=True)
    env = dict(
        os.environ,
        CUDA_VISIBLE_DEVICES="",
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        TOKENIZERS_PARALLELISM="false",
        OMP_NUM_THREADS="4",
        MKL_NUM_THREADS="4",
        OPENBLAS_NUM_THREADS="4",
    )
    for kind, name in MODELS:
        output = a.out / (name.replace("/", "--") + ".json")
        with output.with_suffix(".log").open("w", encoding="utf-8") as log:
            print("START", name, flush=True)
            subprocess.run(
                [
                    a.python,
                    str(Path(__file__).resolve()),
                    "--deps",
                    a.deps,
                    "--out",
                    str(output),
                    "--worker",
                    name,
                    "--kind",
                    kind,
                ],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=900,
                check=True,
            )
        print("FINISHED", name, flush=True)


if __name__ == "__main__":
    main()
