"""``memspine`` CLI (typer, D-04). Phase 0 ships the config commands;
tasks / rebuild / forget / audit / prompts join in later phases."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer
import yaml

from memspine.config.loader import ResolvedConfig, flatten_dotted, load_config
from memspine.core.registry import dependency_closure
from memspine.exceptions import ConfigError
from memspine.services.secrets.env import EnvSecrets

app = typer.Typer(name="memspine", help="memspine — cognitive memory engine for AI agents.")
config_app = typer.Typer(help="Inspect and validate layered configuration (D-11/D-12).")
app.add_typer(config_app, name="config")

TemplateOpt = Annotated[str | None, typer.Option("--template", "-t", help="Shipped template name")]
FileOpt = Annotated[Path | None, typer.Option("--config", "-c", help="User config YAML path")]


def _load(template: str | None, config_file: Path | None) -> ResolvedConfig:
    secrets = EnvSecrets(dotenv_path=".env")
    return load_config(
        template=template,
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


if __name__ == "__main__":  # pragma: no cover
    app()
