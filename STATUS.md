# 📊 memspine — Status

> Auto-refreshed every 30 min · **last refresh: 2026-07-08 00:03 IST** · working tree: uncommitted changes present (git index unreadable on this mount — exact count unavailable).

| | |
|---|---|
| **Phase** | ✅ P0–P6 complete · 🔨 **P7 (Prospective + shared + REST) in progress** |
| **Tests** | **467** passing |
| **ADRs** | 19 |
| **Version** | `0.0.1` — pre-alpha |
| **Latest commit** | `fab25bd` — P7b: REST + taskiq + E5/E8 — rest FastAPI factory (21 routes), taskiq runner, assembly compression, rerank (2026-07-07 22:40) |

### 🏗️ Architecture — four-layer engine, event-sourced core
- **core** — audit · erasure · events · firewall · namespace · projector · records · registry · replay
- **memories (9/9)** — working · episodic · semantic · procedural · reflective · resource · associative · prospective · shared
- **services** — cache · embedding · graph · llm · rerank · secrets · storage · vector _(graph is a P6 scaffold until the adapter lands)_
- **workers** — dbos_runner · inline · pipelines · runner · schedule · taskiq_runner
- **prompts** — registry · loader · YAML default pack
- **config** — loader · schema · 6 templates
- **protocols** — REST (rest/ FastAPI factory, 21 routes — P7b landed)

### ✅ Done
P0 Substrate · P1 Working memory + retrieval · P2 Semantic · P3 Episodic + lifecycle · P4 Memory Firewall · P5 Procedural + reflective · P6 Associative graph

### 🔜 Next
**P7** Prospective + shared + REST → P7a (prospective + shared) and P7b (REST + taskiq) landed; P7 review-fixes pass remains to close the final phase.

### 🕑 Recent commits
```
fab25bd P7b: REST + taskiq + E5/E8 - rest FastAPI factory (21 routes), taskiq runner, compression…
0648ec6 P7a: prospective + shared memories - watches, grants as WRITE events, subscriptions v0.1…
8620873 P6 review fixes: 5-agent pass (python, code, silent-failure, test-coverage, blueprint)…
5299124 P6: associative memory + graph services - GraphStore port, EventKind.LINK, PPR retrieval…
12b2eb8 docs: ADR-014 P5 lifecycle amendments (D-48) - staged-plan RESOLVING hold, prompt active…
```

<sub>Generated from `git log` + `src/` scan. Do not hand-edit — the scheduled task overwrites this file.</sub>
