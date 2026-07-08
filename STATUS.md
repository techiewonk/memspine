# 📊 memspine — Status

> Auto-refreshed every 30 min · **last refresh: 2026-07-08 10:33 IST** · working tree: 18 uncommitted change(s).

| | |
|---|---|
| **Phase** | ✅ P0–P7 complete · 🎉 **full roadmap landed (P7 review fixes in)** |
| **Tests** | **575** passing |
| **ADRs** | 20 |
| **Version** | `0.0.1` — pre-alpha |
| **Latest commit** | `af359bb` — docs: full feature + how-to-use pass for P0–P7 (README/FEATURES/USAGE rewrite, CLAUDE.md) (2026-07-08 00:54) |

### 🏗️ Architecture — four-layer engine, event-sourced core
- **core** — audit · erasure · events · firewall · namespace · projector · records · registry · replay
- **memories (9/9)** — working · episodic · semantic · procedural · reflective · resource · associative · prospective · shared _(all types built)_
- **services** — cache · embedding · graph · lexical · llm · rerank · secrets · storage · vector _(graph adapter landed in P6)_
- **workers** — dbos_runner · inline · pipelines · runner · schedule · taskiq_runner
- **prompts** — registry · loader · YAML default pack
- **config** — loader · schema · 6 templates
- **protocols** — REST (rest/ FastAPI factory, 21 routes — P7 landed)

### ✅ Done
P0 Substrate · P1 Working memory + retrieval · P2 Semantic · P3 Episodic + lifecycle · P4 Memory Firewall · P5 Procedural + reflective · P6 Associative graph · P7 Prospective + shared + REST

### 🔜 Next
All 8 planned phases (P0–P7) complete. Remaining: hardening/review follow-ups, combination-test coverage (C6 landed), and a v0.1 authn layer (REST ships no-authn in v0.1).

### 🕑 Recent commits
```
af359bb docs: full feature + how-to-use pass for P0-P7 - README rewrite, FEATURES/USAGE, CLAUDE.md
42cbe7d C6: combination test matrix - template + single-type boots, kitchen-sink, config-validate golden
dd0816a P7 review fixes: 5-agent pass incl. security (cross-tenant forget IDOR, audit_taint scoping…)
fab25bd P7b: REST + taskiq + E5/E8 - rest FastAPI factory (21 routes), taskiq runner, compression…
0648ec6 P7a: prospective + shared memories - watches, grants as WRITE events, subscriptions v0.1…
```

<sub>Generated from `git log` + `src/` scan. Do not hand-edit — the scheduled task overwrites this file.</sub>
