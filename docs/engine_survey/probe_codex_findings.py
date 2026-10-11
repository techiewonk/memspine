"""Offline probes of actual MemSpine functions; no model, network or persistent store."""
from datetime import UTC, datetime
import json
from pathlib import Path

from memspine.core.policies.assembly import AssemblyPolicy
from memspine.core.records import MemoryRecord
from memspine.prompts.models import ExtractedEdge
from memspine.workers.pipelines import _edge_key, _fact_date
from memspine.workers.windows import validate_edges, validate_facts

def main():
    source='Alex said the bicycle is blue.'
    facts=validate_facts([dict(entity='Alex',attribute='vehicle',value='Alex owns a spaceship',kind='event',date=None,turns=[1],span='the')],[source])
    edge=dict(src_entity='Alex',rel='owns',dst_entity='spaceship',fact='Alex owns a spaceship',kind='event',turns=[1])
    edges=validate_edges([edge],1)
    event=ExtractedEdge.model_validate(edge)
    state=event.model_copy(update={'kind':'state'})
    repeated=event.model_copy(update={'rel':'visited','dst_entity':'workshop','valid_from':'2023-05-09','fact':'Alex visited the workshop again'})
    first=event.model_copy(update={'rel':'visited','dst_entity':'workshop','valid_from':'2023-05-09','fact':'Alex visited the workshop'})
    a=MemoryRecord(namespace='probe',memory_type='semantic',content='first small item')
    b=MemoryRecord(namespace='probe',memory_type='semantic',content='oversized '*200)
    c=MemoryRecord(namespace='probe',memory_type='semantic',content='last small item')
    policy=AssemblyPolicy.bind({'theta_abstain':0.0,'mmr_lambda':1.0})
    packed=policy.assemble([(a,1.0),(b,.9),(c,.8)],budget_tokens=20)
    results={
        'synthetic_only':True,
        'weak_span_unsupported_fact_accepted':bool(facts and facts.facts),
        'line_index_only_edge_accepted':bool(edges and edges.edges),
        'plain_state_event_key_collision':_edge_key('probe',event)==_edge_key('probe',state),
        'dated_repeated_same_day_event_key_collision':_edge_key('probe',first,dated=True)==_edge_key('probe',repeated,dated=True),
        'year_precision_becomes':_fact_date('2023',datetime(2024,1,1,tzinfo=UTC)).isoformat(),
        'month_precision_becomes':_fact_date('2023-05',datetime(2024,1,1,tzinfo=UTC)).isoformat(),
        'packing_selected_contents':[r.content for r in packed.records],
        'packing_skips_fitting_tail':c.record_id not in [r.record_id for r in packed.records],
        'scope':'Function-level reproductions, not end-to-end answer-quality measurements. Packing follows its current documented never-empty/stop contract.',
    }
    p=Path(__file__).resolve().parent/'independent_review_2026-10-11/codex_probe_results.json'
    p.write_text(json.dumps(results,indent=2),encoding='utf-8')
    print(json.dumps(results,indent=2))

if __name__=='__main__':main()
