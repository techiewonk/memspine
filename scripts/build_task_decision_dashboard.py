"""Build the offline task-status and owner-decision dashboard.

The Markdown register remains authoritative. Decisions entered in the dashboard
stay in browser localStorage until the owner exports them as JSON; this script
never writes decisions back into the repository.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

if __package__:
    from scripts.refresh_tracker import SOURCE, tasks
else:
    from refresh_tracker import SOURCE, tasks

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "v2" / "task_decision_dashboard.html"


def _impact(status: str, priority: str) -> tuple[str, str]:
    if status == "DONE":
        return "REALIZED", "Delivered and verified; revisit only when affected code changes."
    if status == "EXCLUDED":
        return "NONE", "Explicitly outside the selected product scope."
    if status in {"DEPLOYMENT_CHECK", "NEEDS_TOOLCHAIN"}:
        return "EXTERNAL", "Completion depends on deployment infrastructure or a native toolchain."
    if priority == "P1":
        return (
            "HIGH",
            "Core correctness, safety, compatibility, or a prerequisite chain may be affected.",
        )
    if priority == "P2":
        return "MEDIUM", "Material capability, quality, or operational readiness may be affected."
    return "LOW", "Useful follow-up with limited impact on the current core path."


def _suggested_action(status: str) -> str:
    return {
        "DONE": "Keep complete",
        "EXCLUDED": "Keep excluded",
        "DEFERRED": "Revisit later",
        "DEPLOYMENT_CHECK": "Schedule deployment check",
        "NEEDS_INPUT": "Owner decision required",
        "NEEDS_TOOLCHAIN": "Provide toolchain",
        "OPEN_REVIEW": "Accept, revise, or close",
        "REVIEW": "Review evidence",
        "PROPOSED": "Approve or reject",
        "OPEN": "Prioritize or defer",
        "IN_PROGRESS": "Continue or pause",
    }.get(status, "Review")


def _records(source: str) -> list[dict[str, str]]:
    records = []
    for row in tasks(source):
        task_id, status, priority, owner, task, evidence, next_step, dependencies = row
        impact, impact_reason = _impact(status, priority)
        records.append(
            {
                "id": task_id,
                "program": task_id.split("-", 1)[0],
                "status": status,
                "priority": priority,
                "owner": owner,
                "task": task,
                "evidence": evidence,
                "next": next_step,
                "dependencies": dependencies,
                "impact": impact,
                "impact_reason": impact_reason,
                "suggested_action": _suggested_action(status),
            }
        )
    return records


def build() -> str:
    source = SOURCE.read_text(encoding="utf-8")
    records = _records(source)
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    payload = json.dumps(records, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    template = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MemSpine task decisions</title>
<style>
:root{--ink:#182033;--muted:#64718a;--line:#dbe2ee;--paper:#fff;--bg:#f3f6fb;--nav:#14213d;--blue:#2457d6;--green:#16835b;--amber:#b76a00;--red:#b42318;--violet:#6e49c8;--radius:12px}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}.shell{max-width:1800px;margin:auto;padding:18px}.top{background:linear-gradient(130deg,#14213d,#223866);color:#fff;border-radius:16px;padding:22px 24px;box-shadow:0 10px 30px #13213d22}.topline{display:flex;justify-content:space-between;gap:18px;align-items:flex-start;flex-wrap:wrap}h1{margin:0 0 5px;font-size:25px}.subtitle{color:#cbd7ec;max-width:850px}.actions{display:flex;gap:8px;flex-wrap:wrap}button,.file-button{border:1px solid #ffffff40;background:#ffffff12;color:#fff;border-radius:8px;padding:9px 12px;font:inherit;font-weight:650;cursor:pointer}.file-button input{display:none}button:hover,.file-button:hover{background:#ffffff25}.source{font:12px ui-monospace,SFMono-Regular,Consolas,monospace;color:#aebed8;margin-top:12px;overflow-wrap:anywhere}.kpis{display:grid;grid-template-columns:repeat(5,minmax(145px,1fr));gap:12px;margin:16px 0}.card{background:var(--paper);border:1px solid var(--line);border-radius:var(--radius);padding:15px 17px;box-shadow:0 3px 12px #17233b0a}.kpi-label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}.kpi-value{font-size:27px;font-weight:780;margin-top:3px}.kpi-note{font-size:12px;color:var(--muted)}.grid{display:grid;grid-template-columns:minmax(320px,.65fr) minmax(420px,1.35fr);gap:14px;margin-bottom:14px}.panel{background:var(--paper);border:1px solid var(--line);border-radius:var(--radius);padding:17px;box-shadow:0 3px 12px #17233b0a}.panel h2{font-size:15px;margin:0 0 14px}.bar-row{display:grid;grid-template-columns:135px 1fr 40px;gap:10px;align-items:center;margin:8px 0}.bar-label{font-size:12px;overflow:hidden;text-overflow:ellipsis}.track{height:12px;background:#edf1f7;border-radius:20px;overflow:hidden}.fill{height:100%;border-radius:inherit}.bar-count{text-align:right;font-variant-numeric:tabular-nums}.decision-summary{display:grid;grid-template-columns:repeat(3,1fr);gap:9px}.decision-pill{border:1px solid var(--line);border-radius:9px;padding:10px}.decision-pill strong{display:block;font-size:20px}.decision-pill span{font-size:11px;color:var(--muted)}.filters{background:var(--paper);border:1px solid var(--line);border-radius:var(--radius);padding:14px;display:grid;grid-template-columns:minmax(220px,2fr) repeat(4,minmax(130px,1fr)) auto;gap:10px;align-items:end;margin-bottom:14px;position:sticky;top:0;z-index:5;box-shadow:0 5px 18px #17233b12}.field label{display:block;color:var(--muted);font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;margin:0 0 5px}.field input,.field select{width:100%;border:1px solid #c8d2e1;border-radius:8px;background:#fff;color:var(--ink);padding:9px 10px;font:inherit}.toggle{white-space:nowrap;padding:9px 5px}.toggle input{vertical-align:middle}.table-card{background:var(--paper);border:1px solid var(--line);border-radius:var(--radius);overflow:hidden;box-shadow:0 3px 12px #17233b0a}.table-head{display:flex;justify-content:space-between;gap:12px;padding:14px 16px;border-bottom:1px solid var(--line);align-items:center}.table-wrap{overflow:auto;max-height:72vh}table{border-collapse:separate;border-spacing:0;width:100%;min-width:1450px}th{position:sticky;top:0;background:#edf2f8;color:#536079;font-size:11px;text-transform:uppercase;letter-spacing:.05em;text-align:left;padding:10px;border-bottom:1px solid #cbd5e3;cursor:pointer;z-index:2}td{padding:10px;border-bottom:1px solid #edf0f5;vertical-align:top}tbody tr:hover{background:#f8faff}.id{font-weight:800;white-space:nowrap}.badge{display:inline-block;border-radius:999px;padding:3px 8px;font-size:10px;font-weight:800;letter-spacing:.03em;white-space:nowrap}.s-DONE,.i-REALIZED{background:#dff5eb;color:#086744}.s-IN_PROGRESS{background:#dce8ff;color:#174ca3}.s-OPEN,.s-OPEN_REVIEW,.s-REVIEW{background:#fff0d5;color:#8b5100}.s-NEEDS_INPUT,.i-HIGH{background:#ffe0dc;color:#9b2318}.s-DEPLOYMENT_CHECK,.s-NEEDS_TOOLCHAIN,.i-EXTERNAL{background:#eee6ff;color:#5932a6}.s-DEFERRED,.s-PROPOSED,.i-MEDIUM{background:#f2e9d8;color:#79510e}.s-EXCLUDED,.i-NONE,.i-LOW{background:#e9edf3;color:#526076}.task{min-width:270px;font-weight:620}.impact{min-width:210px}.impact div{color:var(--muted);font-size:12px;margin-top:5px}.next{min-width:260px}.decision-cell{min-width:210px}.decision-cell select,.decision-cell input{width:100%;border:1px solid #c8d2e1;border-radius:7px;padding:7px;background:#fff;font:inherit}.decision-cell input{margin-top:6px;font-size:12px}.details{min-width:360px;color:var(--muted);font-size:12px}.details summary{color:var(--blue);cursor:pointer;font-weight:650}.details p{margin:8px 0}.empty{text-align:center;color:var(--muted);padding:40px}.footer{color:var(--muted);font-size:12px;padding:13px 4px}.toast{position:fixed;right:20px;bottom:20px;background:#16213b;color:#fff;padding:11px 15px;border-radius:9px;opacity:0;transform:translateY(8px);transition:.2s;pointer-events:none;z-index:20}.toast.show{opacity:1;transform:none}@media(max-width:1000px){.kpis{grid-template-columns:repeat(2,1fr)}.grid{grid-template-columns:1fr}.filters{position:static;grid-template-columns:1fr 1fr}.decision-summary{grid-template-columns:1fr 1fr}}@media(max-width:620px){.shell{padding:9px}.kpis,.filters{grid-template-columns:1fr}.top{padding:18px}.decision-summary{grid-template-columns:1fr}}@media print{body{background:#fff}.actions,.filters,.decision-cell{display:none}.shell{max-width:none}.table-wrap{max-height:none;overflow:visible}.panel,.card,.table-card{box-shadow:none}}
</style>
</head>
<body><main class="shell">
<section class="top"><div class="topline"><div><h1>MemSpine task decisions</h1><div class="subtitle">Point-in-time view of the canonical task register. Filter by status and impact, inspect evidence and dependencies, then record an owner decision. Browser decisions do not change source files.</div></div><div class="actions"><button id="export">Export decisions JSON</button><label class="file-button">Import decisions JSON<input id="import" type="file" accept="application/json"></label><button id="clear">Clear decisions</button></div></div><div class="source">Source SHA256: __SOURCE_HASH__</div></section>
<section class="kpis"><div class="card"><div class="kpi-label">Total tasks</div><div class="kpi-value" id="k-total">0</div><div class="kpi-note">Canonical register</div></div><div class="card"><div class="kpi-label">Completed</div><div class="kpi-value" id="k-done">0</div><div class="kpi-note" id="k-rate">0%</div></div><div class="card"><div class="kpi-label">Active</div><div class="kpi-value" id="k-active">0</div><div class="kpi-note">In progress + open</div></div><div class="card"><div class="kpi-label">Owner decisions</div><div class="kpi-value" id="k-input">0</div><div class="kpi-note">Needs input / review</div></div><div class="card"><div class="kpi-label">Selections saved</div><div class="kpi-value" id="k-selected">0</div><div class="kpi-note">Stored in this browser</div></div></section>
<section class="grid"><div class="panel"><h2>Status distribution</h2><div id="status-bars"></div></div><div class="panel"><h2>Decision progress</h2><div class="decision-summary" id="decision-summary"></div></div></section>
<section class="filters"><div class="field"><label for="search">Search</label><input id="search" placeholder="ID, owner, task, evidence, dependency…"></div><div class="field"><label for="status">Status</label><select id="status"><option value="">All statuses</option></select></div><div class="field"><label for="priority">Priority</label><select id="priority"><option value="">All priorities</option></select></div><div class="field"><label for="program">Program</label><select id="program"><option value="">All programs</option></select></div><div class="field"><label for="decision">Decision</label><select id="decision"><option value="">All decisions</option><option value="unset">Unset only</option><option value="proceed">Proceed</option><option value="keep">Keep</option><option value="defer">Defer</option><option value="remove">Remove</option><option value="needs_input">Needs input</option><option value="review">Review</option></select></div><label class="toggle"><input id="hide-done" type="checkbox"> Hide completed</label></section>
<section class="table-card"><div class="table-head"><strong>Task register</strong><span id="visible-count"></span></div><div class="table-wrap"><table><thead><tr><th data-sort="id">ID</th><th data-sort="status">Status</th><th data-sort="priority">Priority</th><th data-sort="task">Task</th><th data-sort="impact">Impact</th><th data-sort="next">Next action</th><th data-sort="decision">Your decision</th><th>Evidence & dependencies</th></tr></thead><tbody id="rows"></tbody></table><div class="empty" id="empty" hidden>No tasks match the current filters.</div></div></section>
<div class="footer">Authoritative status remains in <code>docs/PROVIDER_TODO.md</code>. Exported decisions are proposals for review; importing them restores browser selections only.</div></main><div class="toast" id="toast"></div>
<script>
const DATA=__TASK_DATA__;
const SOURCE_HASH="__SOURCE_HASH__";
const STORAGE_KEY="memspine.taskDecisions.v1";
const DECISION_OPTIONS=[["","Unset"],["proceed","Proceed"],["keep","Keep"],["defer","Defer"],["remove","Remove"],["needs_input","Needs input"],["review","Review"]];
const COLORS={DONE:"#16835b",IN_PROGRESS:"#2457d6",OPEN_REVIEW:"#d98a16",OPEN:"#b76a00",REVIEW:"#7d5bbb",PROPOSED:"#9a6b18",NEEDS_INPUT:"#b42318",NEEDS_TOOLCHAIN:"#6e49c8",DEPLOYMENT_CHECK:"#7456b8",DEFERRED:"#788399",EXCLUDED:"#a6afbd"};
let decisions=loadDecisions(),sortKey="id",sortAsc=true;
function esc(v){return String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
function loadDecisions(){try{return JSON.parse(localStorage.getItem(STORAGE_KEY)||"{}")}catch{return {}}}
function saveDecisions(){localStorage.setItem(STORAGE_KEY,JSON.stringify(decisions));renderKPIs();renderDecisionSummary()}
function toast(message){const el=document.getElementById("toast");el.textContent=message;el.classList.add("show");setTimeout(()=>el.classList.remove("show"),1800)}
function fillSelect(id,field){const el=document.getElementById(id);[...new Set(DATA.map(r=>r[field]))].sort().forEach(v=>el.insertAdjacentHTML("beforeend",`<option>${esc(v)}</option>`))}
function decisionOf(id){return decisions[id]?.decision||""}
function filtered(){const q=document.getElementById("search").value.trim().toLowerCase(),status=document.getElementById("status").value,priority=document.getElementById("priority").value,program=document.getElementById("program").value,decision=document.getElementById("decision").value,hideDone=document.getElementById("hide-done").checked;return DATA.filter(r=>{if(status&&r.status!==status)return false;if(priority&&r.priority!==priority)return false;if(program&&r.program!==program)return false;if(hideDone&&r.status==="DONE")return false;const d=decisionOf(r.id);if(decision==="unset"&&d)return false;if(decision&&decision!=="unset"&&d!==decision)return false;if(q&&!Object.values(r).join(" ").toLowerCase().includes(q))return false;return true}).sort((a,b)=>{let av=sortKey==="decision"?decisionOf(a.id):a[sortKey],bv=sortKey==="decision"?decisionOf(b.id):b[sortKey];return String(av).localeCompare(String(bv),undefined,{numeric:true})*(sortAsc?1:-1)})}
function renderKPIs(){const done=DATA.filter(r=>r.status==="DONE").length,active=DATA.filter(r=>["IN_PROGRESS","OPEN"].includes(r.status)).length,input=DATA.filter(r=>["NEEDS_INPUT","OPEN_REVIEW","REVIEW","PROPOSED"].includes(r.status)).length,selected=Object.values(decisions).filter(v=>v.decision).length;document.getElementById("k-total").textContent=DATA.length;document.getElementById("k-done").textContent=done;document.getElementById("k-rate").textContent=(done/DATA.length*100).toFixed(1)+"% of tracker";document.getElementById("k-active").textContent=active;document.getElementById("k-input").textContent=input;document.getElementById("k-selected").textContent=selected}
function renderBars(){const counts={};DATA.forEach(r=>counts[r.status]=(counts[r.status]||0)+1);const max=Math.max(...Object.values(counts));document.getElementById("status-bars").innerHTML=Object.entries(counts).sort((a,b)=>b[1]-a[1]).map(([s,n])=>`<div class="bar-row"><div class="bar-label">${esc(s)}</div><div class="track"><div class="fill" style="width:${n/max*100}%;background:${COLORS[s]||"#788399"}"></div></div><div class="bar-count">${n}</div></div>`).join("")}
function renderDecisionSummary(){const counts={unset:0};DATA.forEach(r=>{const d=decisionOf(r.id)||"unset";counts[d]=(counts[d]||0)+1});document.getElementById("decision-summary").innerHTML=Object.entries(counts).sort((a,b)=>b[1]-a[1]).map(([d,n])=>`<div class="decision-pill"><strong>${n}</strong><span>${esc(DECISION_OPTIONS.find(x=>x[0]===d)?.[1]||d)}</span></div>`).join("")}
function renderTable(){const rows=filtered();document.getElementById("visible-count").textContent=`Showing ${rows.length} of ${DATA.length}`;document.getElementById("empty").hidden=rows.length>0;document.getElementById("rows").innerHTML=rows.map(r=>{const saved=decisions[r.id]||{},options=DECISION_OPTIONS.map(([v,l])=>`<option value="${v}" ${saved.decision===v?"selected":""}>${l}</option>`).join("");return `<tr><td class="id">${esc(r.id)}</td><td><span class="badge s-${esc(r.status)}">${esc(r.status)}</span></td><td>${esc(r.priority)}</td><td class="task">${esc(r.task)}<div style="font-size:11px;color:var(--muted);margin-top:5px">${esc(r.owner)}</div></td><td class="impact"><span class="badge i-${esc(r.impact)}">${esc(r.impact)}</span><div>${esc(r.impact_reason)}</div></td><td class="next"><strong>${esc(r.suggested_action)}</strong><div style="color:var(--muted);font-size:12px;margin-top:5px">${esc(r.next)}</div></td><td class="decision-cell"><select data-id="${esc(r.id)}" data-kind="decision">${options}</select><input data-id="${esc(r.id)}" data-kind="note" value="${esc(saved.note||"")}" placeholder="Decision note"></td><td class="details"><details><summary>Show evidence and dependencies</summary><p><strong>Evidence:</strong> ${esc(r.evidence)}</p><p><strong>Dependencies:</strong> ${esc(r.dependencies)}</p></details></td></tr>`}).join("")}
function render(){renderTable();renderKPIs();renderDecisionSummary()}
document.querySelectorAll(".filters input,.filters select").forEach(el=>el.addEventListener("input",renderTable));document.querySelectorAll("th[data-sort]").forEach(th=>th.addEventListener("click",()=>{const key=th.dataset.sort;if(sortKey===key)sortAsc=!sortAsc;else{sortKey=key;sortAsc=true}renderTable()}));document.getElementById("rows").addEventListener("change",e=>{const el=e.target,id=el.dataset.id;if(!id)return;decisions[id]=decisions[id]||{};decisions[id][el.dataset.kind]=el.value;decisions[id].updated_at=new Date().toISOString();if(!decisions[id].decision&&!decisions[id].note)delete decisions[id];saveDecisions();renderTable()});document.getElementById("rows").addEventListener("input",e=>{const el=e.target,id=el.dataset.id;if(!id||el.dataset.kind!=="note")return;decisions[id]=decisions[id]||{};decisions[id].note=el.value;decisions[id].updated_at=new Date().toISOString();if(!decisions[id].decision&&!decisions[id].note)delete decisions[id];saveDecisions()});
document.getElementById("export").addEventListener("click",()=>{const body={schema:"memspine-task-decisions/v1",source_sha256:SOURCE_HASH,exported_at:new Date().toISOString(),decisions};const blob=new Blob([JSON.stringify(body,null,2)+"\n"],{type:"application/json"}),a=document.createElement("a");a.href=URL.createObjectURL(blob);a.download="memspine-task-decisions.json";a.click();URL.revokeObjectURL(a.href);toast("Decisions exported")});document.getElementById("import").addEventListener("change",async e=>{const file=e.target.files[0];if(!file)return;try{const body=JSON.parse(await file.text());if(body.schema!=="memspine-task-decisions/v1"||typeof body.decisions!=="object")throw new Error("Unsupported decision file");decisions=body.decisions;saveDecisions();render();toast(body.source_sha256===SOURCE_HASH?"Decisions imported":"Imported from a different tracker revision")}catch(err){alert(err.message)}finally{e.target.value=""}});document.getElementById("clear").addEventListener("click",()=>{if(!confirm("Clear all browser-saved task decisions and notes?"))return;decisions={};saveDecisions();render();toast("Decisions cleared")});
fillSelect("status","status");fillSelect("priority","priority");fillSelect("program","program");renderBars();render();
</script></body></html>"""
    return template.replace("__TASK_DATA__", payload).replace("__SOURCE_HASH__", digest)


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(build(), encoding="utf-8")
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
