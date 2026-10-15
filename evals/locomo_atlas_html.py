"""HTML template of the LoCoMo problem atlas (LOCAL ONLY: the page carries dataset text, see build_locomo_problem_atlas.py).

``render_html(data_json)`` returns one self-contained page: no network, no external script or font. The data object is
embedded as JSON (already escaped by ``build_forensics_explorer.dumps``).
"""

from __future__ import annotations

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>LoCoMo problem atlas</title>
<style>
:root{--bg:#fafafa;--fg:#1b1b1b;--mut:#666;--line:#d8d8d8;--acc:#0b5cad;--bad:#b3261e;--ok:#1b7f3b;--warn:#8a5a00}
*{box-sizing:border-box}body{font:13px/1.4 system-ui,Segoe UI,sans-serif;margin:0;background:var(--bg);color:var(--fg)}
header{padding:10px 16px;border-bottom:1px solid var(--line);background:#fff;position:sticky;top:0;z-index:5}
h1{font-size:16px;margin:0 0 4px}.sub{color:var(--mut)}nav button{margin:6px 4px 0 0;padding:4px 10px;border:1px solid var(--line);background:#fff;cursor:pointer;border-radius:4px}
nav button.on{background:var(--acc);color:#fff;border-color:var(--acc)}main{padding:12px 16px}
table{border-collapse:collapse;margin:6px 0 14px}th,td{border:1px solid var(--line);padding:3px 7px;text-align:left;vertical-align:top}
th{background:#f0f0f0;position:sticky;top:0}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.pill{display:inline-block;padding:0 6px;border-radius:9px;background:#eee;margin:1px;font-size:11px}
.pill.ok{background:#dff3e4}.pill.bad{background:#fbdcd9}.pill.warn{background:#fdeccb}.pill.acc{background:#dbe9f8}
.mono{font-family:Consolas,monospace;font-size:12px}.filters{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:8px}
select,input[type=text]{padding:3px;border:1px solid var(--line);border-radius:3px}
tr.q{cursor:pointer}tr.q:hover{background:#eef4fb}tr.det td{background:#fcfcfc}
.cell-click{cursor:pointer;color:var(--acc);text-decoration:underline}.hid{display:none}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px;margin:8px 0}
.card{border:1px solid var(--line);background:#fff;padding:6px 10px;border-radius:5px}.card b{font-size:18px;display:block}
.small{font-size:11px;color:var(--mut)}
.cv{display:grid;grid-template-columns:360px 1fr;gap:12px;align-items:start}.cvq{position:sticky;top:96px;max-height:calc(100vh - 110px);overflow:auto;border:1px solid var(--line);background:#fff}
.cvq div{padding:4px 8px;border-bottom:1px solid #eee;cursor:pointer}.cvq div:hover{background:#eef4fb}.cvq div.cur{background:#fff3c4}.cvq .k{border-left:4px solid var(--ok)}.cvq .w{border-left:4px solid var(--bad)}
.sess{margin:10px 0 4px;font-weight:600;border-bottom:1px solid var(--line);padding-bottom:2px}.turn{display:flex;gap:8px;padding:3px 6px;border-left:5px solid transparent;border-bottom:1px solid #f0f0f0;background:#fff}
.turn .id{min-width:56px;font-family:Consolas,monospace;font-size:11px;color:var(--mut)}.turn .sp{min-width:70px;font-weight:600}.turn .tx{flex:1}.turn .cap{color:var(--mut);font-style:italic}
.turn.gnr{border-left-color:#c62828;background:#fde7e5}.turn.gic{border-left-color:#1b7f3b;background:#e0f3e5}.turn.rng{border-left-color:#e0a100;background:#fff6dc}
.bd{display:inline-block;margin:0 2px 2px 0;padding:0 5px;border-radius:8px;font-size:10px;cursor:pointer;border:1px solid}.bd.ok{background:#dff3e4;border-color:#1b7f3b;color:#1b7f3b}.bd.bad{background:#fbdcd9;border-color:#b3261e;color:#b3261e}.bd.cur{outline:2px solid #000}
.lg span{display:inline-block;padding:1px 8px;margin-right:6px;border-left:5px solid}.lg .a{border-color:#c62828;background:#fde7e5}.lg .b{border-color:#1b7f3b;background:#e0f3e5}.lg .c{border-color:#e0a100;background:#fff6dc}
.focus{border:1px solid var(--line);background:#fff;padding:6px 10px;margin-bottom:8px;max-height:42vh;overflow:auto}details.ctx{margin:6px 0}.ans{white-space:pre-wrap}.rk{display:inline-block;min-width:34px;text-align:right}
</style></head><body>
<header><h1>LoCoMo problem atlas <span class="sub" id="meta"></span></h1>
<div class="sub">Local only. Carries dataset text (questions, answers, gold turns). Every number tagged "if it captured X%" is a hypothesis, not a measurement.</div>
<nav id="nav"></nav></header><main id="main"></main>
<script id="data" type="application/json">__DATA__</script>
__CONVS__
<script>
const D=JSON.parse(document.getElementById('data').textContent);
const CAT=['single-hop','multi-hop','temporal','open-domain'];
const SHORT={'in_reference':'ref','built_off_default':'off','built_harness_flag':'harness','not_built':'GAP'};
const $=(h)=>{const t=document.createElement('template');t.innerHTML=h.trim();return t.content.firstChild};
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const F={cls:'',stage:'',cat:'',conv:'',screen:'',sess:'',verdict:'',flip:'',txt:'',relation:'any'};
let FOCUS=null;const CONVC={},CITES={};
let TAB='overview',PAGE=0,OPEN=new Set();
const screenDef={};D.screens.forEach(s=>screenDef[s.id]=s);
const wrongQ=D.q.filter(r=>!r.correct);const QBY={};D.q.forEach(r=>QBY[r.item+' '+r.qid]=r);
document.getElementById('meta').textContent=`${D.run} - ${D.q.length} questions, ${wrongQ.length} wrong - generated ${D.generated}`;
const tabs=[['overview','Overview'],['questions','Questions'],['conv','Conversations'],['sessions','Sessions'],['screens','Screens: expected recovery'],['flips','Measured flips'],['classes','Classes and solutions']];
function nav(){const n=document.getElementById('nav');n.innerHTML='';tabs.forEach(([k,l])=>{const b=$(`<button class="${TAB===k?'on':''}">${l}</button>`);b.onclick=()=>{TAB=k;draw()};n.appendChild(b)})}
function go(tab,f){Object.assign(F,{cls:'',stage:'',cat:'',conv:'',screen:'',sess:'',txt:'',flip:''},f||{});F.verdict=(f&&f.wrong===false)?'':'wrong';delete F.wrong;PAGE=0;TAB=tab;draw()}
function clsPill(c){const m=D.classes[c];return `<span class="pill ${m&&m.heuristic?'warn':'acc'}" title="${esc(m?m.rule:'')}">${esc(c)}${m&&m.heuristic?' (H)':''}</span>`}
function stat(s){return `<span class="pill ${s==='in_reference'?'ok':s==='not_built'?'bad':s==='built_off_default'?'acc':'warn'}">${SHORT[s]||s}</span>`}
function screenTargets(sid){const s=screenDef[sid];return s?{p:s.primary,s:s.secondary}:{p:[],s:[]}}
function matches(r){
  if(F.verdict==='wrong'&&r.correct)return false;
  if(F.verdict==='correct'&&!r.correct)return false;
  if(F.flip&&!(r.screen_flips||[]).some(x=>x.run===F.flip))return false;
  if(F.cls&&r.rclass!==F.cls)return false;
  if(F.stage&&r.stage!==F.stage)return false;
  if(F.cat&&r.category!==F.cat)return false;
  if(F.conv&&r.item!==F.conv)return false;
  if(F.sess&&!r.sessions.some(x=>String(x[0])===String(F.sess)))return false;
  if(F.screen){const t=screenTargets(F.screen);const ok=t.p.includes(r.rclass)||(F.relation==='any'&&t.s.includes(r.rclass));if(!ok)return false}
  if(F.txt){const q=F.txt.toLowerCase();if(!((r.question+' '+r.gold+' '+r.answer+' '+r.item+' '+r.qid+' '+(r.judge_raw||'')).toLowerCase().includes(q)))return false}
  return true}
function overview(){
  const bc={},cxs={},cxc={};
  wrongQ.forEach(r=>{bc[r.rclass]=(bc[r.rclass]||0)+1;cxs[r.rclass+'|'+r.stage]=1+(cxs[r.rclass+'|'+r.stage]||0);cxc[r.rclass+'|'+r.category]=1+(cxc[r.rclass+'|'+r.category]||0)});
  const cl=Object.keys(bc).sort((a,b)=>bc[b]-bc[a]);
  let h=`<div class="grid"><div class="card"><b>${D.q.length}</b>questions</div><div class="card"><b>${wrongQ.length}</b>wrong</div><div class="card"><b>${(100*(D.q.length-wrongQ.length)/D.q.length).toFixed(1)}%</b>accuracy</div><div class="card"><b>${cl.length}</b>root-cause classes</div></div>`;
  h+=`<p class="small">Click a count to open the matching questions. (H) = heuristic class: the rule reads wording or token overlap (hover for the rule).</p><table><tr><th>class</th><th>stage(s)</th>${CAT.map(c=>`<th class="n">${c}</th>`).join('')}<th class="n">total</th><th>solutions (status; screens)</th></tr>`;
  cl.forEach(c=>{
    const st=Object.keys(D.stages).filter(s=>cxs[c+'|'+s]).map(s=>`<span class="cell-click" onclick='go("questions",{cls:"${c}",stage:"${s}"})'>${s} ${cxs[c+'|'+s]}</span>`).join(', ');
    const sols=(D.classes[c].solutions||[]).map(x=>`${x.relation==='primary'?'<b>':''}${esc(x.solution)}${x.relation==='primary'?'</b>':''} ${stat(x.status)} ${x.screens.length?esc(x.screens.join(',')):'<i>no screen</i>'}`).join('<br>')||'<i>'+esc(D.classes[c].no_solution_needed||'none')+'</i>';
    h+=`<tr><td>${clsPill(c)}</td><td>${st}</td>${CAT.map(k=>`<td class="n">${cxc[c+'|'+k]?`<span class="cell-click" onclick='go("questions",{cls:"${c}",cat:"${k}"})'>${cxc[c+'|'+k]}</span>`:''}</td>`).join('')}<td class="n"><span class="cell-click" onclick='go("questions",{cls:"${c}"})'>${bc[c]}</span></td><td class="small">${sols}</td></tr>`});
  h+='</table>';
  if(D.val&&D.val.available)h+=`<p class="small">Classifier vs hand labels (full-persp-loc, ${D.val.n} wrong): exact stage ${D.val.exact}, group-level ${D.val.group}.</p>`;
  return h}
function qFilters(){
  const opt=(arr,sel,lab)=>`<option value="">${lab}: all</option>`+arr.map(x=>`<option ${x===sel?'selected':''}>${esc(x)}</option>`).join('');
  const sessions=F.conv?(D.sessions[F.conv]||[]).map(x=>String(x.s)):[];
  return `<div class="filters"><select id="fw"><option value="">verdict: all</option><option value="wrong" ${F.verdict==='wrong'?'selected':''}>wrong</option><option value="correct" ${F.verdict==='correct'?'selected':''}>correct</option></select>
  <select id="fc">${opt(Object.keys(D.classes).filter(c=>c!=='ok'),F.cls,'class')}</select>
  <select id="fs">${opt(Object.keys(D.stages).filter(s=>s!=='ok'),F.stage,'stage')}</select>
  <select id="fk">${opt(CAT,F.cat,'category')}</select>
  <select id="fv">${opt(D.convs,F.conv,'conversation')}</select>
  <select id="fe">${opt(sessions,String(F.sess),'session')}</select>
  <select id="fx"><option value="">screen: all</option>${D.screens.filter(s=>s.ub_primary>0||s.ub_secondary>0).map(s=>`<option value="${s.id}" ${s.id===F.screen?'selected':''}>${s.id} ${esc(s.title)} (${s.status})</option>`).join('')}</select>
  <select id="ff"><option value="">screen run flips: any</option>${(D.flip_runs||[]).filter(x=>x.gains+x.losses>0).map(x=>`<option value="${x.run}" ${x.run===F.flip?'selected':''}>${x.screen} ${x.mode} +${x.gains}/-${x.losses} ${x.run.replace('smp-','')}</option>`).join('')}</select>
  <select id="fr"><option value="any" ${F.relation==='any'?'selected':''}>primary+secondary</option><option value="primary" ${F.relation==='primary'?'selected':''}>primary only</option></select>
  <input type="text" id="ft" placeholder="search text / id" value="${esc(F.txt)}"><button id="reset">reset</button></div>`}
function bindQF(){
  const m={fc:'cls',fs:'stage',fk:'cat',fv:'conv',fe:'sess',fx:'screen',fr:'relation',ff:'flip'};
  Object.keys(m).forEach(i=>{document.getElementById(i).onchange=e=>{F[m[i]]=e.target.value;if(i==='fv')F.sess='';PAGE=0;draw()}});
  document.getElementById('fw').onchange=e=>{F.verdict=e.target.value;PAGE=0;draw()};
  document.getElementById('ft').oninput=debounce(e=>{F.txt=e.target.value;PAGE=0;draw();const t=document.getElementById('ft');t.focus();t.setSelectionRange(t.value.length,t.value.length)});
  document.getElementById('reset').onclick=()=>{FOCUS=null;go(TAB,{wrong:false})}}
function debounce(fn){let t;return e=>{clearTimeout(t);t=setTimeout(()=>fn(e),350)}}
function rank(v){return v==null?'-':v}
function getConv(item){if(!CONVC[item]){const el=document.getElementById('cv-'+item);CONVC[item]=el?JSON.parse(el.textContent):{sessions:[],ctxtext:{},speakers:[]};const T={};CONVC[item].sessions.forEach(s=>s.turns.forEach(t=>{t.s=s.s;t.date=s.date;T[t.id]=t}));CONVC[item].T=T}return CONVC[item]}
function cites(item){if(!CITES[item]){const m={};D.q.filter(r=>r.item===item).forEach(r=>r.evidence.forEach(t=>(m[t]=m[t]||[]).push(r)));CITES[item]=m}return CITES[item]}
function verdict(c){return c?'<span class="pill ok">correct</span>':'<span class="pill bad">wrong</span>'}
function turnLine(c,id,gold,ctx){const t=c.T[id];const cls=gold.has(id)?(ctx.has(id)?'gic':'gnr'):'rng';
  const txt=c.ctxtext[id]||(t?t.sp+': '+t.t:'(turn not in dataset)');
  return `<div class="turn ${cls}"><span class="id">${esc(id)}</span><span class="tx">${esc(txt)}</span></div>`}
function detail(r,inConv){
  const c=getConv(r.item);const gold=new Set(r.gold_turns.map(g=>g.turn)),ctx=new Set(r.ctx);
  let h=`<div><b>${esc(r.item)} ${esc(r.qid)}</b> ${esc(r.category)} ${verdict(r.correct)} ${r.score==null?'':'score '+r.score}${inConv?'':` <button onclick="focusQ('${r.item}','${r.qid}')">view in conversation</button>`}</div>`;
  h+=`<div><b>Question</b> ${esc(r.question)}</div><div><b>Gold answer</b> ${esc(r.gold)}</div><div class="ans"><b>Model answer</b> ${esc(r.answer)}</div>`;
  h+=`<div class="small"><b>Judge</b> ${r.judge_raw?'<span class="mono">'+esc(r.judge_raw)+'</span>':'not logged (no judge call on this path)'} ${r.judge_prompt?'prompt '+esc(r.judge_prompt):''}</div>`;
  h+=`<div class="small">${r.correct?'':'class '+clsPill(r.rclass)+' stage '+esc(r.stage)+' gap '+esc(r.primary_gap)+' bucket '+esc(r.bucket)+' tags '+esc((r.tags||[]).join(', '))+' - '}evidence ${esc(r.evidence.join(', '))} - sessions ${r.sessions.map(x=>'S'+x[0]+' ('+esc(x[1])+')').join(', ')||'-'}</div>`;
  h+=`<table><tr><th>gold turn</th><th>speaker</th><th>date</th><th>in ctx</th><th>lost at</th><th class="n">vector</th><th class="n">lexical</th><th>extra legs</th><th class="n">fused</th><th class="n">pool</th><th class="n">rerank</th><th class="n">final</th><th>text</th></tr>`;
  r.gold_turns.forEach(g=>{const t=c.T[g.turn];h+=`<tr><td class="mono">${esc(g.turn)}</td><td>${esc(g.speaker)}</td><td>${esc(g.date||g.ts)}</td><td>${g.in_context?'<span class="pill ok">yes</span>':'<span class="pill bad">no</span>'}</td><td>${esc(g.lost_at)}</td><td class="n">${rank(g.vector)}</td><td class="n">${rank(g.lexical)}</td><td class="mono">${esc(Object.entries(g.extra||{}).map(([k,v])=>k+':'+rank(v)).join(' '))}</td><td class="n">${rank(g.fused)}</td><td class="n">${rank(g.pool)}</td><td class="n">${rank(g.rerank)}</td><td class="n">${rank(g.final)}</td><td class="small">${esc(t?t.t:(g.text||''))}${t&&t.cap?' <i>[image: '+esc(t.cap)+']</i>':''}</td></tr>`});
  h+=`</table><div class="small">Ranks are 1-based positions in each leg's logged list ("-" = not in it). fused/pool = candidate pool after fusion, rerank = rerank order, final = kept turns.</div>`;
  h+=`<details class="ctx" ${inConv?'':'open'}><summary><b>Reader context</b>: ${r.n_ctx} turns, ${r.ctx.filter(t=>gold.has(t)).length}/${gold.size} gold in context - source: ${esc(r.ctx_source||'not logged')}</summary>${r.ctx.length?r.ctx.map(id=>turnLine(c,id,gold,ctx)).join('')+[...gold].filter(id=>!ctx.has(id)).map(id=>turnLine(c,id,gold,ctx)).join(''):'<i>no context logged</i>'}<div class="small">Order = the order the reader saw them. Text = the rendered context record (date resolution and image tags as given to the reader). Red rows after the list are gold turns that never reached the context.</div></details>`;
  if(r.screen_flips&&r.screen_flips.length){h+='<div><b>Changed by sample screens</b> (verdict differs from the reference answer)</div><table><tr><th>screen</th><th>run</th><th>reference</th><th>ref answer</th><th>screen answer</th><th></th></tr>'+r.screen_flips.map(f=>`<tr><td>${esc(f.screen)} ${f.mode}</td><td class="small mono">${esc(f.run.replace('smp-',''))}</td><td class="small">${esc(f.ref.replace('smp-ref-scr-pool-protect-jina-','').slice(0,34))} ${f.ref_score?'<span class="pill ok">correct</span>':'<span class="pill bad">wrong</span>'}</td><td>${esc(f.ref_answer)}</td><td>${esc(f.answer)}</td><td>${f.kind==='gain'?'<span class="pill ok">gain</span>':'<span class="pill bad">loss</span>'}</td></tr>`).join('')+'</table>'}
  else h+='<div class="small">No sample-screen run changed this question\'s verdict (or none covered it).</div>';
  if(r.solutions.length){h+='<div><b>Possible solutions</b><ul>'+r.solutions.map(s=>`<li>${s.relation==='primary'?'<b>':''}${esc(s.id)}${s.relation==='primary'?'</b>':''} ${stat(s.status)} - ${esc((D.solutions.find(x=>x.id===s.id)||{}).name)} - keys: <span class="mono">${esc(((D.solutions.find(x=>x.id===s.id)||{}).keys||[]).join('; '))}</span> - screens: ${s.screens.length?s.screens.map(x=>`<span class="cell-click" onclick='go("screens",{screen:"${x}"})'>${x}</span>`).join(' '):'<i>none queued</i>'}</li>`).join('')+'</ul></div>'}
  return h}
function focusQ(item,qid){FOCUS={item,qid};F.conv=item;F.sess='';TAB='conv';draw();const el=document.querySelector('#cvt .turn.gnr[id],#cvt .turn.gic[id]');if(el)setTimeout(()=>el.scrollIntoView({block:'center'}),0)}
function conv(){
  const item=F.conv||D.convs[0];F.conv=item;const c=getConv(item);
  const fr=FOCUS&&FOCUS.item===item?QBY[FOCUS.item+' '+FOCUS.qid]:null;
  const gold=new Set(fr?fr.gold_turns.map(g=>g.turn):[]),ctx=new Set(fr?fr.ctx:[]);
  const qs=D.q.filter(r=>r.item===item&&matches(r));
  let h=qFilters()+`<div class="lg small"><span class="a">gold, not retrieved</span><span class="b">gold in reader context</span><span class="c">in context, not gold</span> Badges on a turn = questions citing it as evidence (green correct, red wrong); click to jump. ${qs.length} questions match the filters.</div>`;
  h+='<div class="cv"><div class="cvq">'+qs.map(r=>`<div class="${r.correct?'k':'w'} ${fr&&fr.qid===r.qid?'cur':''}" onclick="focusQ('${r.item}','${r.qid}')"><span class="mono">${esc(r.qid)}</span> <span class="pill">${esc(r.category)}</span> ${r.correct?'':clsPill(r.rclass)}<br>${esc(r.question)}</div>`).join('')+'</div><div id="cvt">';
  if(fr)h+=`<div class="focus">${detail(fr,true)}</div>`;
  const ci=cites(item);
  c.sessions.forEach(s=>{h+=`<div class="sess">Session ${s.s} - ${esc(s.date)}</div>`;
    s.turns.forEach(t=>{const g=gold.has(t.id),x=ctx.has(t.id);const cls=g?(x?'gic':'gnr'):x?'rng':'';
      const bd=(ci[t.id]||[]).map(r=>`<span class="bd ${r.correct?'ok':'bad'} ${fr&&fr.qid===r.qid?'cur':''}" title="${esc(r.question)}" onclick="event.stopPropagation();focusQ('${r.item}','${r.qid}')">${esc(r.qid)}</span>`).join('');
      h+=`<div class="turn ${cls}" id="t-${t.id.replace(':','_')}"><span class="id">${esc(t.id)}</span><span class="sp">${esc(t.sp)}</span><span class="tx">${esc(t.t)}${t.cap?` <span class="cap">[image: ${esc(t.cap)}${t.iq?'; search: '+esc(t.iq):''}]</span>`:''}</span><span>${bd}</span></div>`})});
  return h+'</div></div>'}
function questions(){
  const rows=D.q.filter(matches);const per=150,pages=Math.max(1,Math.ceil(rows.length/per));if(PAGE>=pages)PAGE=0;
  let h=qFilters()+`<div class="small">${rows.length} questions ${F.screen?`(screen ${F.screen}: classes ${screenTargets(F.screen).p.join(', ')}${F.relation==='any'&&screenTargets(F.screen).s.length?' + secondary '+screenTargets(F.screen).s.join(', '):''})`:''} - page ${PAGE+1}/${pages}
  <button onclick="PAGE=Math.max(0,PAGE-1);draw()">prev</button><button onclick="PAGE=Math.min(${pages-1},PAGE+1);draw()">next</button></div>`;
  h+='<table><tr><th>conv</th><th>qid</th><th>category</th><th>stage</th><th>class</th><th>gold in ctx</th><th>sessions</th><th>question</th><th>solutions / screens</th></tr>';
  rows.slice(PAGE*per,(PAGE+1)*per).forEach(r=>{const id=r.item+' '+r.qid;
    h+=`<tr class="q" data-id="${id}"><td>${esc(r.item)}</td><td class="mono">${esc(r.qid)}</td><td>${esc(r.category)}</td><td>${r.correct?'<span class="pill ok">ok</span>':esc(r.stage)}</td><td>${r.correct?'':clsPill(r.rclass)}</td><td>${r.all_gold_in_context===null?'-':r.all_gold_in_context?'yes':'<span class="pill bad">no</span>'}</td><td class="small">${r.sessions.map(x=>'S'+x[0]).join(' ')}</td><td>${esc(r.question)}</td><td class="small">${r.correct?'':r.solutions.filter(s=>s.relation==='primary').map(s=>esc(s.id)+(s.screens.length?' ['+s.screens.join(',')+']':' [no screen]')).join('<br>')}</td></tr>`;
    if(OPEN.has(id))h+=`<tr class="det"><td colspan="9">${detail(r,false)}</td></tr>`});
  h+='</table>';return h}
function sessions(){
  const conv=F.conv||D.convs[0];
  let h=`<div class="filters"><select id="sc">${D.convs.map(c=>`<option ${c===conv?'selected':''}>${c}</option>`).join('')}</select></div>`;
  h+='<table><tr><th>session</th><th>date</th><th class="n">turns</th><th class="n">questions with gold here</th><th class="n">wrong</th><th>wrong by class</th></tr>';
  (D.sessions[conv]||[]).forEach(s=>{h+=`<tr><td>S${s.s}</td><td>${esc(s.date)}</td><td class="n">${s.n_turns}</td><td class="n">${s.n_q?`<span class="cell-click" onclick='go("questions",{conv:"${conv}",sess:"${s.s}",wrong:false})'>${s.n_q}</span>`:0}</td><td class="n">${s.n_wrong?`<span class="cell-click" onclick='go("questions",{conv:"${conv}",sess:"${s.s}"})'>${s.n_wrong}</span>`:0}</td><td>${Object.entries(s.classes).sort((a,b)=>b[1]-a[1]).map(([c,n])=>clsPill(c)+' '+n).join(' ')}</td></tr>`});
  return h+'</table><p class="small">A question counts under every session that holds one of its gold turns, so rows overlap.</p>'}
function screens(){
  const only=window._openOnly!==false;
  let h=`<div class="filters"><label><input type="checkbox" id="oo" ${only?'checked':''}> only open screens (${D.open_statuses.join(', ')})</label></div><p class="small">Upper bound = wrong questions in the classes the screen is aimed at (primary). 30% / 60% = hypothetical capture. Bundles are unions, not sums. Click a screen id to list its target questions.</p>`;
  h+=`<table><tr><th>#</th><th>screen</th><th>status</th><th>setting</th><th>bundle</th><th class="n">upper bound</th><th class="n">if 30%</th><th class="n">if 60%</th><th class="n">+secondary bound</th><th class="n">est min</th><th>primary classes</th><th>solutions</th></tr>`;
  let i=0;D.screens.filter(s=>(s.ub_primary>0||s.ub_secondary>0)&&(!only||D.open_statuses.includes(s.status))).forEach(s=>{i++;
    h+=`<tr><td>${i}</td><td><span class="cell-click" onclick='go("questions",{screen:"${s.id}"})'>${s.id}</span> ${esc(s.title)}</td><td>${esc(s.status)}</td><td class="mono">${esc(s.setting)}</td><td>${s.bundle?'yes':''}</td><td class="n">${s.ub_primary}</td><td class="n">${s.exp30}</td><td class="n">${s.exp60}</td><td class="n">${s.ub_secondary}</td><td class="n">${s.est_min||''}</td><td class="small">${s.primary.map(clsPill).join(' ')}</td><td class="small">${esc(s.solutions.join(', '))}</td></tr>`});
  return h+'</table>'}
function flips(){
  const f=D.flips;
  let h=`<p class="small">Each sample screen vs its same-slice reference rerun. Reference noise: two reruns of the 80-slice reference differ on ${f.noise_flips_80_ref_pair} of ${f.noise_pair_n} questions. Class = class in the full run.</p><table><tr><th>screen</th><th class="n">n</th><th class="n">ref</th><th class="n">screen</th><th class="n">+</th><th class="n">-</th><th class="n">net</th><th class="n">gain in target</th><th class="n">gain elsewhere</th><th class="n">gain noise</th><th class="n">loss on full-correct</th><th>fixes its class?</th><th>flips</th></tr>`;
  f.screens.forEach(s=>{h+=`<tr><td>${s.screen}</td><td class="n">${s.n}</td><td class="n">${s.ref_correct}</td><td class="n">${s.screen_correct}</td><td class="n">${s.gains}</td><td class="n">${s.losses}</td><td class="n">${s.net>0?'+':''}${s.net}</td><td class="n">${s.gains_primary}</td><td class="n">${s.gains_other+s.gains_secondary}</td><td class="n">${s.gains_noise_full_correct}</td><td class="n">${s.losses_on_full_correct}</td><td>${esc(s.fixes_target)}</td><td class="small">${s.gains_detail.map(d=>`<span class="cell-click pill ok" onclick='openQ("${d.item}","${d.qid}")'>+${d.item.slice(5)}/${d.qid} ${esc(d.class)}</span>`).join(' ')} ${s.losses_detail.map(d=>`<span class="cell-click pill bad" onclick='openQ("${d.item}","${d.qid}")'>-${d.item.slice(5)}/${d.qid} ${esc(d.class)}</span>`).join(' ')}</td></tr>`});
  return h+'</table>'}
function openQ(item,qid){OPEN.add(item+' '+qid);go('questions',{conv:item,txt:qid,wrong:false})}
function classes(){
  let h='<table><tr><th>class</th><th class="n">wrong</th><th>group</th><th>rule</th><th>doc</th><th>solutions</th></tr>';
  Object.entries(D.classes).filter(([c])=>c!=='ok').sort((a,b)=>b[1].n-a[1].n).forEach(([c,m])=>{h+=`<tr><td>${clsPill(c)}</td><td class="n">${m.n}</td><td>${esc(m.group)}</td><td class="small">${esc(m.rule)}</td><td class="small">${esc(m.doc)}</td><td class="small">${(m.solutions||[]).map(x=>esc(x.solution)+' ('+x.relation+')').join('<br>')}</td></tr>`});
  h+='</table><h3>Solutions</h3><table><tr><th>id</th><th>name</th><th>status</th><th>keys</th><th>packages</th><th>screens</th><th>doc refs</th><th>note</th></tr>';
  D.solutions.forEach(s=>{h+=`<tr><td>${esc(s.id)}</td><td>${esc(s.name)}</td><td>${stat(s.status)}</td><td class="mono small">${esc(s.keys.join('; '))}</td><td>${esc(s.packages.join(', '))}</td><td>${s.screens.join(' ')||'<i>none</i>'}</td><td class="small">${s.doc_refs.map(r=>esc(r.file.split('/').pop())+' '+esc(r.section)).join('<br>')}</td><td class="small">${esc(s.note)}</td></tr>`});
  return h+'</table><h3>Structural gaps</h3><ul>'+D.gaps.map(g=>`<li>${clsPill(g.class)} ${esc(g.gap)}: ${esc(g.detail)}</li>`).join('')+'</ul>'}
function draw(){nav();const m=document.getElementById('main');
  m.innerHTML=TAB==='overview'?overview():TAB==='questions'?questions():TAB==='conv'?conv():TAB==='sessions'?sessions():TAB==='screens'?screens():TAB==='flips'?flips():classes();
  if(TAB==='conv'){bindQF()}
  if(TAB==='questions'){bindQF();m.querySelectorAll('tr.q').forEach(tr=>tr.onclick=()=>{const id=tr.dataset.id;OPEN.has(id)?OPEN.delete(id):OPEN.add(id);draw()})}
  if(TAB==='sessions'){document.getElementById('sc').onchange=e=>{F.conv=e.target.value;draw()}}
  if(TAB==='screens'){document.getElementById('oo').onchange=e=>{window._openOnly=e.target.checked;draw()}}}
{const h=location.hash.slice(1);if(tabs.some(t=>t[0]===h))TAB=h}
draw();
</script></body></html>"""


def render_html(data_json: str, conv_blobs: dict[str, str] | None = None) -> str:
    """``conv_blobs``: ``{item: json}``; one ``<script type=application/json id=cv-<item>>`` per conversation, parsed lazily."""
    tags = "\n".join(
        f'<script id="cv-{k}" type="application/json">{v}</script>'
        for k, v in (conv_blobs or {}).items()
    )
    return PAGE.replace("__CONVS__", tags).replace("__DATA__", data_json)
