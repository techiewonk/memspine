"""Build the MemSpine gap-plan page from gap_plan.json (+ prompt_matrix.json when present).

    python docs/engine_survey/build_gap_plan.py <out.html>
"""

from __future__ import annotations

import html
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "gap_plan.html"
ORDER = ["memspine", "combined", "memos", "memu", "mnemon", "honcho", "memmachine", "hindsight", "mirix",
         "cognee", "memori", "nemori", "lightmem", "mem0", "evermemos"]
NAMES = {"memspine": "MemSpine", "combined": "Combined", "memos": "MemOS", "memu": "memU", "mnemon": "Mnemon",
         "honcho": "Honcho", "memmachine": "MemMachine", "hindsight": "Hindsight", "mirix": "MIRIX", "cognee": "Cognee",
         "memori": "Memori", "nemori": "Nemori", "lightmem": "LightMem", "mem0": "Mem0", "evermemos": "EverMemOS"}
AREAS = {"arch": "Architecture", "write": "Write path", "extract": "Extraction", "prompt": "Prompts", "derive": "Derived layers",
         "time": "Time and updates", "read": "Retrieval", "rank": "Ranking", "answer": "Context", "speed": "Engineering"}
STATUS = {"missing": ("no", "Not built"), "partial": ("part", "Partial"), "built-off": ("off", "Built, off"),
          "by-design": ("design", "By design")}


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def main() -> None:
    plan = json.loads((HERE / "gap_plan.json").read_text(encoding="utf-8"))
    pm_path = HERE / "prompt_matrix.json"
    pm = json.loads(pm_path.read_text(encoding="utf-8")) if pm_path.exists() else None
    gaps = plan["gaps"]

    counts = {k: sum(1 for g in gaps if g["status"] == k) for k in STATUS}
    tiles = "".join(f'<div class="tile"><span class="tnum">{counts[k]}</span><span class="chip {c}">{esc(l)}</span></div>'
                    for k, (c, l) in STATUS.items())

    def flow(title, steps):
        return f'<div class="flowcol"><h3>{esc(title)}</h3><ol>{"".join(f"<li>{esc(s)}</li>" for s in steps)}</ol></div>'
    fl = plan["flow"]
    flows = (f'<div class="flowgrid">{flow("Write path today", fl["write_now"])}{flow("Write path target", fl["write_target"])}'
             f'{flow("Read path today", fl["read_now"])}{flow("Read path target", fl["read_target"])}</div>')

    byid = {g["id"]: g for g in gaps}
    wave_html = []
    for w in plan["waves"]:
        cards = []
        for gid in w["items"]:
            g = byid.get(gid)
            if not g:
                continue
            c, l = STATUS[g["status"]]
            cards.append(
                f'<article class="gap" data-status="{esc(g["status"])}" data-area="{esc(g["area"])}">'
                f'<header><span class="gid">{esc(g["id"])}</span><span class="chip {c}">{esc(l)}</span>'
                f'<span class="area">{esc(AREAS.get(g["area"], g["area"]))}</span><span class="eff">Effort {esc(g["effort"])}</span></header>'
                f'<h3>{esc(g["title"])}</h3>'
                f'<dl><dt>Other engines</dt><dd>{esc(g["others"])}</dd><dt>MemSpine now</dt><dd>{esc(g["now"])}</dd>'
                f'<dt>Why it is not there</dt><dd>{esc(g["why"])}</dd><dt>Proposal</dt><dd>{esc(g["proposal"])}</dd>'
                f'<dt>Test gate</dt><dd>{esc(g["gate"])}</dd></dl></article>')
        wave_html.append(f'<section class="wave" id="wave-{esc(w["wave"])}"><div class="whead"><span class="wtag">Wave {esc(w["wave"])}</span>'
                         f'<h2>{esc(w["name"])}</h2><span class="wtime">{esc(w["time"])}</span></div><div class="gaps">{"".join(cards)}</div></section>')
    planned = {i for w in plan["waves"] for i in w["items"]}
    rest = [g for g in gaps if g["id"] not in planned]
    rest_rows = "".join(
        f'<tr data-status="{esc(g["status"])}"><th scope="row">{esc(g["id"])}</th><td>{esc(g["title"])}</td>'
        f'<td><span class="chip {STATUS[g["status"]][0]}">{esc(STATUS[g["status"]][1])}</span></td><td>{esc(g["others"])}</td><td>{esc(g["why"])}</td><td>{esc(g["proposal"])}</td></tr>'
        for g in rest)

    prompt_html = ""
    if pm:
        feats = pm.get("features", {})
        engs = [s for s in ORDER if s in pm.get("engines", {})]
        head = '<tr><th scope="col" class="param">Prompt feature</th>' + "".join(f'<th scope="col" class="eng">{esc(NAMES.get(s, s))}</th>' for s in engs) + "</tr>"
        rows = []
        for fid, fname in feats.items():
            cells = []
            for s in engs:
                sc = pm["engines"][s].get("scores", {}).get(fid, {})
                st = sc.get("s", "")
                cls = {"Y": "yes", "P": "part", "N": "no"}.get(st, "unk")
                cells.append(f'<td class="dotcell"><span class="dot {cls}" title="{esc(NAMES.get(s, s))}: {esc(sc.get("t", ""))}"></span></td>')
            rows.append(f'<tr><th scope="row" class="param"><span class="fid">{esc(fid)}</span> {esc(fname)}</th>{"".join(cells)}</tr>')
        pg = "".join(
            f'<article class="gap"><header><span class="gid">{esc(x.get("feature"))}</span><span class="area">{esc(feats.get(x.get("feature"), ""))}</span></header>'
            f'<dl><dt>Best examples</dt><dd>{esc("; ".join(x.get("best_examples", [])))}</dd><dt>MemSpine now</dt><dd>{esc(x.get("memspine_now"))}</dd>'
            f'<dt>Proposal</dt><dd>{esc(x.get("proposal"))}</dd><dt>Risk</dt><dd>{esc(x.get("risk"))}</dd></dl></article>'
            for x in pm.get("memspine_gaps", []))
        tech = "".join(f'<li><b>{esc(NAMES.get(t.get("engine"), t.get("engine")))}: {esc(t.get("technique"))}</b> {esc(t.get("t"))} <span class="ev">{esc(t.get("e"))}</span></li>'
                       for t in pm.get("notable_prompt_techniques", []))
        prompt_html = (f'<section class="wave" id="prompts"><div class="whead"><span class="wtag">Prompts</span><h2>Prompt features across engines</h2></div>'
                       f'<p class="intro">Hover a dot for the detail. Filled: has it; half: partial; grey ring: not present.</p>'
                       f'<div class="tablewrap"><table class="matrix"><thead>{head}</thead><tbody>{"".join(rows)}</tbody></table></div>'
                       f'<h3 class="sub">MemSpine prompt gaps and proposals</h3><div class="gaps">{pg}</div>'
                       f'<h3 class="sub">Notable techniques in other engines</h3><ul class="tech">{tech}</ul></section>')

    leads = "".join(f"<li>{esc(x)}</li>" for x in plan["leads"])
    nav = "".join(f'<a class="navchip" href="#wave-{esc(w["wave"])}">Wave {esc(w["wave"])}</a>' for w in plan["waves"])
    page = PAGE.format(css=CSS, title=esc(plan["title"]), intro=esc(plan["intro"]), tiles=tiles, flows=flows,
                       waves="".join(wave_html), rest_rows=rest_rows, prompts=prompt_html, leads=leads, nav=nav,
                       pnav='<a class="navchip" href="#prompts">Prompts</a>' if pm else "", js=JS, date=esc(plan["date"]))
    OUT.write_text(page, encoding="utf-8")
    print(f"wrote {OUT} ({len(page):,} bytes; {len(gaps)} gaps; prompt matrix {'yes' if pm else 'no'})")


CSS = """
/* Layout: one reading column; status tiles, flow comparison, then gap cards grouped by delivery wave, then prompt matrix. */
:root{
  --bg:#f3f5f4; --surface:#ffffff; --ink:#18201d; --muted:#56635d; --line:#d6dedb; --accent:#1f6f5c;
  --yes:#1d7a4f; --yes-bg:#e1f2e8; --off:#7a5a12; --off-bg:#f6ecd2; --part:#355e9a; --part-bg:#e3eaf6; --no:#8a3b33; --no-bg:#f6e2df; --design:#5a4f7a; --design-bg:#ebe7f5;
  --font-display:"Archivo",system-ui,sans-serif; --font-body:"IBM Plex Sans",system-ui,sans-serif; --font-mono:"IBM Plex Mono",ui-monospace,monospace;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#121715; --surface:#1a211e; --ink:#e4ebe7; --muted:#9aa8a1; --line:#2c3632; --accent:#5fc2a4;
  --yes:#7fd6a6; --yes-bg:#1d3328; --off:#e7c875; --off-bg:#36301b; --part:#9dbcf0; --part-bg:#1f2b40; --no:#f0a399; --no-bg:#3a2321; --design:#c4b5f0; --design-bg:#2a2540; color-scheme:dark}}
:root[data-theme="dark"]{
  --bg:#121715; --surface:#1a211e; --ink:#e4ebe7; --muted:#9aa8a1; --line:#2c3632; --accent:#5fc2a4;
  --yes:#7fd6a6; --yes-bg:#1d3328; --off:#e7c875; --off-bg:#36301b; --part:#9dbcf0; --part-bg:#1f2b40; --no:#f0a399; --no-bg:#3a2321; --design:#c4b5f0; --design-bg:#2a2540; color-scheme:dark}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--ink);font-family:var(--font-body);font-size:15px;line-height:1.5;margin:0}
.wrap{max-width:1240px;margin:0 auto;padding-inline:20px;padding-block:28px 64px;display:flex;flex-direction:column;gap:34px}
.eyebrow{font-family:var(--font-mono);font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
h1{font-family:var(--font-display);font-weight:800;font-size:clamp(28px,4.4vw,42px);line-height:1.08;margin:0;text-wrap:balance}
h2{font-family:var(--font-display);font-size:23px;margin:0;text-wrap:balance}
h3{font-family:var(--font-display);font-size:16.5px;margin:0}
.intro{color:var(--muted);max-width:85ch;margin:0}
header.top{display:flex;flex-direction:column;gap:12px}
.tiles{display:flex;flex-wrap:wrap;gap:10px}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:10px 14px;display:flex;align-items:center;gap:10px}
.tnum{font-family:var(--font-display);font-size:26px;font-weight:800;font-variant-numeric:tabular-nums}
.chip{display:inline-block;font-size:10.5px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;border-radius:4px;padding:1px 6px}
.chip.yes{color:var(--yes);background:var(--yes-bg)}.chip.off{color:var(--off);background:var(--off-bg)}
.chip.part{color:var(--part);background:var(--part-bg)}.chip.no{color:var(--no);background:var(--no-bg)}.chip.design{color:var(--design);background:var(--design-bg)}
.tools{position:sticky;top:env(safe-area-inset-top,0px);z-index:5;background:var(--bg);padding-block:10px;border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.navchip{font-size:12.5px;text-decoration:none;color:var(--ink);border:1px solid var(--line);background:var(--surface);border-radius:999px;padding:4px 10px}
.navchip:hover,.navchip:focus-visible{border-color:var(--accent);outline:none}
.filter{font:inherit;font-size:12.5px;border:1px solid var(--line);background:var(--surface);color:var(--ink);border-radius:6px;padding:4px 10px;cursor:pointer}
.filter[aria-pressed="true"]{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
.filter:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.flowgrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.flowcol{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:12px 14px;min-width:0}
.flowcol ol{margin:6px 0 0;padding-left:20px;display:flex;flex-direction:column;gap:4px;font-size:14px}
.wave{display:flex;flex-direction:column;gap:12px;scroll-margin-top:70px}
.whead{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 14px}
.wtag{font-family:var(--font-mono);font-size:12px;color:var(--accent);letter-spacing:.06em;text-transform:uppercase}
.wtime{font-size:13px;color:var(--muted)}
.gaps{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:12px}
.gap{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:12px 14px;display:flex;flex-direction:column;gap:6px;min-width:0}
.gap header{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center}
.gid{font-family:var(--font-mono);font-weight:600;color:var(--accent)}
.area,.eff{font-size:12px;color:var(--muted)}
.gap dl{margin:0;display:grid;grid-template-columns:minmax(0,1fr);gap:2px}
.gap dt{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin-top:4px}
.gap dd{margin:0;font-size:13.5px}
.hide{display:none!important}
.tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:8px;background:var(--surface)}
table{border-collapse:collapse;font-size:13px;width:100%}
th,td{text-align:left;vertical-align:top;padding:7px 10px;border-bottom:1px solid var(--line)}
thead th{font-weight:600}
table.rest{min-width:900px}
table.matrix{width:max-content;min-width:100%}
th.param{position:sticky;left:0;background:var(--surface);min-width:240px;font-weight:500;box-shadow:1px 0 0 var(--line)}
th.eng{font-size:12px;text-align:center;min-width:72px}
.fid{font-family:var(--font-mono);color:var(--accent);font-size:11.5px}
.dotcell{text-align:center}
.dot{display:inline-block;width:13px;height:13px;border-radius:50%;border:2px solid currentColor}
.dot.yes{color:var(--yes);background:var(--yes)}.dot.part{color:var(--part);background:linear-gradient(90deg,var(--part) 50%,transparent 50%)}
.dot.no{color:var(--line)}.dot.unk{color:var(--line);border-style:dashed}
.sub{margin-top:8px}
.tech{margin:0;padding-left:20px;display:flex;flex-direction:column;gap:5px;font-size:13.5px;max-width:100ch}
.ev{font-family:var(--font-mono);font-size:10.5px;color:var(--muted)}
.leads{margin:0;padding-left:20px;display:flex;flex-direction:column;gap:4px}
@media (max-width:700px){.flowgrid{grid-template-columns:minmax(0,1fr)}.gaps{grid-template-columns:minmax(0,1fr)}.wrap{padding-inline:16px}}
"""

JS = """
(function(){
  var btns=[].slice.call(document.querySelectorAll('.filter'));
  function apply(){
    var on=btns.filter(function(b){return b.getAttribute('aria-pressed')==='true'}).map(function(b){return b.dataset.s});
    [].slice.call(document.querySelectorAll('[data-status]')).forEach(function(el){
      el.classList.toggle('hide', on.length>0 && on.indexOf(el.dataset.status)<0);
    });
  }
  btns.forEach(function(b){b.addEventListener('click',function(){b.setAttribute('aria-pressed', b.getAttribute('aria-pressed')==='true'?'false':'true');apply();});});
})();
"""

PAGE = """<title>MemSpine Gap Plan</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700;800&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;600&display=swap">
<style>{css}</style>
<div class="wrap">
<header class="top">
  <span class="eyebrow">Gap analysis · {date}</span>
  <h1>{title}</h1>
  <p class="intro">{intro}</p>
  <div class="tiles">{tiles}</div>
</header>
<div class="tools"><a class="navchip" href="#flow">Flow</a>{nav}<a class="navchip" href="#others">Not scheduled</a>{pnav}<a class="navchip" href="#leads">Where MemSpine leads</a>
<span class="intro" style="margin-left:6px">Show:</span>
<button class="filter" type="button" data-s="missing" aria-pressed="false">Not built</button><button class="filter" type="button" data-s="partial" aria-pressed="false">Partial</button><button class="filter" type="button" data-s="built-off" aria-pressed="false">Built, off</button><button class="filter" type="button" data-s="by-design" aria-pressed="false">By design</button></div>
<section class="wave" id="flow"><div class="whead"><span class="wtag">Flow</span><h2>How the write and read paths change</h2></div>{flows}</section>
{waves}
<section class="wave" id="others"><div class="whead"><span class="wtag">Not scheduled</span><h2>Deliberately absent or on hold, with the reason</h2></div>
<div class="tablewrap"><table class="rest"><thead><tr><th scope="col">ID</th><th scope="col">Capability</th><th scope="col">Status</th><th scope="col">Who has it</th><th scope="col">Why not in MemSpine</th><th scope="col">Position</th></tr></thead><tbody>{rest_rows}</tbody></table></div></section>
{prompts}
<section class="wave" id="leads"><div class="whead"><span class="wtag">Keep</span><h2>Where MemSpine already leads</h2></div><ul class="leads">{leads}</ul></section>
<p class="intro">Sources: memspine/docs/engine_survey (15 verified engine records, prompt_matrix.json, gap_plan.json), evals/analysis/GAP_REGISTER.md, docs/adr. Built by build_gap_plan.py.</p>
</div>
<script>{js}</script>
"""

if __name__ == "__main__":
    main()
