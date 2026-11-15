"""RM-136 ownership and isolation checks for the disposable service launcher."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _launcher() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "local_services.py"
    spec = importlib.util.spec_from_file_location("local_services", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_new_state_has_isolated_project_and_ports() -> None:
    launcher = _launcher()
    state = launcher.new_state()
    assert launcher.PROJECT_RE.fullmatch(state["project"])
    assert len(set(state["ports"].values())) == len(launcher.PORTS)
    assert set(state["ports"]) == set(launcher.PORTS)


def test_refuses_cleanup_of_container_without_owner_label(monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = _launcher()
    state = launcher.new_state()
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: Any) -> Any:
        calls.append(args)
        return type("Result", (), {"stdout": "foreign-id\n" if args[1] == "ps" else "other\n"})()

    monkeypatch.setattr(launcher, "run", fake_run)
    with pytest.raises(RuntimeError, match="ownership marker"):
        launcher.owned_containers(state)
    assert not any("rm" in call or "down" in call for call in calls)


@pytest.mark.parametrize("kind", ["volume", "network"])
def test_refuses_foreign_resource_even_without_containers(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    launcher = _launcher()
    state = launcher.new_state()
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: Any) -> Any:
        calls.append(args)
        if args[2] == "ls":
            name = (
                f"{state['project']}_postgres_data"
                if kind == "volume"
                else f"{state['project']}_default"
            )
            return type(
                "Result", (), {"returncode": 0, "stdout": name if args[1] == kind else ""}
            )()
        return type(
            "Result",
            (),
            {"returncode": 0, "stdout": f"[{json.dumps({'Name': args[-1], 'Labels': {}})}]"},
        )()

    monkeypatch.setattr(launcher, "run", fake_run)
    with pytest.raises(RuntimeError, match="ownership marker"):
        launcher.owned_resources(state)
    assert not any("down" in call for call in calls)


def test_absent_resources_are_safe_to_reconcile(monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = _launcher()
    state = launcher.new_state()

    def missing(args: list[str], **_kwargs: Any) -> Any:
        assert args[2] == "ls"
        return type("Result", (), {"returncode": 0, "stdout": ""})()

    monkeypatch.setattr(launcher, "run", missing)
    launcher.owned_resources(state)


@pytest.mark.parametrize("kind", ["volume", "network"])
def test_inspect_error_is_not_treated_as_absent_resource(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    launcher = _launcher()
    state = launcher.new_state()

    def failed_inspect(args: list[str], **_kwargs: Any) -> Any:
        if args[2] == "ls":
            name = (
                f"{state['project']}_postgres_data"
                if kind == "volume"
                else f"{state['project']}_default"
            )
            return type(
                "Result", (), {"returncode": 0, "stdout": name if args[1] == kind else ""}
            )()
        if args[1] == kind and args[2] == "inspect":
            return type(
                "Result", (), {"returncode": 1, "stdout": "", "stderr": "permission denied"}
            )()
        raise AssertionError(args)

    monkeypatch.setattr(launcher, "run", failed_inspect)
    with pytest.raises(RuntimeError, match="inspect|inspection"):
        launcher.owned_resources(state)


def test_resource_list_error_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = _launcher()
    state = launcher.new_state()

    def denied(_args: list[str], **_kwargs: Any) -> Any:
        raise RuntimeError("docker command failed")

    monkeypatch.setattr(launcher, "run", denied)
    with pytest.raises(RuntimeError, match="docker command failed"):
        launcher.owned_resources(state)


def test_status_refuses_stopped_container_even_if_ports_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _launcher()
    state = launcher.new_state()
    monkeypatch.setattr(launcher, "owned_containers", lambda _state: list(launcher.SERVICES))
    monkeypatch.setattr(launcher, "wait_tcp", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(launcher, "initialize_mongo", lambda _state: None)

    def fake_run(args: list[str], **_kwargs: Any) -> Any:
        if args[:2] == ["docker", "inspect"]:
            return type("Result", (), {"returncode": 0, "stdout": "exited\n"})()
        raise AssertionError(args)

    def fake_compose(_state: dict[str, object], args: list[str], **_kwargs: Any) -> Any:
        if args[:2] == ["exec", "-T"]:
            return type("Result", (), {"returncode": 0, "stdout": ""})()
        raise AssertionError(args)

    monkeypatch.setattr(launcher, "run", fake_run)
    monkeypatch.setattr(launcher, "compose", fake_compose)
    with pytest.raises(RuntimeError, match="running|healthy|status|inspect"):
        launcher.check_status(state)


def test_status_requires_neo4j_read_only_query(monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = _launcher()
    state = launcher.new_state()
    monkeypatch.setattr(launcher, "owned_containers", lambda _state: list(launcher.SERVICES))
    monkeypatch.setattr(launcher, "wait_tcp", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(launcher, "initialize_mongo", lambda _state: None)

    def fake_run(args: list[str], **_kwargs: Any) -> Any:
        if args[:2] == ["docker", "inspect"]:
            return type(
                "Result", (), {"returncode": 0, "stdout": f"{args[-1]}|running|healthy\n"}
            )()
        raise AssertionError(args)

    def fake_compose(_state: dict[str, object], args: list[str], **_kwargs: Any) -> Any:
        assert args[:3] == ["exec", "-T", "neo4j"]
        return type("Result", (), {"returncode": 1, "stdout": ""})()

    monkeypatch.setattr(launcher, "run", fake_run)
    monkeypatch.setattr(launcher, "compose", fake_compose)
    with pytest.raises(RuntimeError, match="Neo4j read-only query"):
        launcher.check_status(state)


def test_status_uses_current_state_password_after_neo4j_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _launcher()
    state = launcher.new_state()
    state["password"] = "current-state-password"
    monkeypatch.setenv("NEO4J_AUTH", "neo4j/stale-bootstrap-password")
    monkeypatch.setattr(launcher, "owned_containers", lambda _state: list(launcher.SERVICES))
    monkeypatch.setattr(launcher, "wait_tcp", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(launcher, "initialize_mongo", lambda _state: None)

    def fake_run(args: list[str], **_kwargs: Any) -> Any:
        if args[:2] == ["docker", "inspect"]:
            return type(
                "Result", (), {"returncode": 0, "stdout": f"{args[-1]}|running|healthy\n"}
            )()
        raise AssertionError(args)

    def fake_compose(_state: dict[str, object], args: list[str], **kwargs: Any) -> Any:
        if args[:3] == ["exec", "-T", "neo4j"]:
            assert kwargs["input_text"] == "current-state-password\n"
            assert 'tr -d "\\r"' in " ".join(args)
            assert "stale-bootstrap-password" not in " ".join(args)
            assert "current-state-password" not in " ".join(args)
            assert "NEO4J_AUTH" not in " ".join(args)
            return type("Result", (), {"returncode": 0, "stdout": "ready\n1\n"})()
        assert args[:3] == ["exec", "-T", "temporal"]
        return type("Result", (), {"returncode": 0, "stdout": ""})()

    monkeypatch.setattr(launcher, "run", fake_run)
    monkeypatch.setattr(launcher, "compose", fake_compose)
    launcher.check_status(state)
