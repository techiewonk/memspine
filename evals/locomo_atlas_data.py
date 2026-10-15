"""ATLAS-TURNS: pure data builders for the LoCoMo atlas conversation viewer and full per-question detail.

No I/O beyond what the caller hands in, no model call, no network. The output carries dataset text, so the caller may only
write it under the gitignored ``evals/runs/_analysis/`` (see ``build_locomo_problem_atlas.assert_ignored``).

* ``build_conversations``  every session and every turn of every conversation (speaker, text, ``D<s>:<n>``, image caption).
* ``build_context``        the turns the reader saw per question, from the forensics ``context_records`` (fallback: the
                           ``retrieved_ids`` of results.jsonl), plus the per-turn rendered text (date resolution, image tag).
* ``enrich_rows``          adds the evidence ids, the judge verdict/raw reply, the reader context ids and the speaker/date of
                           every gold turn to the problem-table rows.
* ``screen_flips``         questions whose score differs between a reference run and a sample-screen run, with both answers.
"""

from __future__ import annotations

import collections
import re
from collections.abc import Iterable

TURN_RE = re.compile(r"D\d+:\d+")
SESSION_KEY = re.compile(r"session_(\d+)")


def parse_evidence(ev: Iterable[str] | str | None) -> list[str]:
    """Turn ids from the messy ``evidence`` field ('D1:3', 'D8:6; D9:17', ' D1:3 ')."""
    if ev is None:
        return []
    parts = [ev] if isinstance(ev, str) else list(ev)
    out: list[str] = []
    for p in parts:
        for m in TURN_RE.findall(str(p)):
            if m not in out:
                out.append(m)
    return out


def build_conversations(raw: list[dict]) -> dict[str, dict]:
    """``{item: {speakers, sessions: [{s, date, turns: [{id, sp, t, cap?, iq?}]}]}}`` for every conversation in locomo10.json."""
    out: dict[str, dict] = {}
    for c in raw:
        conv = c["conversation"]
        sessions = []
        for k, v in conv.items():
            m = SESSION_KEY.fullmatch(k)
            if not (m and isinstance(v, list)):
                continue
            n = int(m.group(1))
            turns = []
            for t in v:
                row = {"id": t["dia_id"], "sp": t.get("speaker", ""), "t": t.get("text", "")}
                if t.get("blip_caption"):
                    row["cap"] = t["blip_caption"]
                if t.get("query"):
                    row["iq"] = t["query"]
                turns.append(row)
            sessions.append(
                {"s": n, "date": conv.get(f"session_{n}_date_time", ""), "turns": turns}
            )
        sessions.sort(key=lambda s: s["s"])
        out[c["sample_id"]] = {
            "speakers": [conv.get("speaker_a", ""), conv.get("speaker_b", "")],
            "sessions": sessions,
            "ctxtext": {},
        }
    return out


def turn_index(convs: dict[str, dict]) -> dict[tuple[str, str], dict]:
    """``(item, turn id) -> {sp, t, s, date}``."""
    idx: dict[tuple[str, str], dict] = {}
    for item, c in convs.items():
        for s in c["sessions"]:
            for t in s["turns"]:
                idx[(item, t["id"])] = {"sp": t["sp"], "t": t["t"], "s": s["s"], "date": s["date"]}
    return idx


def qa_index(raw: list[dict]) -> dict[tuple[str, str], dict]:
    """``(item, 'i-j') -> qa`` where ``i`` is the conversation index and ``j`` the position in its ``qa`` list."""
    out = {}
    for i, c in enumerate(raw):
        for j, qa in enumerate(c["qa"]):
            out[(c["sample_id"], f"{i}-{j}")] = qa
    return out


def build_context(
    forensics: Iterable[dict], results: dict[tuple[str, str], dict], convs: dict[str, dict]
) -> dict[tuple[str, str], dict]:
    """Per question the ordered ids of the turns in the reader context, with the source used.

    ``forensics`` rows carry ``context_records`` (``{turn, text}`` in rendered order). The rendered text differs from the raw
    turn (date resolution suffix, ``[image: ...]`` tag), so per conversation the most common rendered text per turn is kept in
    ``convs[item]['ctxtext']`` when it differs from ``Speaker: text``. A question whose forensics row is missing falls back to
    the ``retrieved_ids`` of its result row (source ``results.retrieved_ids``: ids only, no rendered text).
    """
    ids: dict[tuple[str, str], dict] = {}
    seen: dict[tuple[str, str], collections.Counter] = collections.defaultdict(collections.Counter)
    for f in forensics:
        k = (f.get("item"), f.get("query_id"))
        recs = f.get("context_records") or []
        ids[k] = {"ids": [r["turn"] for r in recs], "source": "forensics.context_records"}
        for r in recs:
            seen[(k[0], r["turn"])][r.get("text") or ""] += 1
    for k, r in results.items():
        if k not in ids and r.get("retrieved_ids"):
            ids[k] = {"ids": list(r["retrieved_ids"]), "source": "results.retrieved_ids"}
    idx = turn_index(convs)
    for (item, tid), cnt in seen.items():
        raw = idx.get((item, tid))
        text = cnt.most_common(1)[0][0]
        if raw is not None and text != f"{raw['sp']}: {raw['t']}" and item in convs:
            convs[item]["ctxtext"][tid] = text
    return ids


def judge_fields(res: dict | None) -> dict:
    """Judge verdict as logged: the raw judge reply (it holds the label, and the reason when the judge wrote one)."""
    if not res:
        return {"judge_raw": None, "judge_prompt": None, "score": None}
    meta = res.get("meta") or {}
    return {
        "judge_raw": meta.get("judge_raw"),
        "judge_prompt": meta.get("judge_prompt_id"),
        "score": res.get("score"),
    }


def enrich_rows(
    rows: list[dict],
    convs: dict[str, dict],
    qa: dict[tuple[str, str], dict],
    results: dict,
    ctx: dict,
) -> dict:
    """Add evidence, judge, reader context and gold-turn speaker/date to the table rows (in place). Returns coverage counts."""
    idx = turn_index(convs)
    stats = collections.Counter()
    for r in rows:
        k = (r["item"], r["qid"])
        evidence = parse_evidence((qa.get(k) or {}).get("evidence"))
        have = {g["turn"] for g in r["gold_turns"]}
        for tid in evidence:
            if (
                tid not in have
            ):  # evidence the forensics gold list does not carry: shown with no ranks
                r["gold_turns"].append(
                    {
                        "turn": tid,
                        "ts": None,
                        "in_context": False,
                        "lost_at": None,
                        "vector": None,
                        "lexical": None,
                        "extra": {},
                        "fused": None,
                        "pool": None,
                        "rerank": None,
                        "final": None,
                        "text": None,
                    }
                )
                stats["gold_added_from_dataset"] += 1
        c = ctx.get(k)
        r["ctx"] = c["ids"] if c else []
        r["ctx_source"] = c["source"] if c else None
        ctx_set = set(r["ctx"])
        for g in r["gold_turns"]:
            t = idx.get((r["item"], g["turn"]))
            g["speaker"] = t["sp"] if t else None
            g["date"] = t["date"] if t else None
            g["session"] = t["s"] if t else None
            if t and not g.get("text"):
                g["text"] = f"{t['sp']}: {t['t']}"
            if c:
                g["in_context"] = g["turn"] in ctx_set
        r["evidence"] = evidence or [g["turn"] for g in r["gold_turns"]]
        r["n_ctx"] = len(r["ctx"])
        r.update(judge_fields(results.get(k)))
        r["screen_flips"] = []
        stats["rows"] += 1
        stats["no_context"] += c is None
        stats["no_judge_raw"] += r["judge_raw"] is None
    return dict(stats)


def screen_flips(
    ref: dict[tuple[str, str], tuple[float, str]], run: dict[tuple[str, str], tuple[float, str]]
) -> list[dict]:
    """Questions whose score differs between ``ref`` and ``run`` (each ``{key: (score, answer)}``)."""
    out = []
    for k, (s, a) in run.items():
        if k not in ref:
            continue
        rs, ra = ref[k]
        if s != rs:
            out.append(
                {
                    "item": k[0],
                    "qid": k[1],
                    "ref_score": rs,
                    "ref_answer": ra,
                    "score": s,
                    "answer": a,
                    "kind": "gain" if s > rs else "loss",
                }
            )
    return out


def attach_flips(rows: list[dict], run_info: dict, flips: list[dict]) -> None:
    """Attach one screen run's flips to the rows. ``run_info`` = {screen, run, ref, mode, n}."""
    by = {(r["item"], r["qid"]): r for r in rows}
    for f in flips:
        r = by.get((f["item"], f["qid"]))
        if r is not None:
            r["screen_flips"].append(
                {
                    **{k: run_info[k] for k in ("screen", "run", "ref", "mode")},
                    **{k: f[k] for k in ("ref_score", "ref_answer", "score", "answer", "kind")},
                }
            )
