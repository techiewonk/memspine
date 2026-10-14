"""Tuning Studio (TS-1/3/4): a local stdio MCP server over the Trace Inspector step session.

    python -m memspine_evals.studio.server [--data demo|PATH] [--out DIR] [--allow-gpu]

``api.StudioAPI`` returns structured dicts per ``docs/v2/TUNING_STUDIO_PLAN.md`` (tool contract v1);
``server`` speaks JSON-RPC 2.0 over stdio. No dependency beyond the evals harness.
"""
