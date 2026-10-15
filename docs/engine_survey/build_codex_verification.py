"""Codex-authored adjudications from direct source reading, not inferred certification.

Hashes and line ranges bind the review to inspected source. Merely finding a path never
marks its claim verified. Third-party implementations are only read, never imported/run.
"""
from pathlib import Path
import hashlib
import json
import subprocess

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
OUT=HERE/'independent_review_2026-10-11'
CLONES=Path('C:/Users/hembad/AppData/Local/Temp/claude/C--Users-hembad-Documents-GitHub-memory-research/740f1bef-daa9-480a-95a1-041d8fe2753f/scratchpad/clones')

# Claim text is deliberately narrower than many inherited survey rows.
ENGINES=[
('memspine','memspine','MemSpine',[
 ('Window fact validation accepts a matching normalised span; edge validation checks cited indices without receiving source text. A synthetic unsupported assertion passes each gate.','src/memspine/workers/windows.py',199,264),
 ('Graph keys omit kind in plain mode. Dated keys add day/numerals, not a distinct occurrence ID. Function probes reproduce collisions.','src/memspine/workers/pipelines.py',1882,1896),
 ('Packing stops on an oversize candidate after selecting an item; the first oversize item is intentionally allowed. Existing tests encode that contract.','src/memspine/core/policies/assembly.py',283,309),
 ('Resource ingest re-extracts and re-chunks the document, but skips writing unchanged chunks by path/content fingerprint. Whole-document rewrite is an overstatement.','src/memspine/memories/resource/store.py',60,102),
 ('A relevance-gated, per-subject profile card already has a token allowance. G-W30 must specify the remaining typed persistent-card work.','src/memspine/engine.py',8289,8355),
]),
('memu','memu','MemU',[
 ('Commit plans first, resolves embeddings, then performs storage writes. This protects against embedding failure, not arbitrary mid-write storage failure.','src/memu/app/agentic.py',360,391),
 ('Bridge advances the snapshot and pending session cursor after backend commit returns; cleanup happens later. This is staged progress, not proof of a globally atomic commit.','src/memu/hosts/bridging/pipeline.py',139,175),
 ('The inspected retrieval function embeds the query and gathers segment/file/resource hits without an extraction or answer-model call. Host-agent work is outside that function.','src/memu/app/agentic.py',190,230),
]),
('mnemon','mnemon','Mnemon',[
 ('Record mutation holds a lock, validates revisions, fsyncs a temporary file and renames it. Full platform power-loss guarantees, including directory sync, were not established.','src/sdk/source/records.ts',102,127),
 ('Consolidation attempts a reply twice, then optionally splits the batch. If both halves parse-fail, failures are counted and the original pending batch can still be advanced; a thrown exception restores the memory snapshot.','plugins/dsh-mnemon-strategy-jev-optmem/src/natural.ts',1094,1117),
 ('Consolidated facts append dated values and conflict flags under a lowercased key. References are range-filtered; the inspected function does not require every accepted item to have a nonempty reference list.','plugins/dsh-mnemon-strategy-jev-optmem/src/consolidation.ts',128,156),
]),
('lightmem','lightmem','LightMem',[
 ('The write returns before extraction when no segments or no buffer trigger exist. Durability of buffered input is separate from fact insertion.','src/lightmem/memory/lightmem.py',303,328),
 ('Offline update changes memory text and sends the old vector to update; delete invokes the vector-store delete method.','src/lightmem/memory/lightmem.py',594,626),
 ('Extraction exceptions return an empty cleaned_result and no usage; no retry is present in the inspected wrapper.','src/lightmem/factory/memory_manager/openai.py',338,375),
 ('Pre-compression and topic segmentation both default to false in BaseMemoryConfigs.','src/lightmem/configs/base.py',16,27),
]),
('memori','memori','Memori',[
 ('Cloud augmentation is an HTTP request with configured attempts and backoff; background failures are logged. Hosted prompt and internal model-call count cannot be verified from this SDK.','memori/memory/augmentation/_handler.py',74,106),
 ('The inspected local candidate ranking combines dense and lexical scores; this is not evidence of how the hosted service ranks.','memori/search/_core.py',116,150),
]),
('combined','combined-src','Combined Graphiti + SimpleMem',[
 ('The API appends graphiti/simplemem to stored_in after awaited calls without checking their return values. SimpleMem fan-out is assistant-only and errors are logged.','src/api/memory/session_service.py',320,355),
 ('Extraction loops over max_retries; on final failure migration raises ExtractionError while live mode returns an empty list. The configured attempt count is not universally three.','module/dev_mem/short_term/memory_builder.py',322,368),
]),
('honcho','honcho','Honcho',[
 ('Deriver enqueue is scheduled as a FastAPI BackgroundTask, with separate immediate embedding scheduling. A response is not a synchronous guarantee that these tasks finished.','src/routers/messages.py',153,181),
 ('Representations carry the latest message timestamp and save into observer/observed collections. Empty observations are logged.','src/deriver/deriver.py',207,246),
]),
('cognee','cognee','Cognee',[
 ('Graph text assembly deduplicates nodes by node.id, then includes full text for each retained node. The inherited claim that every edge repeats a whole chunk is incorrect for this function.','cognee/modules/graph/utils/resolve_edges_to_text.py',32,70),
 ('Contradiction detection defaults off. Summary method defaults llm, with an alternative from_extraction mode.','cognee/modules/cognify/config.py',12,29),
 ('Session caching and auto_feedback default true, with concurrent session search and a seven-day TTL. These settings alone do not reproduce the hosted benchmark token count.','cognee/infrastructure/databases/cache/config.py',53,70),
]),
('mirix','mirix','MIRIX',[
 ('Queue type defaults to memory; Kafka is an environment-selectable alternative. The default number of workers is one.','mirix/queue/config.py',8,46),
 ('Selected memory updates run through gather(return_exceptions=True); after all finish, a failed result raises. Successful siblings are not rolled back by this function.','mirix/functions/function_sets/memory_tools.py',1995,2012),
]),
('memmachine','memmachine','MemMachine',[
 ('Episode, episodic-memory and semantic-memory work run concurrently; results are checked after gather. The code explicitly notes unresolved partial-write repair.','packages/server/src/memmachine_server/main/memmachine.py',791,822),
 ('delete_history explicitly removes citation and ingested-history rows. This function does not explicitly delete feature rows; complete erasure requires tracing schema cascades and all callers.','packages/server/src/memmachine_server/semantic_memory/storage/sqlalchemy_pgvector_semantic.py',549,563),
]),
('mem0','mem0','Mem0 OSS',[
 ('The inspected additive extraction path passes existing memories and recent messages into one LLM request. Provider failure raises; parsing failure becomes an empty extraction with messages saved.','mem0/memory/main.py',940,991),
 ('Deleting a vector writes its previous text to the history store as DELETE with is_deleted=1. This is a retention mechanism, not complete text erasure.','mem0/memory/main.py',2114,2142),
]),
('memos','memos','MemOS',[
 ('The inner fine-string extractor catches a generation exception and constructs a raw-text fallback memory. Therefore not every extraction error loses content.','src/memos/mem_reader/multi_modal_struct.py',499,519),
 ('A fine-transfer exception returns from the scheduler handler before deletion.','src/memos/mem_scheduler/task_schedule_modules/handlers/mem_read_handler.py',171,184),
 ('The handler can reach raw/working deletion after a zero-result branch; deletion also depends on the memory-version switch. This is a conditional path, not a runtime reproduction of every malformed reply.','src/memos/mem_scheduler/task_schedule_modules/handlers/mem_read_handler.py',392,418),
]),
('nemori','nemori','Nemori',[
 ('The episode generator catches ordinary exceptions and returns a raw-transcript fallback. Claude’s statement that an episode-generation exception simply propagates was incomplete.','nemori/llm/generators/episode.py',65,84),
 ('The fallback retains source messages and marks fallback=true; it does not supply an embedding in this constructor.','nemori/llm/generators/episode.py',131,143),
 ('The system marks buffered IDs processed after processing groups; semantic-generation failures are caught and logged. A retained episode and missing semantic facts are distinct outcomes.','nemori/core/memory_system.py',153,195),
]),
('evermemos','evermemos-everos','EverMemOS / EverOS',[
 ('Memorize awaits the user pipeline, and in agent mode gathers user and agent pipelines, before constructing the result. No boundary cells returns early.','src/everos/service/memorize.py',271,299),
 ('Atomic facts are an offline strategy triggered by EpisodeExtracted; extraction consumes episode text and writes facts with episode parent IDs. This is not direct extraction from all raw turns.','src/everos/memory/strategies/extract_atomic_facts.py',38,77),
]),
('hindsight','hindsight','Hindsight',[
 ('The extraction wrapper recursively splits on output overflow, but returns empty facts when splitting cannot progress. One call per chunk is not a universal total and retry is not a guarantee of complete extraction.','hindsight-api-slim/hindsight_api/engine/retain/fact_extraction.py',2499,2620),
 ('Recall defaults max_tokens to 4096 and exposes separate chunk/source-fact allowances. prefer_observations defaults false; a single number is not automatically a cap on every included payload.','hindsight-api-slim/hindsight_api/engine/memory_engine.py',8771,8803),
 ('Retrieval result structure represents semantic, BM25, graph and temporal lanes, with graph retrieval delegated to the selected store.','hindsight-api-slim/hindsight_api/engine/search/retrieval.py',37,87),
]),
('graphiti','graphiti','Graphiti standalone',[
 ('Single-episode extraction and bulk extraction are different entry points. Bulk combined extraction defaults true; a universal two-call extraction claim is incorrect.','graphiti_core/graphiti.py',1094,1128),
 ('With no candidate edges, the resolver skips dedupe and runs timestamp/optional attribute work; verbatim matching text plus endpoints can reuse an edge and add episode provenance.','graphiti_core/utils/maintenance/edge_operations.py',904,957),
]),
]

# All 41 gap statuses are adjudicated; absence claims are scoped, never universal proofs.
GAPS={
'G-W4':('partial-confirmed','Window job passes content/participants/turn_count, not a known-facts set. Ordering is per pipeline worker; cross-process ordering is not established.','src/memspine/workers/windows.py',698,743),
'G-W26':('confirmed','Episode summary is requested but deposit uses title+narrative; facts-only modes do not request it.','src/memspine/workers/windows.py',883,913),
'G-W27':('confirmed-with-scope','Window jobs/pending edges are process-local. Raw durability depends on configured storage/event-log mode; avoid “always durable” for in-memory deployments.','src/memspine/workers/windows.py',320,337),
'G-W36':('partial-confirmed','Citations use line numbers; invalid indices are filtered. This does not verify the universal claim that no other prompt exposes a record ID.','src/memspine/workers/windows.py',186,225),
'G-W31':('partial-confirmed','Window transcript is sent as content; optional pre-compression is not in the inspected job. Mean token counts are not remeasured.','src/memspine/workers/windows.py',698,730),
'G-CALL2':('confirmed','Default session-close implementation emits a marker and returns stub unless a handler is supplied.','src/memspine/workers/pipelines.py',3669,3699),
'G-W30':('correction','A per-subject card with a token allowance already exists; “no card or caps” is too broad. Separate it from the proposed persistent typed card.','src/memspine/engine.py',8289,8355),
'G-W1':('partial-confirmed','Episode deposit is implemented in the optional window path. A dedicated retrieval-leg absence was not proven across all extensions.','src/memspine/workers/windows.py',883,913),
'G-W29':('partial-confirmed','Semantic merge retains corroboration and modifies salience. The proposed consolidated observation layer is not proved by those hooks.','src/memspine/memories/semantic/store.py',280,309),
'G-W38':('conditional-risk','Manual sleep and scheduler invoke cycles through separate paths; no common lock was found in those paths. Overlap was not reproduced.','src/memspine/engine.py',13314,13334),
'G-W39':('not-found-in-scope','No standing-answer/mental-model implementation found in src/memspine search. An absence search is weaker than a full extension audit.','src/memspine/config/schema.py',1180,1215),
'G-W40':('confirmed','Semantic merge adds importance 0.1 and utility 0.05. This is existing reinforcement; optimal ranking weights are unmeasured.','src/memspine/memories/semantic/store.py',280,309),
'G-W25':('reproduced-contract-limitation','Actual assembly stops early and permits a first oversize item. This is the current documented contract, not a failing existing test; hard-cap design needs a contract change.','src/memspine/core/policies/assembly.py',283,309),
'G-W28':('confirmed','rerank_context adds neighbours before the reranker; blanket “missing contextual rerank” is false.','src/memspine/engine.py',5734,5750),
'G-W33':('correction','Relative retrieval uses active_as_of() when provided, otherwise read clock. It is not exclusively read-clock anchored.','src/memspine/engine.py',2430,2444),
'G-W6':('confirmed','entity_leg ranks name matches without frequency damping; entity_expand_leg has max_share damping.','src/memspine/core/temporal_query.py',745,825),
'G-W8':('confirmed-with-scope','facts_to_sources lifts atomic_fact-tagged records to live parents with same score and dedup. It does not lift every derived type.','src/memspine/engine.py',8357,8386),
'G-W41':('partial-confirmed','Read-time chain/ledger and agentic modes are configuration choices. A new per-call tier design remains a proposal.','src/memspine/config/schema.py',1180,1210),
'G-W45':('partial-confirmed','Window prompt has a static system template plus dynamic content. Prefix-cache speed benefit was not measured here.','src/memspine/prompts/defaults/window_extract.yaml',12,74),
'G-W13':('partial-confirmed','Window counters exist; comprehensive per-layer cost/failure accounting is a larger proposal.','src/memspine/workers/windows.py',914,969),
'G-W46':('correction','Ingest reparses/rechunks the file but skips unchanged chunk writes by path/fingerprint. Needed: changed-chunk derivation plus stale-chunk retirement.','src/memspine/memories/resource/store.py',60,102),
'G-W47':('confirmed-with-scope','Explicit like/dislike/note API exists. No implicit conversation-feedback extractor was found in the inspected interface.','src/memspine/engine.py',11620,11658),
'G-W48':('partial-confirmed','Subject-aware views/cards exist; they are not a complete per-observer belief store. Not every turn is guaranteed these tags in every configuration.','src/memspine/engine.py',8289,8330),
'BD-1':('policy-not-performance-proof','Optional agentic/read-time components exist; one screen cannot establish a universal ban on decider reads.','src/memspine/config/schema.py',1180,1215),
'BD-2':('partial-confirmed','Skills and successful plans have staged promotion APIs. Keep policy about model-authored lessons separate from capability.','src/memspine/engine.py',11795,11841),
'BD-3':('architecture-choice','Rebuild replays projections from event storage. Human-editable Markdown as source of truth is a policy choice, not a proven disadvantage in all products.','src/memspine/engine.py',13357,13366),
'BD-4':('partial-confirmed','Graph writes route through semantic supersession machinery, but key short-circuits can bypass it. Do not certify universal history correctness.','src/memspine/workers/pipelines.py',2198,2248),
'BD-5':('correction','Engine configuration supports OpenAI/Bedrock/Vertex/Azure and local backends. Local-only evaluation is a project policy, not engine-only capability.','src/memspine/config/schema.py',193,210),
'BD-6':('partial-confirmed','auto_watch hook exists for mined plans; this is distinct from proving correctness of future-event predictions.','src/memspine/engine.py',14501,14524),
'BD-7':('not-found-in-scope','No KV-activation memory implementation found in src/memspine search. This does not rule out provider-side inference caching.','src/memspine/config/schema.py',193,215),
'BD-8':('partial-confirmed','Asset precompute and cached evidence tooling exist. Installed model availability was not verified and no vision calls were made.','evals/precompute_assets.py',1,25),
'G-P1':('confirmed-in-window-scope','Window prompt has no same-language rule; defaults search found no explicit same/original-language instruction. Quality effect remains untested.','src/memspine/prompts/defaults/window_extract.yaml',12,68),
'G-P2':('confirmed','Window prompt conveys plans/wishes in prose; speaker/subject schema fields are not consumed by the fact deposit, though speaker tags can be inherited from parents.','src/memspine/workers/windows.py',829,870),
'G-P3':('partial-confirmed','Window prompt specifies episode lengths, no word limit on fact value. A global hard word cap is not justified by code.','src/memspine/prompts/defaults/window_extract.yaml',33,65),
'G-P4':('partial-confirmed','Fact/edge validators independently drop malformed items; diagnostic repair requires additional orchestration. Successful existing tests do not establish repair quality.','src/memspine/workers/windows.py',199,264),
'G-P5':('correction','Gold lint includes harness files and known single-token speaker names. Its automatic answer harvesting requires multiword capitalised answers and its prompt glob omits partials.','tests/unit/test_prompt_gold_lint.py',22,114),
'G-P6':('reproduced','Year and month parse to first day; probes show 2023→Jan 1 and 2023-05→May 1. Precision is lost in the returned datetime.','src/memspine/workers/pipelines.py',2912,2935),
'G-P7':('proposal','Confidence exists in prompt, but fact deposit ignores it. A new importance label and its usefulness have not been validated.','src/memspine/workers/windows.py',829,870),
'G-W49':('proposal-with-prerequisites','Existing window path is not the proposed unified schema. Duplicate representation is possible, not guaranteed for every relation. Prompt speed/token figures remain unverified measurements.','src/memspine/prompts/defaults/window_extract.yaml',33,57),
'G-W50':('reproduced-partly','Function probes reproduce kind and same-day occurrence collisions. End-to-end migration and state-return behaviour still need dedicated fixtures.','src/memspine/workers/pipelines.py',1882,1896),
'G-E1':('confirmed','Missing blind datasets are skipped before verdict; positive available datasets can suffice. This is an evaluation gate defect, separate from model quality.','evals/eval_blind.py',101,120),
}

def evidence(base,path,start,end):
    p=base/path
    data=p.read_bytes();lines=data.decode('utf-8').splitlines()
    assert 1<=start<=end<=len(lines),(p,start,end,len(lines))
    return dict(path=path,line_start=start,line_end=end,sha256=hashlib.sha256(data).hexdigest(),range_sha256=hashlib.sha256('\n'.join(lines[start-1:end]).encode()).hexdigest())

def main():
    engine_checks=[]
    for slug,folder,name,checks in ENGINES:
        base=ROOT if slug=='memspine' else CLONES/folder
        commit=subprocess.check_output(['git','-C',str(base),'rev-parse','HEAD'],text=True).strip() if (base/'.git').exists() else 'embedded source snapshot; per-file hashes'
        dirty=subprocess.run(['git','-C',str(base),'diff','--quiet','--','src','mem0','memori','nemori','mirix','cognee','graphiti_core','hindsight-api-slim','plugins','packages'],capture_output=True).returncode!=0 if (base/'.git').exists() else None
        engine_checks.append(dict(slug=slug,name=name,clone=folder,commit=commit,tracked_source_diff_detected=dirty,reviewer='Codex direct source inspection',scope='Listed functions/ranges only. Not all 57 survey rows or benchmark scores certified.',checks=[dict(claim=c,status='source-verified scoped claim',evidence=evidence(base,p,a,b)) for c,p,a,b in checks]))
    plan=json.loads((OUT/'sources/gap_plan.json').read_text(encoding='utf-8'))
    assert set(GAPS)=={g['id'] for g in plan['gaps']}
    gap_checks=[dict(id=g['id'],title=g['title'],status=GAPS[g['id']][0],codex_finding=GAPS[g['id']][1],evidence=evidence(ROOT,*GAPS[g['id']][2:]),solution_status='Proposal; improvement not established by source inspection.') for g in plan['gaps']]
    rv=json.loads((OUT/'review_decisions.json').read_text(encoding='utf-8'))
    limited={'RV07':'conditional cache/forget risk; not reproduced','RV10':'arithmetic/document qualification; token and latency claims unverified','RV12':'historical run interpretation; no benchmark rerun','RV13':'design hypothesis; not an implemented improvement','RV14':'structural audit; not 1,200 semantic verifications','RV15':'methodology requirement; not source-proven performance','RV20':'scoped code checks plus unverified historical token figures','RV21':'conditional scope/erasure risk; full traces incomplete','RV22':'document cross-reference check, not runtime behaviour'}
    decisions=[dict(id=r['id'],status=limited.get(r['id'],'direct source inspection; scope and proposed gates remain qualified'),proposed_solution_verified_effective=False) for r in rv]
    inherited=[]
    for slug,_,_,_ in ENGINES:
        p=OUT/'sources'/(slug+'.json')
        if not p.exists():continue
        rec=json.loads(p.read_text(encoding='utf-8'))
        for rid,row in rec['rows'].items():
            inherited.append(dict(engine=slug,cell=rid,status='inherited; full row not individually certified by Codex',source_evidence=row.get('e','')))
    pm=json.loads((OUT/'sources/prompt_matrix.json').read_text(encoding='utf-8'))
    for slug,record in pm['engines'].items():
        for fid,row in record['scores'].items():
            inherited.append(dict(engine=slug,cell=fid,status='inherited prompt score; not independently rederived',source_evidence=row.get('e','')))
    result=dict(reviewer='Codex',date='2026-10-11',method='Direct source reads and local synthetic MemSpine probes. No third-party code installed or executed. No new Claude agent used for this verification.',engine_count=len(engine_checks),source_claim_count=sum(len(e['checks']) for e in engine_checks),gap_count=len(gap_checks),engine_checks=engine_checks,gap_checks=gap_checks,review_decision_adjudications=decisions,inherited_cell_ledger=inherited,limitations=['All listed engines have actual source checks; not every line of their repositories was read.','The 855 survey rows and 345 prompt-feature scores remain inherited unless a narrower claim is explicitly verified above.','Closed hosted implementations, scores, model quality and current remote PR state are not certified.','Third-party static analysis does not substitute for runtime fault injection.'])
    (OUT/'codex_verification.json').write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps({k:result[k] for k in ['engine_count','source_claim_count','gap_count']},indent=2))

if __name__=='__main__':main()
