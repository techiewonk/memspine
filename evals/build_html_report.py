"""HTML views of the forensic JSON: one page per run (every step separately) plus an index over all runs.

    python evals/build_html_report.py [--rebuild] [--runs-dir evals/runs] [--out evals/reports]

``--rebuild`` first (re)generates ``<run>--memspine/report/`` (per_question.jsonl + run_summary.json,
schema-validated) for every run that has a results.jsonl, attaching ``runs/<run_id>--forensics/`` when that
stage-log directory exists. Then it writes ``<out>/<run_id>.html`` (self-contained: data embedded, no network)
and ``<out>/index.html``.

Per-run page: Overview (models, accuracy by category, outcome classes, primary gaps, retrieval funnel, oracle
ceilings, cascades, reader failure types, injection audit, fix hypotheses) and Questions (filter by category /
verdict / outcome / gap / text; a question opens its step-by-step trace: 1 question and verdict, 2 injection of
each gold turn, 3 retrieval legs, 4 fusion, 5 reranker pool and scores, 6 final list, 7 context the reader saw,
8 answer and judge, 9 gap attribution and flags).
"""

from __future__ import annotations

import argparse
import html
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
DATA = HERE.parent / "data" / "locomo10.json"

CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--mut:#6b6a66;--line:#e2e0da;--card:#fff;--ok:#2f7d4f;--bad:#b4402a;--acc:#2f5fa7;--hl:#fff3c4}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe7;--mut:#a3a19b;--line:#33322f;--card:#1f1f1d;--ok:#6cc391;--bad:#ef8a72;--acc:#8fb3ec;--hl:#4a3f12}}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--fg)}
header{padding:16px;border-bottom:1px solid var(--line)}h1{font-size:19px;margin:0 0 4px}h2{font-size:16px;margin:18px 0 8px}
h3{font-size:14px;margin:14px 0 6px}main{padding:0 16px 40px;max-width:1400px;margin:auto}.mut{color:var(--mut)}
table{border-collapse:collapse;width:100%;margin:6px 0;font-variant-numeric:tabular-nums}th,td{border-bottom:1px solid var(--line);padding:4px 6px;text-align:left;vertical-align:top}
th{font-weight:600;color:var(--mut)}.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;margin:10px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px}.tabs button{margin-right:6px}
button,select,input{font:inherit;padding:4px 8px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg)}
button.on{border-color:var(--acc);color:var(--acc)}.ok{color:var(--ok)}.bad{color:var(--bad)}.gold{background:var(--hl)}
.q{cursor:pointer}.q:hover{background:var(--hl)}.pill{display:inline-block;border:1px solid var(--line);border-radius:10px;padding:0 6px;margin:1px;font-size:12px}
.split{display:grid;grid-template-columns:minmax(300px,40%) 1fr;gap:12px}@media(max-width:800px){.split{grid-template-columns:1fr}}
.scroll{max-height:75vh;overflow:auto}pre{white-space:pre-wrap;margin:0;font:12px/1.4 ui-monospace,Consolas,monospace}a{color:var(--acc)}
"""

PAGE_JS = r"""
const S=DATA.summary, R=DATA.rows; const pc=x=>x==null?'n/a':(100*x).toFixed(1)+'%'; const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
function kv(o){return '<table>'+Object.entries(o||{}).map(([k,v])=>`<tr><th>${esc(k)}</th><td>${esc(typeof v=='object'?JSON.stringify(v):v)}</td></tr>`).join('')+'</table>'}
function overview(){let h=`<div class=grid><div class=card><h3>Run</h3>${kv({run:S.run_id,mode:S.mode,stage_log:S.has_stage_log,questions:S.n_questions,accuracy:pc(S.accuracy),ctx_tokens_mean:S.context_tokens_mean&&S.context_tokens_mean.toFixed(0),answer_p50_ms:S.latency_answer_p50_ms,answer_p95_ms:S.latency_answer_p95_ms})}</div>
<div class=card><h3>Models</h3>${kv(S.models)}</div><div class=card><h3>Accuracy by category</h3><table><tr><th>category</th><th>n</th><th>accuracy</th></tr>${Object.entries(S.by_category).map(([c,v])=>`<tr><td>${c}</td><td>${v.n}</td><td>${pc(v.accuracy)}</td></tr>`).join('')}</table></div>
<div class=card><h3>Outcome classes</h3>${kv(S.outcome_classes)}</div><div class=card><h3>Primary gap (wrong questions)</h3><table><tr><th>gap</th>${Object.keys(S.primary_gaps_by_category).map(c=>`<th>${c}</th>`).join('')}<th>all</th></tr>${Object.keys(S.primary_gaps).map(g=>`<tr><td>${g}</td>${Object.values(S.primary_gaps_by_category).map(v=>`<td>${v[g]}</td>`).join('')}<td><b>${S.primary_gaps[g]}</b></td></tr>`).join('')}</table></div>`;
if(S.funnel){h+=`<div class=card><h3>Retrieval funnel (share of questions whose ALL gold survives)</h3><table><tr><th>category</th><th>n</th><th>any leg</th><th>fused top-k</th><th>pool</th><th>final</th><th>in context</th><th>correct</th></tr>${Object.entries(S.funnel).map(([c,v])=>`<tr><td>${c}</td><td>${v.n}</td><td>${pc(v.any_leg)}</td><td>${pc(v.fused)}</td><td>${pc(v.pool)}</td><td>${pc(v.final)}</td><td>${pc(v.in_context)}</td><td>${pc(v.correct)}</td></tr>`).join('')}</table></div>`}
h+=`<div class=card><h3>Oracle: accuracy with vs without all gold in context</h3><table><tr><th>category</th><th>all gold in ctx</th><th>acc</th><th>gold missing</th><th>acc</th></tr>${Object.entries(S.oracle).map(([c,v])=>`<tr><td>${c}</td><td>${v.n_all_gold_in_context}</td><td>${pc(v.acc_all_gold_in_context)}</td><td>${v.n_missing_gold}</td><td>${pc(v.acc_missing_gold)}</td></tr>`).join('')}</table></div>
<div class=card><h3>Cascades (wrong questions with several flags)</h3><table>${S.cascades.map(c=>`<tr><td>${c.count}</td><td>${c.flags.map(f=>`<span class=pill>${esc(f)}</span>`).join(' ')}</td></tr>`).join('')||'<tr><td class=mut>none</td></tr>'}</table></div>
<div class=card><h3>Reader failure types</h3>${kv(S.reader_buckets)}</div>${S.injection?`<div class=card><h3>Injection audit</h3>${kv(S.injection)}</div>`:''}</div>
<div class=card><h3>Fix hypotheses per gap</h3>${kv(S.fix_hypotheses)}</div><div class=card><h3>Engine config of the arm</h3><pre>${esc(JSON.stringify(S.config,null,1))}</pre></div>`;
document.getElementById('view').innerHTML=h}
function ranked(list,gold,title){if(!list||!list.length)return `<h3>${title}</h3><div class=mut>empty</div>`;return `<h3>${title}</h3><table><tr><th>#</th><th>turn</th><th>score</th></tr>${list.slice(0,30).map(x=>`<tr class="${gold.includes(x.turn)?'gold':''}"><td>${x.r}</td><td>${esc(x.turn)}${gold.includes(x.turn)?' (gold)':''}</td><td>${x.score}</td></tr>`).join('')}</table>`}
function detail(i){const e=R[i],G=e.gold_turns.map(g=>g.turn);let h=`<div class=card><h3>1. Question and verdict</h3>${kv({id:e.item+' / '+e.qid,category:e.category,question:e.question,gold_answer:e.gold_answer,answer:e.answer,status:e.status,judge_score:e.judge_score,judge_raw:e.judge_raw,correct:e.correct,outcome_class:e.outcome_class,primary_gap:e.primary_gap,context_tokens:e.context_tokens})}</div>`;
h+=`<div class=card><h3>2. Gold evidence and its injection</h3>${e.gold_turns.map(g=>`<div class=card><b>${esc(g.turn)}</b> <span class=mut>${esc(g.source_timestamp)}</span> in context: <b class="${g.in_context?'ok':'bad'}">${g.in_context}</b> lost at: <b>${esc(g.lost_at)}</b>${g.rescued_from?` (rescued by neighbour expansion from ${esc(g.rescued_from)})`:''}<div>${esc(g.text)}</div>${g.ingest?kv(g.ingest):'<div class=mut>no injection log for this run</div>'}${g.ranks?'<h3>rank at each stage</h3>'+kv(g.ranks):''}</div>`).join('')||'<div class=mut>no gold evidence</div>'}</div>`;
if(e.stages){const s=e.stages;h+=`<div class=card><div class=grid><div>${ranked(s.vector,G,'3a. Vector leg')}</div><div>${ranked(s.lexical,G,'3b. BM25 leg')}</div>${Object.entries(s.extra_legs||{}).map(([n,l])=>`<div>${ranked(l,G,'3c. '+n+' leg')}</div>`).join('')}<div>${ranked(s.fused,G,'4. Fusion (cut to top-k)')}</div><div>${ranked(s.pool,G,'5a. Candidate pool')}</div><div>${ranked(s.rerank_scores,G,'5b. Reranker raw scores ('+esc(s.reranker||'none')+')')}</div><div>${ranked(s.final,G,'6. Final list')}</div></div></div>`}
else h+=`<div class=card><h3>3-6. Retrieval stages</h3><div class=mut>This run has no stage log (run with MEMSPINE_FORENSICS_DIR to record legs, fusion, rerank and final lists).</div></div>`;
h+=`<div class=card><h3>7. Context the reader saw</h3>${e.context_text?e.context_text.map(t=>`<div class="${G.some(g=>t&&e.gold_turns.find(x=>x.text===t))?'gold':''}"><pre>${esc(t)}</pre></div>`).join(''):`<div>turns: ${e.retrieved_turns.map(t=>`<span class="pill ${G.includes(t)?'gold':''}">${esc(t)}</span>`).join('')}</div>`}</div>`;
h+=`<div class=card><h3>8. Answer and judge</h3>${kv({answer:e.answer,gold:e.gold_answer,judge_score:e.judge_score,judge_raw:e.judge_raw,reader_failure:e.reader_bucket})}</div><div class=card><h3>9. Gap attribution and flags</h3>${kv({outcome_class:e.outcome_class,primary_gap:e.primary_gap})}<div>${e.flags.map(f=>`<span class=pill>${esc(f)}</span>`).join(' ')||'<span class=mut>no flags</span>'}</div>${e.top_non_gold?'<h3>What ranked instead (non-gold in final)</h3><table>'+e.top_non_gold.map(x=>`<tr><td>#${x.rank}</td><td>${esc(x.turn)}</td><td>${esc(x.text)}</td></tr>`).join('')+'</table>':''}</div>`;
document.getElementById('detail').innerHTML=h}
function questions(){const cats=[...new Set(R.map(r=>r.category))],gaps=[...new Set(R.map(r=>r.primary_gap))],outs=[...new Set(R.map(r=>r.outcome_class))];
document.getElementById('view').innerHTML=`<div class=card>category <select id=fc><option value="">all</option>${cats.map(c=>`<option>${c}</option>`).join('')}</select> verdict <select id=fv><option value="">all</option><option value=wrong>wrong</option><option value=right>correct</option></select> outcome <select id=fo><option value="">all</option>${outs.map(c=>`<option>${c}</option>`).join('')}</select> gap <select id=fg><option value="">all</option>${gaps.map(c=>`<option value="${c}">${c}</option>`).join('')}</select> search <input id=ft placeholder="question, answer, flag"> <span id=cnt class=mut></span></div><div class=split><div class="card scroll" id=list></div><div class="scroll" id=detail><div class="card mut">Pick a question.</div></div></div>`;
const f=()=>{const c=fc.value,v=fv.value,o=fo.value,g=fg.value,t=ft.value.toLowerCase();const idx=R.map((r,i)=>i).filter(i=>{const r=R[i];return(!c||r.category==c)&&(!v||(v=='wrong'?!r.correct:r.correct))&&(!o||r.outcome_class==o)&&(!g||String(r.primary_gap)==g)&&(!t||(r.question+' '+r.answer+' '+r.flags.join(' ')).toLowerCase().includes(t))});cnt.textContent=idx.length+' questions';
list.innerHTML='<table>'+idx.slice(0,2000).map(i=>{const r=R[i];return `<tr class=q onclick="detail(${i})"><td class="${r.correct?'ok':'bad'}">${r.correct?'✓':'✗'}</td><td>${esc(r.item)}/${esc(r.qid)}<br><span class=mut>${r.category} · ${r.outcome_class}${r.primary_gap?' · '+r.primary_gap:''}</span></td><td>${esc(r.question)}</td></tr>`}).join('')+'</table>'};
[fc,fv,fo,fg].forEach(x=>x.onchange=f);ft.oninput=f;f()}
function tab(n){document.querySelectorAll('.tabs button').forEach(b=>b.classList.toggle('on',b.dataset.t==n));n=='o'?overview():questions()}
tab('o');
"""


def page(summary: dict, rows: list[dict]) -> str:
    data = json.dumps({"summary": summary, "rows": rows}).replace("</", "<\\/")
    title = f"{summary['run_id']} forensics"
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style></head><body><header><h1>{html.escape(summary['run_id'])}</h1>
<div class=mut>{summary['n_questions']} questions · accuracy {100 * summary['accuracy']:.1f}% · stage log: {summary['has_stage_log']} · <a href="index.html">all runs</a></div>
<div class=tabs style="margin-top:8px"><button data-t=o onclick="tab('o')">Overview</button><button data-t=q onclick="tab('q')">Questions (step by step)</button></div></header>
<main id=view></main><script>const DATA={data};{PAGE_JS}</script></body></html>"""


def index(summaries: list[dict]) -> str:
    rows = []
    for s in sorted(summaries, key=lambda s: (s["mode"], -s["accuracy"])):
        cat = s["by_category"]
        g = s["primary_gaps"]
        rows.append(
            f"<tr><td><a href='{html.escape(s['run_id'])}.html'>{html.escape(s['run_id'])}</a></td><td>{s['mode']}</td>"
            f"<td>{'yes' if s['has_stage_log'] else 'no'}</td><td>{s['n_questions']}</td><td><b>{100 * s['accuracy']:.1f}</b></td>"
            + "".join(f"<td>{100 * cat[c]['accuracy']:.1f}</td>" if c in cat else "<td></td>" for c in
                      ("single-hop", "multi-hop", "temporal", "open-domain"))
            + f"<td>{(s['context_tokens_mean'] or 0):.0f}</td>"
            + f"<td>{html.escape(str(s['models'].get('embedder')))}</td><td>{html.escape(str(s['models'].get('reranker')))}</td>"
            + "".join(f"<td>{g.get(k, 0)}</td>" for k in ("recall", "fusion", "rerank", "assembly", "reader")) + "</tr>")
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>MemSpine run index</title><style>{CSS}</style></head><body><header><h1>MemSpine evaluation runs</h1>
<div class=mut>QA runs: accuracy = LLM-judge correct with a non-empty answer. Retrieval runs: accuracy = all gold evidence in context.
Gap columns count wrong questions by primary gap (runs without a stage log attribute every evidence miss to recall).</div></header>
<main><div class="card" style="overflow:auto"><table><tr><th>run</th><th>mode</th><th>stage log</th><th>n</th><th>overall</th><th>single-hop</th>
<th>multi-hop</th><th>temporal</th><th>open-domain</th><th>ctx tok</th><th>embedder</th><th>reranker</th><th>recall</th><th>fusion</th>
<th>rerank</th><th>assembly</th><th>reader</th></tr>{''.join(rows)}</table></div></main></body></html>"""


def rebuild(runs_dir: Path) -> None:
    for run in sorted(runs_dir.glob("*--memspine")):
        if not (run / "results.jsonl").exists():
            continue
        run_id = run.name.removesuffix("--memspine")
        cmd = [sys.executable, str(HERE / "forensics_report.py"), "--run", str(run), "--data", str(DATA),
               "--out", str(run / "report")]
        fx = runs_dir / f"{run_id}--forensics"
        if fx.exists():
            cmd += ["--forensics", str(fx)]
        res = subprocess.run(cmd, capture_output=True, text=True)
        print((res.stdout or res.stderr).strip().splitlines()[-1] if (res.stdout or res.stderr) else run_id)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs-dir", default=str(HERE / "runs"))
    ap.add_argument("--out", default=str(HERE / "reports"))
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()
    runs_dir, out = Path(args.runs_dir), Path(args.out)
    if args.rebuild:
        rebuild(runs_dir)
    out.mkdir(parents=True, exist_ok=True)
    summaries = []
    for rep in sorted(runs_dir.glob("*--memspine/report/run_summary.json")):
        s = json.loads(rep.read_text(encoding="utf-8"))
        rows = [json.loads(line) for line in (rep.parent / "per_question.jsonl").read_text(encoding="utf-8").splitlines() if line]
        (out / f"{s['run_id']}.html").write_text(page(s, rows), encoding="utf-8")
        summaries.append(s)
    (out / "index.html").write_text(index(summaries), encoding="utf-8")
    print(f"{len(summaries)} run pages + index -> {out}")


if __name__ == "__main__":
    main()
