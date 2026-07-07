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
from memspine.prompts.registry import PromptRegistry
from memspine.services.secrets.env import EnvSecrets

app = typer.Typer(name="memspine", help="memspine — cognitive memory engine for AI agents.")
config_app = typer.Typer(help="Inspect and validate layered configuration (D-11/D-12).")
app.add_typer(config_app, name="config")
prompts_app = typer.Typer(help="Inspect the prompt pack and its overrides (D-43).")
app.add_typer(prompts_app, name="prompts")

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


def _registry(template: str | None, config_file: Path | None) -> PromptRegistry:
    resolved = _load(template, config_file)
    return PromptRegistry(overrides=resolved.config.prompts.overrides)


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


if __name__ == "__main__":  # pragma: no cover
    app()
