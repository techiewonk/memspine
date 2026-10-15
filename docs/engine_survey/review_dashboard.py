"""Presentation only: full-width tables over the frozen independent review."""
from html import escape
from bs4 import BeautifulSoup


def dashboard(page, engines, phases, prompts, order, plan):
    soup = BeautifulSoup(page, 'html.parser')

    def fragment(markup):
        return BeautifulSoup(markup, 'html.parser')

    def matrix(rows):
        out = '<div class="scroll matrix-scroll"><table class="matrix"><thead><tr><th scope="col">Feature / phase</th>'
        out += ''.join(f'<th scope="col" data-engine="{n}">{escape(engines[n]["name"])}</th>' for n in order)
        out += '</tr></thead><tbody>'
        for label, values in rows:
            out += '<tr><th scope="row">'+escape(label)+'</th>'
            for n in order:
                v = values[n]
                out += f'<td data-engine="{n}" data-status="{escape(str(v.get("s", "")))}"><b class="badge">{escape(str(v.get("s", "")))}</b><p>{escape(str(v.get("t", "")))}</p><div class="evidence">{escape(str(v.get("e", "")))}</div></td>'
            out += '</tr>'
        return out+'</tbody></table></div>'

    # Replace stacked comparisons with actual side-by-side matrices.
    panel = soup.find(id='phases')
    for old in panel.find_all('details', recursive=False):
        old.decompose()
    panel.append(fragment(matrix([(title+' / '+label, {n: engines[n]['rows'][rid] for n in order}) for _, title, rows in phases for rid, label in rows])))
    panel = soup.find(id='prompts')
    for old in panel.select('details.searchable'):
        old.decompose()
    panel.find('p').insert_after(fragment(matrix([(fid+' · '+label, {n: prompts['engines'][n]['scores'][fid] for n in order}) for fid, label in prompts['features'].items()])))
    layer_ids = list(dict.fromkeys(k for n in order for k in engines[n].get('layers', {})))
    soup.find(id='layers').append(fragment('<h3>Inherited engine layer coverage</h3><p>Original record statuses; these cells are not newly code-certified.</p>'+matrix([(k, {n: {'s': engines[n].get('layers', {}).get(k, 'Unknown')} for n in order}) for k in layer_ids])))

    # Convert source objects to field/value tables without losing any values.
    for dl in reversed(soup.find_all('dl')):
        t = soup.new_tag('table', attrs={'class': 'record'})
        body = soup.new_tag('tbody')
        for dt in list(dl.find_all('dt', recursive=False)):
            dd = dt.find_next_sibling('dd')
            row = soup.new_tag('tr')
            key = soup.new_tag('th', scope='row')
            key.extend(list(dt.contents))
            value = soup.new_tag('td')
            if dd:
                value.extend(list(dd.contents))
            row.extend([key, value]); body.append(row)
        t.append(body); dl.replace_with(t)

    # Each gap, engine, hypothesis and review is one tabular row, retaining its full detail.
    for panel_id in ['gaps', 'engines', 'questions', 'claude', 'codex']:
        panel = soup.find(id=panel_id)
        if not panel:
            continue
        details = panel.find_all('details', recursive=False)
        if not details:
            continue
        t = fragment('<div class="scroll"><table class="review-table"><thead><tr><th scope="col">Record / finding</th><th scope="col">Full evidence, assessment and proposed solution</th></tr></thead><tbody></tbody></table></div>')
        body = t.find('tbody')
        details[0].insert_before(t)
        for d in details:
            row = soup.new_tag('tr'); key = soup.new_tag('th', scope='row'); val = soup.new_tag('td')
            summary = d.find('summary', recursive=False)
            key.extend(list(summary.contents)); summary.decompose()
            val.extend(list(d.contents)); row.extend([key, val]); body.append(row); d.decompose()

    for panel_id in ['decisions', 'flows']:
        panel = soup.find(id=panel_id)
        articles = panel.find_all('article', recursive=False)
        if not articles:
            continue
        t = fragment('<div class="scroll"><table><thead><tr><th scope="col">Stage / correction</th><th scope="col">Current behaviour, evidence and enhancement</th></tr></thead><tbody></tbody></table></div>')
        body = t.find('tbody')
        articles[0].insert_before(t)
        for article in articles:
            row = soup.new_tag('tr'); key = soup.new_tag('th', scope='row'); val = soup.new_tag('td')
            heading = article.find(['h3', 'h4'])
            if heading:
                key.extend(list(heading.contents)); heading.decompose()
            val.extend(list(article.contents)); row.extend([key, val]); body.append(row); article.decompose()

    for text in soup.find_all(string=True):
        if 'No browser render verification or engine tests are claimed.' in text:
            text.replace_with(str(text).replace('No browser render verification or engine tests are claimed.', 'No browser render verification or model-based evaluation is claimed. The executed unit tests and synthetic probes are listed in Codex verification.'))
    gap_rows = '<h3>Gap and solution comparison</h3><p>Planning text below is inherited. Consult the Codex adjudication in the full evidence table underneath before treating a status or proposed solution as verified.</p><div class="scroll"><table class="gap-matrix"><thead><tr>'
    fields = [('id','Gap'),('title','Feature'),('area','Phase'),('status','Inherited status'),('now','Current behaviour as previously recorded'),('proposal','Proposed solution'),('gate','Acceptance gate'),('wave','Wave'),('effort','Effort')]
    gap_rows += ''.join('<th scope="col">'+label+'</th>' for _,label in fields)+'</tr></thead><tbody>'
    for gap in plan['gaps']:
        gap_rows += '<tr>'+''.join('<td>'+escape(str(gap.get(key,'')))+'</td>' for key,_ in fields)+'</tr>'
    gap_rows += '</tbody></table></div><h3>Full records and current Codex adjudications</h3>'
    soup.find(id='gaps').find('p').insert_after(fragment(gap_rows))
    for script in soup.find_all('script'):
        script.decompose()
    for table in soup.find_all('table'):
        if not table.find_parent('table'):
            table['class'] = table.get('class', [])+['filter-table']
    controls = '''<div class="toolbar"><input id="filter" type="search" placeholder="Search table rows, claims, evidence or gap IDs" aria-label="Search all table rows"><button id="fullscreen">Full screen</button><button id="all-sections">Show all sections</button><button id="reset">Reset filters</button><label><input id="differences" type="checkbox"> Different statuses only (matrices)</label><label><input id="compact" type="checkbox"> Compact rows</label><span id="result-count" role="status"></span></div><details class="engine-picker"><summary>Choose engine columns for side-by-side comparison</summary><button id="select-all">All engines</button><button id="select-core">MemSpine + Combined + Mem0 + EverMemOS</button><div class="engine-options">'''
    controls += ''.join(f'<label><input type="checkbox" data-choice="{n}" checked> {escape(engines[n]["name"])}</label>' for n in order)
    controls += '</div></details><p class="scope-note">15 inherited engine records · 57 phase features · 23 prompt features · 41 gaps. Codex verification covers 43 scoped source claims across 16 engines including Graphiti; 1,200 inherited cells are not individually certified. Select a tab; click a column heading to sort.</p>'
    soup.find(id='filter').decompose()
    soup.find('header').append(fragment(controls))
    style = soup.new_tag('style'); style.string = CSS; soup.find('header').insert_before(style)
    script = soup.new_tag('script'); script.string = JS; soup.append(script)
    return str(soup)


CSS = '''
.gap-matrix{min-width:2300px}.gap-matrix td:nth-child(5),.gap-matrix td:nth-child(6){min-width:430px}.gap-matrix td:nth-child(7){min-width:300px}
*{box-sizing:border-box}header,main{width:100%;max-width:none;padding:18px 24px;margin:0}header{background:#112b41;color:#f4f8fc}header h1{font-size:26px;margin:0}header a{color:#dae9f5}header p{margin:8px 0}nav{gap:5px;margin:16px 0}nav a{padding:7px 12px;text-decoration:none;border:1px solid #466076;border-radius:5px}nav a[aria-current=page]{background:#fff;color:#143d60}section{margin:0 0 18px;padding:18px;min-width:0}section h2{margin-top:0;font-size:22px}.toolbar{display:flex;align-items:center;flex-wrap:wrap;gap:10px}.toolbar input[type=search]{width:min(550px,100%);margin:0}.toolbar input[type=checkbox],.engine-options input{width:auto;margin:0}button{padding:8px 12px;border:1px solid #aac0d0;background:#fff;color:#17394f;border-radius:4px;cursor:pointer}button:focus-visible,a:focus-visible,input:focus-visible{outline:3px solid #dd9c34;outline-offset:2px}.engine-picker{border-color:#466076}.engine-options{display:flex;flex-wrap:wrap;gap:12px;margin-top:12px}.scope-note{font-size:13px;color:#cbdce9}.scroll{max-height:72vh;overflow:auto;position:relative;border:1px solid #ccd7e2}table{border-collapse:separate;border-spacing:0}td,th{border-width:0 1px 1px 0;overflow-wrap:anywhere}thead th{position:sticky;top:0;z-index:3;background:#e7eff7}table.filter-table>tbody>tr>:first-child{position:sticky;left:0;z-index:1;background:#f0f5fa;min-width:210px;max-width:300px}table.filter-table>thead>tr>:first-child{left:0;z-index:4}.matrix{table-layout:fixed;width:max-content;min-width:100%}.matrix th,.matrix td{width:320px;min-width:320px;max-width:320px}.matrix tr>:first-child{width:230px;min-width:230px;max-width:230px}.matrix p{margin:8px 0}.badge{display:inline-block;border:1px solid #aabac8;background:#f1f6fa;border-radius:4px;padding:1px 8px}.evidence{font-size:12px;line-height:1.5;color:#516781}.record{font-size:13px}.record th{width:170px;min-width:100px;background:#f4f7fa}.record td{min-width:160px}.record .record th{position:static}.record ul{padding-left:18px}tbody tr:hover>td{background:#f8fbfd}.compact td,.compact th{padding:5px 8px}.compact table{font-size:12px}th button{font:inherit;font-weight:650;text-align:left;border:0;background:transparent;padding:0;width:100%}.step{display:inline-block;vertical-align:top;margin:4px;max-width:420px}.arrow{display:inline-block}.empty-note{padding:16px;color:#6b4b17;background:#fff6db}pre{max-width:100%;font-size:13px}html:fullscreen{overflow:auto;background:#f3f6fa}@media(max-width:700px){header,main{padding:12px}.matrix th,.matrix td{width:270px;min-width:270px}.matrix tr>:first-child{width:160px;min-width:160px;max-width:160px}}@media print{header{background:white;color:black}.toolbar,.engine-picker,nav{display:none!important}main>section[hidden]{display:block!important}.scroll{max-height:none;overflow:visible}.matrix{width:100%;table-layout:auto}.matrix td,.matrix th{min-width:0;width:auto;font-size:8px}th,td{position:static!important}section{break-before:page}}
'''

JS = '''
const panels=[...document.querySelectorAll('main>section')];
const tables=[...document.querySelectorAll('table.filter-table')];
const query=document.getElementById('filter');
let active='phases';
function show(id){active=id;panels.forEach(p=>p.hidden=id!=='all'&&p.id!==id);document.querySelectorAll('nav a').forEach(a=>{if(a.hash==='#'+id)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});apply();}
function apply(){
 const q=query.value.trim().toLowerCase();const selected=new Set([...document.querySelectorAll('[data-choice]:checked')].map(x=>x.dataset.choice));
 document.querySelectorAll('[data-engine]').forEach(c=>c.hidden=!selected.has(c.dataset.engine));
 let shown=0,total=0;
 tables.forEach(t=>{let visible=0;const rows=[...t.tBodies[0].rows];rows.forEach(r=>{
  const cells=[...r.cells].filter(c=>!c.hidden);const text=cells.map(c=>c.textContent).join(' ').toLowerCase();
  const statuses=cells.filter(c=>c.dataset.engine).map(c=>c.dataset.status);
  const diff=!t.classList.contains('matrix')||!document.getElementById('differences').checked||new Set(statuses).size>1;
  r.hidden=!(text.includes(q)&&diff);if(!r.hidden)visible++;
 });
 let note=t.parentElement.querySelector(':scope > .empty-note');if(!note){note=document.createElement('p');note.className='empty-note';t.parentElement.append(note);}note.textContent='No rows match these filters.';note.hidden=visible>0;
 if(!t.closest('section').hidden){shown+=visible;total+=rows.length;}
 });document.getElementById('result-count').textContent=shown+' / '+total+' table rows shown';
}
document.querySelectorAll('nav a').forEach(a=>a.addEventListener('click',()=>show(a.hash.slice(1))));
window.addEventListener('hashchange',()=>{const id=location.hash.slice(1);if(panels.some(p=>p.id===id))show(id);});
document.getElementById('all-sections').onclick=()=>show('all');
query.oninput=apply;document.getElementById('differences').onchange=apply;
document.querySelectorAll('[data-choice]').forEach(c=>c.onchange=apply);
document.getElementById('select-all').onclick=()=>{document.querySelectorAll('[data-choice]').forEach(c=>c.checked=true);apply();};
document.getElementById('select-core').onclick=()=>{document.querySelectorAll('[data-choice]').forEach(c=>c.checked=['memspine','combined','mem0','evermemos'].includes(c.dataset.choice));apply();};
document.getElementById('compact').onchange=e=>document.body.classList.toggle('compact',e.target.checked);
document.getElementById('reset').onclick=()=>{query.value='';document.getElementById('differences').checked=false;document.getElementById('select-all').click();};
const fs=document.getElementById('fullscreen');fs.onclick=async()=>{try{if(document.fullscreenElement)await document.exitFullscreen();else if(document.documentElement.requestFullscreen)await document.documentElement.requestFullscreen();else throw Error();}catch{document.getElementById('result-count').textContent='Use your browser’s full-screen command (usually F11).';}};
document.addEventListener('fullscreenchange',()=>fs.textContent=document.fullscreenElement?'Exit full screen':'Full screen');
tables.forEach(t=>{if(!t.tHead)return;[...t.tHead.rows[0].cells].forEach((h,i)=>{const b=document.createElement('button');b.textContent=h.textContent;b.title='Sort by '+h.textContent;h.textContent='';h.append(b);b.onclick=()=>{const asc=h.getAttribute('aria-sort')!=='ascending';[...t.tHead.rows[0].cells].forEach(x=>x.removeAttribute('aria-sort'));h.setAttribute('aria-sort',asc?'ascending':'descending');[...t.tBodies[0].rows].sort((a,b)=>a.cells[i].textContent.localeCompare(b.cells[i].textContent,undefined,{numeric:true})*(asc?1:-1)).forEach(r=>t.tBodies[0].append(r));};});});
const initial=location.hash.slice(1);show(panels.some(p=>p.id===initial)?initial:'phases');
'''
