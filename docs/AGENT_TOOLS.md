# Agent memory tools and the MCP server (I65, I66)

MemSpine ships a framework-neutral tool-set a model can call to search and manage its own
memory, one dispatcher that runs those calls on an `Engine`, and a stdio MCP server that
serves the same tools. Nothing here needs a third-party package. The design and its
evidence (Letta, LangMem, MIRIX, Mem0, Hindsight, Graphiti, Zep) are in
`FRAMEWORK_TOOLCALL_SURVEY.md` section 4.1 and 4.2.

## The tools

| Tool | Tier | Engine verb | Notes |
|---|---|---|---|
| `memory_search` | read | `Engine.search` | `query`, `top_k` (1-20), `kinds`, `purpose`. Rows: `id, text, kind, trust, src, valid_from, score`. Text is marker-escaped, clipped to 500 characters, 6000 in total, and labelled as data. Quarantined records are never returned. |
| `memory_write` | write | `Engine.write_ex` | `content` (max 1000), `kind` (`semantic`/`episodic`), `entity`, `attribute`. Verbatim. Returns a verdict: `stored`, `duplicate`, `rejected` or `quarantined`. |
| `memory_update` | write | `Engine.correct` | `id`, `content`, `reason`. Supersedes; the old value stays as history. |
| `memory_forget` | admin | `Engine.forget` | One `id`. Soft. No bulk, hard or erase form exists. |
| `memory_confirm` | write | `Engine.correct` | `id`, `user_quote`. Flips `src:assistant-proposed` to `src:user-confirmed`. |

Schemas are closed (`additionalProperties: false`). Use them from any framework:

```python
from memspine import Engine
from memspine.protocols.tools import AgentMemoryTools, ToolContext, openai_tools, anthropic_tools

engine = await Engine(template="assistant").start()
tools = AgentMemoryTools(engine, ToolContext(namespace="user/ana", principal="support-bot",
                                             session_id="s-42"))
openai_tools()      # [{"type": "function", "function": {...}}, ...]
anthropic_tools()   # [{"name", "description", "input_schema"}, ...]
tools.definitions() # neutral [{"name", "description", "parameters"}]

result = await tools.call("memory_write", {"content": "Prefers window seats"})
# {"ok": True, "verdict": "stored", "id": "..."}
tools.begin_turn()  # once per user turn: resets the per-turn write budget
```

`call` never raises for a bad call. It returns `{"ok": False, "error": <code>, "message": ...}`
so the model sees the refusal. Error codes: `unknown_tool`, `unknown_argument`,
`missing_argument`, `bad_argument`, `too_long`, `too_short`, `write_budget_exhausted`,
`not_found`, `not_permitted`, `quote_not_found`, plus engine errors by class name.

## Guards

All guards live in `AgentMemoryTools.call`; the prompt is never the only defence.

1. **Fixed context.** `namespace`, `principal`, `session_id` and `profile` come from
   `ToolContext`, set by the host. A call that passes `namespace`, `hard`, `ids`, `all` or any
   other undeclared argument is refused with `unknown_argument`. A record id from another
   namespace looks missing (`not_found`), so ids cannot be used to reach across tenants.
2. **Verbatim, capped writes.** The text is stored as given (no rewrite, no model call). It
   lands on the `agent_tool` channel, which the trust policy treats as external (trust capped
   at the retrieved-content cap, never boosted), with `role=assistant`, the caller's principal,
   and the tag `src:assistant-proposed`.
3. **Write budget.** `session_write_budget` (default 30) and `turn_write_budget` (default 5,
   reset by `begin_turn`). Every attempt counts, including quarantined and duplicate ones, so
   probing the firewall is bounded. Reads are free.
4. **Confirmation needs the user's words.** `memory_confirm` takes a quote of at least 8
   characters that must occur (case and whitespace insensitive) in a stored turn whose role is
   `user` and whose channel is not a tool channel; with a `session_id` the turn must be in that
   session. Only an `src:assistant-proposed` record can be confirmed. The new record keeps a
   parent link to the user turn. The quote proves the user said something; it does not prove it
   relates to the record, so treat it as a floor, not a judge.
5. **Single-record, soft forget.** `memory_forget` takes one id, runs `Engine.forget` with
   `hard=False, cascade=False`, and in the `standard` profile reaches only records this
   principal wrote through the tool channel. `update` has the same ownership rule. The
   `operator` profile lifts it.
6. **Firewall and audit.** Every write passes the engine firewall (redaction, instruction
   detection, anomaly and trust checks). A prompt-injection string is quarantined, its id is
   not handed back, and it is never returned by search. Every call is logged
   (`agent_tool.call`), kept in `tools.audit` (tool, namespace, principal, outcome, ids, argument
   names; never memory text), and, with `audit.actions: true`, appended to the engine's
   hash-chained audit log as `agent_tool.<name>`.
7. **Profiles.** `read_only` exposes `memory_search` only; `standard` exposes all five;
   `operator` also lets update and forget reach records the principal did not write.

Not guarded: a model can still write false but harmless-looking facts. They stay low-trust and
labelled `assistant-proposed` until a user quote confirms them.

## MCP server (stdio, no dependency)

```bash
memspine mcp --namespace user/ana --config ./memspine.yaml
memspine mcp -n user/ana -t assistant --db ./ana.db --profile read_only
```

Options: `--namespace/-n` (required, fixed for the server's life), `--config/-c`,
`--template/-t`, `--db`, `--profile` (`read_only|standard|operator`), `--principal`
(default `mcp-agent`), `--session`, `--session-writes` (30), `--turn-writes` (default 0 =
off; MCP has no turn signal, so a non-zero value is a lifetime burst cap that never resets).

The server speaks newline-delimited JSON-RPC 2.0 on stdin/stdout: `initialize` (echoes a
supported protocol version: 2025-06-18, 2025-03-26, 2024-11-05), `ping`, `tools/list`,
`tools/call`; notifications are accepted and ignored. Tools carry MCP annotations
(`readOnlyHint` on search, `destructiveHint` on forget). Stdout holds protocol frames only;
logs and stray prints go to stderr. Tool failures come back as a result with `isError: true`.

The engine boots with your config, so use a real embedder there (the default `fastembed`),
and point `storage.path` (or `--db`) at a file so memories persist between sessions.

### Register in Claude Code

```bash
claude mcp add memspine -- memspine mcp -n user/ana -c /abs/path/memspine.yaml
```

or in `.mcp.json`:

```json
{
  "mcpServers": {
    "memspine": {
      "command": "memspine",
      "args": ["mcp", "-n", "user/ana", "-c", "/abs/path/memspine.yaml"]
    }
  }
}
```

### Register in Claude Desktop

Add to `claude_desktop_config.json` (use the full path of the Python that has memspine
installed if `memspine` is not on the PATH Claude Desktop sees):

```json
{
  "mcpServers": {
    "memspine": {
      "command": "C:\\path\\to\\.venv\\Scripts\\memspine.exe",
      "args": ["mcp", "-n", "user/ana", "-c", "C:\\path\\to\\memspine.yaml"]
    }
  }
}
```

An MCP host decides when to call tools and may loop; the write budget is what bounds that.

### Official SDK / HTTP

Not shipped. The stdlib server covers stdio clients, which is what Claude Code and Claude
Desktop use. `McpServer.handle(message)` is transport-free, so an HTTP or SDK wrapper can
call it later without touching the guards.

## Tests

`tests/unit/protocols/test_agent_tools.py` (schemas, dispatch, budgets, quote check, bulk and
namespace injection, quarantine of an injection write) and
`tests/unit/protocols/test_mcp_server.py` (stdio round trip with an in-process engine and the
hash embedder, plus a real `memspine mcp` subprocess).
