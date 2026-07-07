# Changelog

All notable changes to memspine are documented here. Format: [Keep a Changelog](https://keepachangelog.com/); versioning: [SemVer](https://semver.org/).

## [Unreleased]

### Added
- Project scaffold: `pyproject.toml` (slim core + extras matrix), `src/memspine` package stub, `justfile`, `.gitignore`, `.env.example`.
- Design docs under `docs/`: structure plan (blueprint + decision register D-01…D-43), rework proposal, dependency analysis, package catalog, ADR template.
- Claude Code setup: `CLAUDE.md`, `.claude/settings.json`, slash commands (`/phase`, `/decision`, `/scaffold`, `/check`), subagents (`memory-architect`, `dep-scout`).

### Notes
- Design is complete (P0–P7 planned); implementation of the P0 substrate is next.
