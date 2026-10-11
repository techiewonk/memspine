"""Build the consolidated memory-engine comparison page from docs/engine_survey/*.json.

    python docs/engine_survey/build_comparison.py <out.html>

Every engine record follows SPEC.md. Phases, rows and layers come from SPEC.md so the page and the
records cannot drift apart. An optional synthesis.json adds the decision section.
"""

from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "engine_comparison.html"

ORDER = ["memspine", "combined", "memos", "memu", "mnemon", "honcho", "memmachine", "hindsight", "mirix",
         "cognee", "memori", "nemori", "lightmem", "mem0", "evermemos"]
STATUS = {"Y": ("yes", "Has it"), "O": ("off", "Built, off"), "P": ("part", "Partial"), "N": ("no", "Not present")}
LAYER_NAMES = {
    "raw": "Raw turns", "segment": "Segmentation", "episode": "Episode narrative", "facts": "Atomic facts",
    "graph": "Entity graph", "bitemporal": "Bi-temporal validity", "session_summary": "Session summary",
    "entity_summary": "Entity summary", "profile": "Profile", "prospective": "Prospective / foresight",
    "procedural": "Procedural", "reflection": "Reflection", "shared": "Shared memory", "firewall": "Write firewall",
    "provenance": "Provenance + trust",
}


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def parse_spec():
    text = (HERE / "SPEC.md").read_text(encoding="utf-8")
    phases, cur = [], None
    for line in text.splitlines():
        m = re.match(r"^Phase `([a-z]+)` (.+):$", line)
        if m:
            cur = {"id": m.group(1), "title": m.group(2), "rows": []}
            phases.append(cur)
            continue
        m = re.match(r"^- `([a-z]+\.[a-z]+)` (.+)$", line)
        if m and cur:
            cur["rows"].append((m.group(1), m.group(2)))
    return phases


def load():
    recs = {}
    for f in HERE.glob("*.json"):
        if f.name == "synthesis.json":
            continue
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"skip {f.name}: {exc}", file=sys.stderr)
            continue
        recs[r.get("slug", f.stem)] = r
    order = [s for s in ORDER if s in recs] + sorted(s for s in recs if s not in ORDER)
    return [recs[s] for s in order]


def best_score(r) -> str:
    for x in r.get("locomo") or []:
        if x.get("score"):
            return str(x["score"])
    return "-"


def chip(st):
    if st not in STATUS:
        return ""
    cls, label = STATUS[st]
    return f'<span class="chip {cls}">{esc(label)}</span>'


def main():
    phases = parse_spec()
    engines = load()
    slugs = [e["slug"] for e in engines]
    synth = json.loads((HERE / "synthesis.json").read_text(encoding="utf-8")) if (HERE / "synthesis.json").exists() else None

    def thead(first="Parameter"):
        cells = "".join(
            f'<th scope="col" class="eng" data-e="{esc(e["slug"])}"><span class="ename">{esc(e["name"])}</span>'
            f'<span class="esub">LoCoMo {esc(best_score(e))}</span></th>' for e in engines)
        return f'<tr><th scope="col" class="param">{esc(first)}</th>{cells}</tr>'

    tiles = "".join(
        f'<label class="etile" data-e="{esc(e["slug"])}"><input type="checkbox" class="etoggle" value="{esc(e["slug"])}" checked>'
        f'<span class="tname">{esc(e["name"])}</span><span class="tscore">{esc(best_score(e))}</span>'
        f'<span class="tline">{esc(e.get("one_line", ""))}</span></label>' for e in engines)

    layer_rows = []
    for lid, lname in LAYER_NAMES.items():
        cells = []
        for e in engines:
            st = (e.get("layers") or {}).get(lid, "")
            cls, label = STATUS.get(st, ("unk", "Not recorded"))
            cells.append(f'<td class="dotcell" data-e="{esc(e["slug"])}"><span class="dot {cls}" role="img" aria-label="{esc(label)}" title="{esc(e["name"])}: {esc(label)}"></span></td>')
        layer_rows.append(f'<tr><th scope="row" class="param">{esc(lname)}</th>{"".join(cells)}</tr>')

    nav, sections = [], []
    for i, ph in enumerate(phases, 1):
        nav.append(f'<a href="#{ph["id"]}" class="navchip"><span class="num">{i:02d}</span>{esc(ph["title"])}</a>')
        body = []
        for rid, rname in ph["rows"]:
            cells = []
            for e in engines:
                c = (e.get("rows") or {}).get(rid) or {}
                ev = c.get("e", "")
                evh = f'<span class="ev">{esc(ev)}</span>' if ev and ev != "see engine document" else ""
                cells.append(f'<td data-e="{esc(e["slug"])}" data-s="{esc(c.get("s", ""))}">{chip(c.get("s", ""))}'
                             f'<span class="txt">{esc(c.get("t", "not recorded"))}</span>{evh}</td>')
            body.append(f'<tr><th scope="row" class="param">{esc(rname)}</th>{"".join(cells)}</tr>')
        sections.append(
            f'<section class="phase" id="{ph["id"]}"><div class="phead"><span class="pnum">Phase {i:02d}</span>'
            f'<h2>{esc(ph["title"])}</h2></div><div class="tablewrap"><table class="cmp"><thead>{thead()}</thead>'
            f'<tbody>{"".join(body)}</tbody></table></div></section>')

    locomo_rows = []
    for e in engines:
        for x in e.get("locomo") or []:
            locomo_rows.append(
                f'<tr data-e="{esc(e["slug"])}"><th scope="row" class="param">{esc(e["name"])}</th><td class="num">{esc(x.get("score"))}</td>'
                f'<td>{esc(x.get("who"))}</td><td>{esc(x.get("protocol"))}</td><td class="ev">{esc(x.get("source"))}</td></tr>')

    def cardlist(key):
        out = []
        for e in engines:
            v = e.get(key) or {}
            if not v.get("t"):
                continue
            out.append(f'<article class="ecard" data-e="{esc(e["slug"])}"><h3>{esc(e["name"])}</h3><p>{esc(v["t"])}</p>'
                       f'<p class="ev">{esc(v.get("e", ""))}</p></article>')
        return "".join(out)

    extras = []
    phase_title = {p["id"]: p["title"] for p in phases}
    for e in engines:
        for x in e.get("extras") or []:
            extras.append((phase_title.get(x.get("phase"), x.get("phase", "")), e, x))
    extras.sort(key=lambda t: ([p["title"] for p in phases].index(t[0]) if t[0] in phase_title.values() else 99))
    extra_rows = "".join(
        f'<tr data-e="{esc(e["slug"])}"><td>{esc(ph)}</td><th scope="row" class="param">{esc(e["name"])}</th>'
        f'<td><b>{esc(x.get("param"))}</b> {esc(x.get("t"))}</td><td class="ev">{esc(x.get("e"))}</td></tr>' for ph, e, x in extras)

    progress = "".join(
        f'<article class="ecard" data-e="{esc(e["slug"])}"><h3>{esc(e["name"])}</h3><ul>'
        + "".join(f"<li>{esc(p)}</li>" for p in e["in_progress"]) + "</ul></article>"
        for e in engines if e.get("in_progress"))

    keep = "".join(
        f'<article class="ecard" data-e="{esc(e["slug"])}"><h3>{esc(e["name"])}</h3><p class="ev">{esc(e.get("repo", ""))}'
        f'{(" @ " + esc(e["commit"])) if e.get("commit") else ""}</p>'
        f'<h4>Keep or learn</h4><ul>{"".join(f"<li>{esc(k)}</li>" for k in e.get("keep") or [])}</ul>'
        f'<h4>Avoid</h4><ul class="neg">{"".join(f"<li>{esc(k)}</li>" for k in e.get("avoid") or [])}</ul></article>'
        for e in engines)

    synth_html = ""
    if synth:
        blocks = []
        for b in synth.get("sections", []):
            items = "".join(f"<li>{esc(i)}</li>" for i in b.get("items", []))
            table = ""
            if b.get("table"):
                hdr = "".join(f"<th scope='col'>{esc(h)}</th>" for h in b["table"][0])
                rows = "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>" for r in b["table"][1:])
                table = f'<div class="tablewrap"><table class="plain"><thead><tr>{hdr}</tr></thead><tbody>{rows}</tbody></table></div>'
            blocks.append(f'<div class="sblock"><h3>{esc(b.get("title"))}</h3>{f"<p>{esc(b["text"])}</p>" if b.get("text") else ""}'
                          f'{f"<ul>{items}</ul>" if items else ""}{table}</div>')
        synth_html = (f'<section class="phase" id="decision"><div class="phead"><span class="pnum">Decision</span>'
                      f'<h2>{esc(synth.get("title", "What MemSpine should build"))}</h2><p class="pintro">{esc(synth.get("intro", ""))}</p></div>'
                      f'{"".join(blocks)}</section>')

    page = PAGE.format(
        css=CSS, n=len(engines), tiles=tiles, layer_head=thead("Layer"), layer_rows="".join(layer_rows),
        nav="".join(nav), sections="".join(sections), locomo_rows="".join(locomo_rows),
        tokens=cardlist("token_economy"), speed=cardlist("speed"), extra_rows=extra_rows, progress=progress,
        keep=keep, synth=synth_html, js=JS, decision_nav='<a href="#decision" class="navchip"><span class="num">··</span>Decision</a>' if synth else "",
    )
    OUT.write_text(page, encoding="utf-8")
    print(f"wrote {OUT} ({len(page):,} bytes, {len(engines)} engines: {', '.join(slugs)})")


CSS = """
/* Layout: one reading column; a sticky toolbar with phase index and engine switches; every comparison is a table with a sticky parameter column. */
:root{
  --bg:#f3f5f4; --surface:#ffffff; --ink:#18201d; --muted:#56635d; --line:#d6dedb; --accent:#1f6f5c; --hi:#fff6d6;
  --yes:#1d7a4f; --yes-bg:#e1f2e8; --off:#7a5a12; --off-bg:#f6ecd2; --part:#355e9a; --part-bg:#e3eaf6; --no:#8a3b33; --no-bg:#f6e2df;
  --font-display:"Archivo",system-ui,sans-serif; --font-body:"IBM Plex Sans",system-ui,sans-serif; --font-mono:"IBM Plex Mono",ui-monospace,monospace;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#121715; --surface:#1a211e; --ink:#e4ebe7; --muted:#9aa8a1; --line:#2c3632; --accent:#5fc2a4; --hi:#2e2a17;
  --yes:#7fd6a6; --yes-bg:#1d3328; --off:#e7c875; --off-bg:#36301b; --part:#9dbcf0; --part-bg:#1f2b40; --no:#f0a399; --no-bg:#3a2321; color-scheme:dark}}
:root[data-theme="dark"]{
  --bg:#121715; --surface:#1a211e; --ink:#e4ebe7; --muted:#9aa8a1; --line:#2c3632; --accent:#5fc2a4; --hi:#2e2a17;
  --yes:#7fd6a6; --yes-bg:#1d3328; --off:#e7c875; --off-bg:#36301b; --part:#9dbcf0; --part-bg:#1f2b40; --no:#f0a399; --no-bg:#3a2321; color-scheme:dark}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--ink);font-family:var(--font-body);font-size:15px;line-height:1.5;margin:0}
.wrap{max-width:1400px;margin:0 auto;padding-inline:20px;padding-block:28px 64px;display:flex;flex-direction:column;gap:34px}
header.top{display:flex;flex-direction:column;gap:12px}
.eyebrow{font-family:var(--font-mono);font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
h1{font-family:var(--font-display);font-weight:800;font-size:clamp(28px,4.4vw,44px);line-height:1.08;margin:0;text-wrap:balance;letter-spacing:-.01em}
h2{font-family:var(--font-display);font-weight:700;font-size:24px;margin:0;text-wrap:balance}
h3{font-family:var(--font-display);font-size:17px;margin:0}
h4{font-size:11.5px;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);margin:10px 0 4px}
.lede{max-width:78ch;color:var(--muted);margin:0}
.callout{border-left:3px solid var(--accent);background:var(--surface);padding:12px 14px;border-radius:0 6px 6px 0;max-width:92ch}
.callout p{margin:0}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px}
.etile{display:grid;grid-template-columns:auto 1fr auto;gap:2px 8px;align-items:baseline;background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:8px 10px;cursor:pointer;min-width:0}
.etile input{grid-row:1;margin:0}
.etile .tname{font-family:var(--font-display);font-weight:700;font-size:15px}
.etile .tscore{font-family:var(--font-mono);font-size:12.5px;color:var(--accent)}
.etile .tline{grid-column:1/-1;font-size:12px;color:var(--muted)}
.etile:has(input:not(:checked)){opacity:.5}
.etile:focus-within{outline:2px solid var(--accent);outline-offset:1px}
.tools{position:sticky;top:env(safe-area-inset-top,0px);z-index:5;background:var(--bg);padding-block:10px;border-bottom:1px solid var(--line);display:flex;flex-direction:column;gap:8px}
.navrow{display:flex;gap:6px;overflow-x:auto;padding-bottom:2px;scrollbar-width:thin}
.navchip{flex:none;font-size:12.5px;text-decoration:none;color:var(--ink);border:1px solid var(--line);background:var(--surface);border-radius:999px;padding:4px 10px;white-space:nowrap}
.navchip:hover,.navchip:focus-visible{border-color:var(--accent);outline:none}
.navchip .num{font-family:var(--font-mono);color:var(--muted);margin-right:6px}
.filters{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center}
.filters input[type=search]{font:inherit;font-size:14px;padding:6px 10px;border:1px solid var(--line);border-radius:6px;background:var(--surface);color:var(--ink);min-width:0;flex:1 1 220px;max-width:340px}
.filters input:focus-visible,.filters button:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.filters label{font-size:13px;display:inline-flex;gap:6px;align-items:center;cursor:pointer}
.filters button{font:inherit;font-size:12.5px;border:1px solid var(--line);background:var(--surface);color:var(--ink);border-radius:6px;padding:4px 10px;cursor:pointer}
.filters .count{font-family:var(--font-mono);font-size:12px;color:var(--muted)}
.legend{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;font-size:12.5px;color:var(--muted)}
.phase{display:flex;flex-direction:column;gap:12px;scroll-margin-top:150px}
.phead{display:flex;flex-direction:column;gap:2px}
.pnum{font-family:var(--font-mono);font-size:12px;color:var(--accent);letter-spacing:.06em;text-transform:uppercase}
.pintro{margin:0;color:var(--muted);max-width:90ch}
.tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:8px;background:var(--surface)}
table{border-collapse:collapse;font-size:13px}
table.cmp,table.layers{width:max-content;min-width:100%}
th,td{text-align:left;vertical-align:top;padding:8px 10px;border-bottom:1px solid var(--line)}
tbody tr:last-child th,tbody tr:last-child td{border-bottom:0}
thead th{background:var(--surface);font-weight:600;border-bottom:2px solid var(--line);vertical-align:bottom}
th.param{min-width:150px;max-width:190px;font-weight:600;position:sticky;left:0;background:var(--surface);z-index:1;box-shadow:1px 0 0 var(--line)}
thead th.param{z-index:2}
table.cmp td,table.cmp th.eng{width:230px;min-width:230px;max-width:230px}
.ename{display:block;font-family:var(--font-display);font-size:14.5px;color:var(--accent)}
.esub{display:block;font-weight:400;font-size:11.5px;color:var(--muted);font-family:var(--font-mono)}
td .txt{display:block}
.ev{display:block;font-family:var(--font-mono);font-size:10.5px;color:var(--muted);margin-top:3px;word-break:break-word}
td[data-e="memspine"],th[data-e="memspine"]{background:color-mix(in srgb,var(--accent) 6%,var(--surface))}
.chip{display:inline-block;font-size:10px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;border-radius:4px;padding:1px 6px;margin-bottom:3px}
.chip.yes{color:var(--yes);background:var(--yes-bg)}.chip.off{color:var(--off);background:var(--off-bg)}
.chip.part{color:var(--part);background:var(--part-bg)}.chip.no{color:var(--no);background:var(--no-bg)}
.dot{display:inline-block;width:14px;height:14px;border-radius:50%;border:2px solid currentColor}
.dot.yes{color:var(--yes);background:var(--yes)}.dot.off{color:var(--off);background:transparent}
.dot.part{color:var(--part);background:linear-gradient(90deg,var(--part) 50%,transparent 50%)}.dot.no{color:var(--line);background:transparent}
.dot.unk{color:var(--line);background:transparent;border-style:dashed}
.dotcell{text-align:center;min-width:64px}
table.layers th.eng{text-align:center;min-width:64px}
table.layers .esub{display:none}
table.plain{width:100%;min-width:640px}
td.num{font-family:var(--font-mono);font-variant-numeric:tabular-nums;white-space:nowrap}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}
.ecard{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:12px 14px;min-width:0;display:flex;flex-direction:column;gap:4px}
.ecard p{margin:0;font-size:13.5px}
.ecard ul{margin:0;padding-left:18px;display:flex;flex-direction:column;gap:3px;font-size:13.5px}
.ecard ul.neg li::marker{color:var(--no)}
.sblock{display:flex;flex-direction:column;gap:6px;max-width:100%}
.sblock p{margin:0;max-width:90ch}
.sblock ul{margin:0;padding-left:20px;display:flex;flex-direction:column;gap:4px;max-width:95ch}
#decision{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:18px}
tr.hide{display:none}
section.phase.empty{display:none}
mark{background:var(--hi);color:inherit}
@media (max-width:560px){.wrap{padding-inline:16px}.tiles{grid-template-columns:minmax(0,1fr)}}
@media (prefers-reduced-motion:no-preference){html{scroll-behavior:smooth}}
"""

JS = """
(function(){
  var style=document.createElement('style'); document.head.appendChild(style);
  var toggles=[].slice.call(document.querySelectorAll('.etoggle'));
  function cols(){
    var off=toggles.filter(function(t){return !t.checked}).map(function(t){return t.value});
    style.textContent=off.map(function(s){return '[data-e="'+s+'"]:not(.etile)'}).join(',')+(off.length?'{display:none}':'');
    apply();
  }
  toggles.forEach(function(t){t.addEventListener('change',cols)});
  document.getElementById('all').addEventListener('click',function(){toggles.forEach(function(t){t.checked=true});cols()});
  document.getElementById('none').addEventListener('click',function(){toggles.forEach(function(t){t.checked=(t.value==='memspine')});cols()});
  var q=document.getElementById('q'), diff=document.getElementById('diff'), count=document.getElementById('count');
  var rows=[].slice.call(document.querySelectorAll('section.phase table.cmp tbody tr'));
  function visible(){return toggles.filter(function(t){return t.checked}).map(function(t){return t.value})}
  function apply(){
    var term=(q.value||'').trim().toLowerCase(), onlyDiff=diff.checked, vis=visible(), shown=0;
    rows.forEach(function(r){
      var ok=!term || r.textContent.toLowerCase().indexOf(term)>-1;
      if(ok && onlyDiff){
        var st=vis.map(function(s){var td=r.querySelector('td[data-e="'+s+'"]');return td?td.getAttribute('data-s'):''}).filter(function(s){return s});
        ok = st.length>1 && !st.every(function(s){return s===st[0]});
      }
      r.classList.toggle('hide',!ok); if(ok) shown++;
    });
    [].slice.call(document.querySelectorAll('section.phase')).forEach(function(s){
      if(!s.querySelector('table.cmp')) return;
      s.classList.toggle('empty', !s.querySelector('table.cmp tbody tr:not(.hide)'));
    });
    count.textContent=shown+' of '+rows.length+' rows';
  }
  q.addEventListener('input',apply); diff.addEventListener('change',apply); cols();
})();
"""

PAGE = """<title>Memory Engine Comparison</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700;800&family=IBM+Plex+Sans:wght@400;600&family=IBM+Plex+Mono:wght@400;600&display=swap">
<style>{css}</style>
<div class="wrap">
<header class="top">
  <span class="eyebrow">Consolidated comparison · code-traced · 11 October 2026</span>
  <h1>{n} memory engines, phase by phase</h1>
  <p class="lede">MemSpine, the combined Graphiti + SimpleMem engine and every open-source system scoring 78 or more on LoCoMo, traced from their code across 14 phases and 57 parameters, with layers, extras, token economy, speed and work in progress. Each cell carries its evidence. Use the switches to choose which engines to compare.</p>
  <div class="callout"><p><b>Read scores with care.</b> Published LoCoMo numbers differ in reader, judge, retrieval budget, question set and who ran the harness; vendor numbers fell by 8 to 68 points in outside harnesses. The score under each name is the first one the engine's record lists, not a ranking. MemSpine's absolute numbers before commit 8cebf97 are contaminated (I80); only its relative deltas stand.</p></div>
  <div class="tiles">{tiles}</div>
</header>

<div class="tools">
  <nav class="navrow" aria-label="Sections"><a href="#layers" class="navchip"><span class="num">··</span>Layers</a>{decision_nav}{nav}<a href="#locomo" class="navchip"><span class="num">··</span>LoCoMo</a><a href="#tokens" class="navchip"><span class="num">··</span>Token economy</a><a href="#speed" class="navchip"><span class="num">··</span>Speed</a><a href="#extras" class="navchip"><span class="num">··</span>Extras</a><a href="#progress" class="navchip"><span class="num">··</span>In progress</a><a href="#keep" class="navchip"><span class="num">··</span>Keep / avoid</a></nav>
  <div class="filters">
    <input type="search" id="q" placeholder="Filter rows, e.g. temporal, rerank, provenance" aria-label="Filter rows">
    <label><input type="checkbox" id="diff"> Only rows where the shown engines differ</label>
    <button type="button" id="all">Show all engines</button><button type="button" id="none">Only MemSpine</button>
    <span class="count" id="count" aria-live="polite"></span>
  </div>
  <div class="legend"><span class="chip yes">Has it</span><span class="chip off">Built, off</span><span class="chip part">Partial</span><span class="chip no">Not present</span><span>Grey mono line under a cell = evidence (file:line or document)</span></div>
</div>

<section class="phase" id="layers"><div class="phead"><span class="pnum">Overview</span><h2>Which memory layers each engine has</h2>
<p class="pintro">Filled dot: on. Ring: built, off by default. Half dot: partial. Grey ring: absent. Dashed ring: not recorded.</p></div>
<div class="tablewrap"><table class="layers"><thead>{layer_head}</thead><tbody>{layer_rows}</tbody></table></div></section>

{synth}

{sections}

<section class="phase" id="locomo"><div class="phead"><span class="pnum">Results</span><h2>Every LoCoMo number, with its protocol</h2>
<p class="pintro">None of these is comparable with another unless reader, judge, budget and question set match.</p></div>
<div class="tablewrap"><table class="plain"><thead><tr><th scope="col" class="param">Engine</th><th scope="col">Score</th><th scope="col">Who ran it</th><th scope="col">Protocol</th><th scope="col">Source</th></tr></thead><tbody>{locomo_rows}</tbody></table></div></section>

<section class="phase" id="tokens"><div class="phead"><span class="pnum">Token economy</span><h2>How each engine keeps context small</h2></div><div class="cards">{tokens}</div></section>

<section class="phase" id="speed"><div class="phead"><span class="pnum">Engineering</span><h2>Speed: async, batching, caching, parallelism</h2></div><div class="cards">{speed}</div></section>

<section class="phase" id="extras"><div class="phead"><span class="pnum">Beyond the rows</span><h2>Extra features, by phase</h2></div>
<div class="tablewrap"><table class="plain"><thead><tr><th scope="col">Phase</th><th scope="col" class="param">Engine</th><th scope="col">Feature</th><th scope="col">Evidence</th></tr></thead><tbody>{extra_rows}</tbody></table></div></section>

<section class="phase" id="progress"><div class="phead"><span class="pnum">Moving parts</span><h2>Work in progress</h2><p class="pintro">Branches, open pull requests, roadmaps and recent commits that change the picture.</p></div><div class="cards">{progress}</div></section>

<section class="phase" id="keep"><div class="phead"><span class="pnum">Per engine</span><h2>What to keep, and what to avoid</h2></div><div class="cards">{keep}</div></section>

<footer class="pintro"><h4>Sources</h4><p>Records live in <code>memspine/docs/engine_survey/*.json</code> (one per engine, schema in SPEC.md) and are built by <code>build_comparison.py</code>. Clones were read only, never installed or run. Earlier engine documents: MEMSPINE_ENGINE_AND_LOCOMO, MEM0_ENGINE, EVERMEMOS_ENGINE, ENGINE_COMPARISON_AND_WRITE_PATH_DESIGN, and the combined engine guide.</p></footer>
</div>
<script>{js}</script>
"""

if __name__ == "__main__":
    main()
