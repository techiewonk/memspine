# Memory layer bug list (independent verification), 2026-10-15

Checked on branch `layer-contract` (= origin/main 7b9dcf03). No code changed. Probe re-run in memory
(`docs/v2/probe_memory_layers_2026_10_15.py`): all four session-close cells reproduce (lineage off: L1/L1,
lineage on: L0/L0, replay unchanged, episode leg `no_records:L2`); helper cells: consolidation, entity_summary,
reorganize stamp to L0 via `stamp_summary`; observation/standing/community/multires correct only when stamped
directly; graph leg with L1-backed edges returns `no_records:L3`, hit once an L3 count exists; `is_fact` vs
`is_input_fact` disagree for observation, standing, entity_summary.

"J" = `evals/arms/v2-PICK3-j.json` (lineage_table off, legs_v2/episode_leg_v2/graph_leg_v2 off, no offline stages, SQLite).
"GC" = `scr-gc3-graphleg` (graph_leg_v2, edge_compiler_v2, extract_graph L3 via channel, no episode leg, no close) and
`scr-b8-read-v2-graph` (adds episode_leg_v2 + offline.session_close_v2; lineage off).

| ID | Sev | Verdict | Root cause (file:line) | Types/layers | J arm | GC arms |
|---|---|---|---|---|---|---|
| MF-01 | P1 | CONFIRMED-reproduced | `derive/pipeline/integrate.py:521` passes `layer="episode"/"summary"` (not L-values); `derive/write.py:167-170` ignores it, `derive_layer("summary","session_close")` -> L0 (lineage on) / semantic default L1 (lineage off, `store/layers.py:84-87`); session_close not in `_CHANNEL_LAYER` (`layers.py:50-58`) | session-close episode+summary, intended L2 | No (session_close_v2 off) | b8: yes, L1 (lineage off) so L2 empty; episode leg inert |
| MF-02 | P1 | CONFIRMED-reproduced (helper); stages by code | `offline_door.py:70` `stamp_derived(record,"summary",site)` with route name as type -> L0; callers `consolidate.py:262`, `communities.py:564`, `entity_summaries.py:300`. Fires only when `ctx.link_derived` set (lineage_table on) | consolidation (L2->L0), entity_summary/reorganize (L3->L0) | No | No (lineage off => stamp is identity, constructor layer kept) |
| MF-03 | P1 | CONFIRMED-reproduced | `derive/write.py:169` `layer if layer in LAYERS else derive_layer(...)`: silent fallback, no error | all derived | latent | latent |
| MF-04 | P1 | CONFIRMED-code | `derive/write.py:117-118` lineage-off early return precedes `stamp_derived` (line 151); explicit L4/L5/L3/L2 from observation/standing/community/multires/entity stages dropped -> L1 | observation L4, standing L5, community/entity L3, multires L2 | No (stages off) | Any arm with those stages and lineage off: mislabelled L1 |
| MF-05 | P1 | CONFIRMED-reproduced | Layer in event payload only when it differs from `derive_layer` (`store/records.py:365-368`); replay keeps stored value. Lineage-on rows explicit L0, lineage-off rows re-derived L1. If `derive_layer` is later changed, omitted-layer history silently relabels on rebuild while explicit-L0 history does not (live/replay divergence) | all persisted derived | No data affected | b8 runs only |
| MF-06 | P1 | CONFIRMED-reproduced (consequence of 01) | `read/legs/episode.py:48-53` gates on registry/records layer L2 only | L2 | n/a (leg off) | b8: leg inert |
| MF-07 | P1 | CONFIRMED-reproduced (leg level) | `read/legs/graph.py:105-107` gate `is_populated("L3")`; edges live in `ctx.graph_edges`, independent of L3 records. Mitigated where `extract_graph`/`entity_summary`/`rule_edges` channels yield L3 records | graph edges backed by L1 facts | n/a | graphleg: works only because extraction yields L3 records; inert otherwise |
| MF-08 | P2 | CONFIRMED-code | `postgres/alembic/versions/pg0007_record_envelope.py:22-31` adds columns, no backfill (SQLite 0007 backfills via `derive_layer`); pg0008-0010 add none | pre-envelope rows on PG | No (SQLite) | No |
| MF-09 | P2 | PARTLY | `sql_base.py:403` coalesce(layer,'L0') vs `_envelope_fields` (`sql_base.py:638-641`) deriving for NULL. Real only for PG legacy rows; SQLite backfilled, new rows always write layer (`sql_base.py:298`) | legacy NULL rows | No | No |
| MF-10 | P2 | CONFIRMED-reproduced | `envelope_adapter.py:35-36,51` `_NON_FACT_CHANNELS` lacks observation/standing/entity_summary/session_close/community/multires; `offline_common.py:44-55,77-86` lists a different set | derived semantic views | No (offline off) | b8 only if close summaries exist |
| NEW-01 | P2 | CONFIRMED-code | Consequence of MF-10: unkeyed derived views pass `is_fact` into `histories.py:99` near-duplicate merge (can archive/merge a summary with a restated fact) and `integration_stage.py:216`; `reconcile.py:61` and the edge compiler require a key so are safe | session_close, entity_summary, observation, standing | No | No |
| MF-11 | P2 | DESIGN-CHOICE | `episode.py:36,63` any L2 record (consolidation, anticipation cue, window_episode) | L2 | n/a | b8 |
| MF-12 | P2 | CONFIRMED-code | `episode.py:57-58` global vector top `max(4*fetch_k,50)` then filtered to episode ids | sparse L2 | n/a | b8 |
| MF-13 | P2 | PARTLY | Profile card is MARKER-backed (`profile_card.py`); no read-side consumer found in `read/` or `api/` besides erasure scrub (`engine.py:3265`); L5 router label separate | profile | No | gc3-profilecard only |
| MF-14 | P2 | CONFIRMED-code | `layer_registry.py:38-59` `worker_model` enables L1+L3 regardless of producers; `read/pipeline/legs.py:372` builds registry with default `LayerTemplate()`, so read gating is population-only | all | No | reporting only |
| MF-15 | P2 | CONFIRMED (coverage gap) | No test asserts layer after session close (`tests/unit/wiring/test_ow_a_close_call2_profile.py`, `test_fu_close_followups.py`) | session close | n/a | n/a |
| NEW-02 | P2 | CONFIRMED-code | `stamp_derived` hashes `derivation` with the (possibly wrong) layer (`write.py:171-173`); a later layer fix changes fingerprints of identical content | derived | No | No |
| MF-16 | P3 | DESIGN-CHOICE | J arm is retrieval-only; cannot exercise the derived stack | all | by design | n/a |
| NEW-03 | P3 | CONFIRMED-code | `memory_type` parameter in `stamp_derived`/`write_derived` carries a write-route name ("summary"), the root of MF-01/02 (`integrate.py:515`, `offline_door.py:70`) | derived | - | - |
