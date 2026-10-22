"""``memspine`` CLI (typer, D-04). Phase 0 ships the config commands;
tasks / rebuild / forget / audit / prompts join in later phases."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml

from memspine.config.loader import ResolvedConfig, default_template, flatten_dotted, load_config
from memspine.core.registry import dependency_closure
from memspine.exceptions import ConfigError
from memspine.prompts.registry import PromptRegistry
from memspine.services.secrets.env import EnvSecrets

app = typer.Typer(name="memspine", help="memspine — cognitive memory engine for AI agents.")
config_app = typer.Typer(help="Inspect and validate layered configuration (D-11/D-12).")
app.add_typer(config_app, name="config")
prompts_app = typer.Typer(help="Inspect the prompt pack and its overrides (D-43).")
app.add_typer(prompts_app, name="prompts")
audit_app = typer.Typer(help="Audit the event log (E1: blast-radius taint tracing).")
app.add_typer(audit_app, name="audit")
tap_app = typer.Typer(help="Wiretap traces (WT-01): per-request pipeline gaps, debugging only.")
app.add_typer(tap_app, name="tap")

TemplateOpt = Annotated[str | None, typer.Option("--template", "-t", help="Shipped template name")]
FileOpt = Annotated[Path | None, typer.Option("--config", "-c", help="User config YAML path")]


def _load(template: str | None, config_file: Path | None) -> ResolvedConfig:
    secrets = EnvSecrets(dotenv_path=".env")
    return load_config(
        template=default_template(template, config_file),  # ADR-032
        user_config=config_file,
        env=os.environ,
        secret_resolver=secrets.get,
    )


@config_app.command("validate")
def config_validate(template: TemplateOpt = None, config_file: FileOpt = None) -> None:
    """Load all layers, run the dependency closure, report the effective combination."""
    try:
        resolved = _load(template, config_file)
        enabled, auto = dependency_closure(resolved.config.enabled_memories())
    except ConfigError as exc:
        typer.secho(f"invalid: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.secho("valid", fg=typer.colors.GREEN)
    typer.echo(f"profile: {resolved.config.profile}")
    typer.echo(f"memories: {', '.join(sorted(enabled)) or '(none)'}")
    if auto:
        typer.echo(f"auto-enabled (C1b): {', '.join(auto)}")
    typer.echo(f"event_log: mode={resolved.config.event_log.mode.value}")


@config_app.command("resolve")
def config_resolve(template: TemplateOpt = None, config_file: FileOpt = None) -> None:
    """Print the merged effective config with a `# source:` annotation per key."""
    try:
        resolved = _load(template, config_file)
    except ConfigError as exc:
        typer.secho(f"invalid: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    flat = flatten_dotted(resolved.config.model_dump(mode="json"))
    width = max(len(key) for key in flat)
    for key, value in sorted(flat.items()):
        rendered = yaml.safe_dump(value, default_flow_style=True).strip()
        source = resolved.sources.get(key, "default")
        typer.echo(f"{key.ljust(width)} : {rendered}  # source: {source}")


def _registry(template: str | None, config_file: Path | None) -> PromptRegistry:
    resolved = _load(template, config_file)
    return PromptRegistry(
        overrides=resolved.config.prompts.overrides,
        partials=resolved.config.prompts.partials,
        selection=resolved.config.prompts.selection,
        layout_v2=resolved.config.prompts.layout_v2,
        layout_partials=resolved.config.prompts.layout_partials,
    )


@prompts_app.command("list")
def prompts_list(template: TemplateOpt = None, config_file: FileOpt = None) -> None:
    """Every prompt with its active version and source layer."""
    try:
        registry = _registry(template, config_file)
    except ConfigError as exc:
        typer.secho(f"invalid: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    for prompt in registry.list():
        typer.echo(
            f"{prompt.id:<16} {prompt.prompt_version:<20} role={prompt.role:<14} "
            f"format={prompt.format.value:<5} source={registry.source_of(prompt.id)}"
        )


@prompts_app.command("show")
def prompts_show(prompt_id: str, template: TemplateOpt = None, config_file: FileOpt = None) -> None:
    """Full definition of one active prompt (frontmatter + body)."""
    try:
        prompt = _registry(template, config_file).get(prompt_id)
    except ConfigError as exc:
        typer.secho(f"invalid: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    meta = prompt.model_dump(exclude={"body"}, exclude_none=True, mode="json")
    typer.echo(yaml.safe_dump(meta, sort_keys=True).strip())
    typer.echo("---")
    typer.echo(prompt.body)


@prompts_app.command("resolve")
def prompts_resolve(template: TemplateOpt = None, config_file: FileOpt = None) -> None:
    """One line per prompt: id@version  # source: defaults|override."""
    try:
        registry = _registry(template, config_file)
    except ConfigError as exc:
        typer.secho(f"invalid: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    for prompt in registry.list():
        typer.echo(f"{prompt.prompt_version}  # source: {registry.source_of(prompt.id)}")


DbOpt = Annotated[Path, typer.Option("--db", help="Path to the memspine SQLite database")]


def _run_engine_op(db: Path, op: str, **kwargs: object) -> dict[str, object]:
    """Boot a throwaway engine on the database and run one verb. The hash
    embedder keeps this deterministic and dependency-free; embedder-scoped
    vector queries simply return nothing if the store used another model."""
    import asyncio

    from memspine import Engine

    async def _inner() -> dict[str, object]:
        # ADR-032: an audit/forget op on a user database must not pick up the
        # assistant read settings (e.g. record_access off); it runs on ``base``.
        engine = Engine(
            template="base",
            dotenv_path=None,
            storage={"settings": {"path": str(db)}},
            embedding={"provider": "hash"},
        )
        await engine.start()
        try:
            if op == "taint":
                report = await engine.audit_taint(
                    str(kwargs["record_id"]), namespace=str(kwargs["namespace"])
                )
                return report.model_dump(mode="json")
            if op == "forget":
                await engine.forget(
                    str(kwargs["record_id"]),
                    namespace=str(kwargs["namespace"]),
                    hard=bool(kwargs["hard"]),
                )
                if kwargs["verify"]:
                    return await engine.verify_forget(
                        str(kwargs["record_id"]), namespace=str(kwargs["namespace"])
                    )
                return {"forgotten": kwargs["record_id"], "hard": kwargs["hard"]}
            raise ValueError(op)
        finally:
            await engine.stop()

    return asyncio.run(_inner())


@audit_app.command("taint")
def audit_taint(
    record_id: str,
    db: DbOpt = Path("./memspine.db"),
    namespace: Annotated[str, typer.Option("--namespace", "-n")] = "default",
) -> None:
    """E1 blast radius: origin + every derivation of one record, from the log.

    Scoped to ``--namespace`` (SEC-C3/ADR-018): the seed record must belong to
    it, else the anti-oracle error is raised.
    """
    report = _run_engine_op(db, "taint", record_id=record_id, namespace=namespace)
    typer.echo(yaml.safe_dump(report, sort_keys=False).strip())


@app.command("migrate")
def migrate_cmd(
    source: Annotated[Path, typer.Argument(help="Existing (v1) SQLite database; never written")],
    target: Annotated[Path, typer.Argument(help="Fresh database to build by replay of the log")],
) -> None:
    """Migrate by replay (G-23): upcast the retained event log into a fresh store.

    Limited rebuild is advertised: the report lists the derived layers replay does not rebuild.
    """
    import asyncio

    from memspine.exceptions import RebuildUnavailableError
    from memspine.store.migrate import migrate_sqlite

    try:
        report = asyncio.run(migrate_sqlite(str(source), str(target)))
    except RebuildUnavailableError as exc:
        typer.secho(f"cannot migrate: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(yaml.safe_dump(report.as_dict(), sort_keys=False).strip())


@app.command("forget")
def forget_cmd(
    record_id: str,
    db: DbOpt = Path("./memspine.db"),
    namespace: Annotated[str, typer.Option("--namespace", "-n")] = "default",
    hard: Annotated[bool, typer.Option("--hard", help="M7 hard delete + log redaction")] = False,
    verify: Annotated[bool, typer.Option("--verify", help="Prove erasure afterwards")] = False,
) -> None:
    """Forget a memory (M7). ``--hard --verify`` = provable erasure."""
    result = _run_engine_op(
        db, "forget", record_id=record_id, namespace=namespace, hard=hard, verify=verify
    )
    typer.echo(yaml.safe_dump(result, sort_keys=False).strip())
    if verify and not result.get("clean", False):
        raise typer.Exit(code=1)


@app.command("export")
def export_cmd(
    out: Annotated[Path, typer.Option("--out", "-o", help="JSONL file to write")],
    db: DbOpt = Path("./memspine.db"),
    namespace: Annotated[str, typer.Option("--namespace", "-n")] = "default",
    subject: Annotated[
        str | None, typer.Option("--subject", help="Only records about this subject")
    ] = None,
    history: Annotated[
        bool, typer.Option("--history/--no-history", help="Keep archived versions")
    ] = True,
    events: Annotated[
        bool, typer.Option("--events/--no-events", help="Add the namespace's log events")
    ] = False,
) -> None:
    """Subject-access export (#46): a namespace's records (and events) as JSONL."""
    import asyncio

    from memspine import Engine

    async def _inner() -> list[str]:
        engine = Engine(
            template="base",
            dotenv_path=None,
            storage={"settings": {"path": str(db)}},
            embedding={"provider": "hash"},
        )
        await engine.start()
        try:
            return await engine.export(
                namespace, subject=subject, include_history=history, include_events=events
            )
        finally:
            await engine.stop()

    lines = asyncio.run(_inner())
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    typer.echo(f"exported {len(lines) - 1} lines to {out}")


@app.command("mcp")
def mcp_cmd(
    namespace: Annotated[
        str, typer.Option("--namespace", "-n", help="Fixed tenant namespace (never model-set)")
    ],
    config_file: FileOpt = None,
    template: TemplateOpt = None,
    db: Annotated[Path | None, typer.Option("--db", help="SQLite path (overrides config)")] = None,
    profile: Annotated[
        str, typer.Option("--profile", help="read_only | standard | operator")
    ] = "standard",
    principal: Annotated[str, typer.Option("--principal", help="Writer identity")] = "mcp-agent",
    session: Annotated[
        str | None, typer.Option("--session", help="Session id for confirm-quote lookup")
    ] = None,
    session_writes: Annotated[int, typer.Option("--session-writes", min=0)] = 30,
    turn_writes: Annotated[
        int, typer.Option("--turn-writes", min=0, help="Burst cap; MCP has no turn signal (0=off)")
    ] = 0,
) -> None:
    """Serve the agent memory tools over MCP (stdio JSON-RPC). Stdout is protocol only."""
    import asyncio
    import sys

    from memspine.api.mcp import McpServer, serve_stdio
    from memspine.api.process import EngineSpec, process_engine, worker_readiness
    from memspine.api.tools import AgentMemoryTools, ToolContext

    try:
        ctx = ToolContext(
            namespace=namespace,
            principal=principal,
            session_id=session,
            profile=profile,
            session_write_budget=session_writes,
            turn_write_budget=turn_writes or None,
        )
    except ValueError as exc:
        typer.secho(f"invalid: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    protocol_out = sys.stdout
    sys.stdout = sys.stderr  # a stray print must never corrupt the protocol stream
    # Diagnostics (including background warm-up warnings, RM-74) go to stderr too: the
    # default log sink was bound to the protocol stream's fd at import.
    from memspine.observability.logging import configure_logging, get_logger

    configure_logging()

    async def _inner() -> None:
        overrides: dict[str, Any] = {"storage": {"settings": {"path": str(db)}}} if db else {}
        spec = EngineSpec(template, str(config_file) if config_file else None, overrides)
        # RM-76 start-up hook: this subprocess builds, starts and warms its own Engine and
        # registry (runtime.shared_resources.warmup); readiness is logged to stderr.
        async with process_engine(spec, role="mcp") as engine:
            ready = worker_readiness()
            get_logger("memspine.cli").info(
                "runtime.worker_ready", role="mcp", pid=ready["pid"], ready=ready["ready"]
            )
            server = McpServer(AgentMemoryTools(engine, ctx))
            await serve_stdio(server, sys.stdin, protocol_out)

    try:
        asyncio.run(_inner())
    except ConfigError as exc:
        typer.secho(f"invalid: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


@tap_app.command("report")
def tap_report_cmd(
    path: Annotated[Path, typer.Argument(help="Tap directory, tap-*.jsonl file or tap.sqlite")],
    request: Annotated[str | None, typer.Option("--request", help="Only this request id")] = None,
    html: Annotated[Path | None, typer.Option("--html", help="Write an HTML report")] = None,
    since: Annotated[str | None, typer.Option("--since", help="2h | 1d | ISO date")] = None,
) -> None:
    """List the gaps and issues of each traced request."""
    from memspine.observability import tap_report

    argv = [str(path)]
    if request:
        argv += ["--request", request]
    if html:
        argv += ["--html", str(html)]
    if since:
        argv += ["--since", since]
    tap_report.main(argv)


if __name__ == "__main__":  # pragma: no cover
    app()
