#!/usr/bin/env python3
"""Own and exercise the disposable ADR-096 local Compose stack (RM-136).

No existing Docker project, container, volume, or host service is reused or stopped.
State lives under the user's temporary directory, outside the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "infra" / "local-services.compose.yaml"
STATE_DIR = Path(tempfile.gettempdir()).resolve() / "memspine-rm136"
STATE = STATE_DIR / (hashlib.sha256(str(ROOT).encode()).hexdigest()[:16] + ".json")
PORTS = ("POSTGRES", "REDIS", "VALKEY", "MONGO", "NEO4J", "TEMPORAL", "TEMPORAL_UI")
SERVICES = ("postgres", "redis", "valkey", "mongo", "neo4j", "temporal")
VOLUMES = (
    "postgres_data",
    "redis_data",
    "valkey_data",
    "mongo_data",
    "neo4j_data",
    "temporal_data",
)
PROJECT_RE = re.compile(r"^memspine-rm136-[0-9a-f]{12}$")


def run(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args, cwd=ROOT, env=env, text=True, capture_output=True, check=False, input=input_text
    )
    if check and result.returncode:
        # Docker output may include Compose-expanded secrets; print only the command name.
        raise RuntimeError(
            f"{args[0]} command failed (exit {result.returncode}); inspect owned container status"
        )
    return result


def docker_ready() -> None:
    if run(["docker", "info", "--format", "{{.ServerVersion}}"], check=False).returncode:
        raise RuntimeError("Docker Linux daemon is unavailable; start Docker Desktop and retry")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def new_state() -> dict[str, object]:
    selected: set[int] = set()
    ports: dict[str, int] = {}
    for name in PORTS:
        port = free_port()
        while port in selected:
            port = free_port()
        ports[name] = port
        selected.add(port)
    owner = secrets.token_hex(16)
    return {
        "project": f"memspine-rm136-{secrets.token_hex(6)}",
        "owner": owner,
        "password": secrets.token_urlsafe(24),
        "ports": ports,
        "root": str(ROOT),
    }


def load_state() -> dict[str, object] | None:
    if not STATE.exists():
        return None
    if STATE_DIR.resolve() != STATE_DIR:
        raise RuntimeError("state directory is redirected outside its temporary location")
    if STATE.resolve().parent != STATE_DIR.resolve():
        raise RuntimeError("state file escaped its temporary directory")
    if os.name != "nt" and stat.S_IMODE(STATE.stat().st_mode) & 0o077:
        raise RuntimeError("state file permissions are too broad")
    raw: object = json.loads(STATE.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise RuntimeError("invalid local stack state")
    data = cast(dict[str, object], raw)
    if not PROJECT_RE.fullmatch(str(data.get("project", ""))) or data.get("root") != str(ROOT):
        raise RuntimeError("local stack state does not match this repository")
    if not re.fullmatch(r"[0-9a-f]{32}", str(data.get("owner", ""))):
        raise RuntimeError("invalid local stack ownership marker")
    ports_for(data)
    return data


def ports_for(data: dict[str, object]) -> dict[str, int]:
    raw = data.get("ports")
    if not isinstance(raw, dict) or set(raw) != set(PORTS):
        raise RuntimeError("invalid local stack port map")
    if any(type(port) is not int or not 1 <= port <= 65535 for port in raw.values()):
        raise RuntimeError("invalid local stack port map")
    ports = cast(dict[str, int], raw)
    if len(set(ports.values())) != len(PORTS):
        raise RuntimeError("duplicate local stack ports")
    return ports


def save_state(data: dict[str, object]) -> None:
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if STATE_DIR.resolve() != STATE_DIR:
        raise RuntimeError("state directory is redirected outside its temporary location")
    if os.name != "nt":
        os.chmod(STATE_DIR, 0o700)
    if STATE.resolve().parent != STATE_DIR.resolve():
        raise RuntimeError("state path escaped its temporary directory")
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f"{STATE.stem}.",
        suffix=".new",
        dir=STATE_DIR,
        delete=False,
    ) as handle:
        handle.write(json.dumps(data))
        temp = Path(handle.name)
    try:
        if os.name != "nt":
            os.chmod(temp, 0o600)
        temp.replace(STATE)
    finally:
        temp.unlink(missing_ok=True)


def env_for(data: dict[str, object]) -> dict[str, str]:
    env = os.environ.copy()
    env["RM136_OWNER"] = str(data["owner"])
    env["RM136_PASSWORD"] = str(data["password"])
    for name, port in ports_for(data).items():
        env[f"RM136_{name}_PORT"] = str(port)
    return env


def compose(
    data: dict[str, object],
    args: list[str],
    *,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return run(
        ["docker", "compose", "-p", str(data["project"]), "-f", str(COMPOSE), *args],
        env=env_for(data),
        check=check,
        input_text=input_text,
    )


def owned_containers(data: dict[str, object]) -> list[str]:
    result = run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label=com.docker.compose.project={data['project']}",
            "--format",
            "{{.ID}}",
        ]
    )
    ids = result.stdout.split()
    for container_id in ids:
        inspected = run(
            [
                "docker",
                "inspect",
                "--format",
                '{{index .Config.Labels "memspine.rm136.owner"}}',
                container_id,
            ]
        )
        if inspected.stdout.strip() != data["owner"]:
            raise RuntimeError("project contains a container without this run's ownership marker")
    return ids


def owned_resources(data: dict[str, object]) -> None:
    """Refuse any colliding volume or network before Compose can change it."""
    project = str(data["project"])
    for kind, names in (
        ("volume", [f"{project}_{name}" for name in VOLUMES]),
        ("network", [f"{project}_default"]),
    ):
        listed = run(["docker", kind, "ls", "--format", "{{.Name}}"])
        existing = set(listed.stdout.splitlines())
        for name in names:
            if name not in existing:
                continue
            result = run(["docker", kind, "inspect", name], check=False)
            if result.returncode:
                raise RuntimeError(f"{kind} {name} inspection failed")
            resources = json.loads(result.stdout)
            if len(resources) != 1 or resources[0].get("Name") != name:
                raise RuntimeError(f"{kind} {name} inspection returned an unexpected resource")
            labels = resources[0].get("Labels") or {}
            if (
                labels.get("memspine.rm136.owner") != data["owner"]
                or labels.get("com.docker.compose.project") != project
            ):
                raise RuntimeError(f"{kind} {name} lacks this run's ownership marker")


def wait_tcp(port: int, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                return
        except OSError:
            time.sleep(1)
    raise TimeoutError(f"loopback port {port} did not open")


def initialize_mongo(data: dict[str, object]) -> None:
    query = 'try { rs.status().myState } catch (e) { rs.initiate({_id:"rs0",members:[{_id:0,host:"mongo:27017"}]}); 0 }'
    result = compose(data, ["exec", "-T", "mongo", "mongosh", "--quiet", "--eval", query])
    if not result.stdout.strip().endswith(("0", "1")):
        raise RuntimeError("Mongo replica set initiation returned an unexpected result")
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        result = compose(
            data,
            ["exec", "-T", "mongo", "mongosh", "--quiet", "--eval", "rs.status().myState"],
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip().endswith("1"):
            return
        time.sleep(2)
    raise TimeoutError("Mongo replica set did not elect PRIMARY")


def check_status(data: dict[str, object]) -> None:
    ids = owned_containers(data)
    if len(ids) != len(SERVICES):
        raise RuntimeError(f"owned stack has {len(ids)} of {len(SERVICES)} containers")
    found: set[str] = set()
    for container_id in ids:
        inspected = run(
            [
                "docker",
                "inspect",
                "--format",
                '{{index .Config.Labels "com.docker.compose.service"}}|{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}',
                container_id,
            ]
        )
        parts = inspected.stdout.strip().split("|")
        if len(parts) != 3:
            raise RuntimeError("owned container status inspection was invalid")
        service, status, health = parts
        if service not in SERVICES or service in found:
            raise RuntimeError("owned container service status was invalid")
        if status != "running" or health not in {"healthy", "none"}:
            raise RuntimeError(f"owned {service} container is not running and healthy")
        found.add(service)
    if found != set(SERVICES):
        raise RuntimeError("owned stack is missing a service container")
    ports = ports_for(data)
    for name in PORTS:
        wait_tcp(int(ports[name]), timeout=120 if name == "NEO4J" else 60)
    initialize_mongo(data)
    neo4j = compose(
        data,
        [
            "exec",
            "-T",
            "neo4j",
            "sh",
            "-c",
            "IFS= read -r NEO4J_PASSWORD && "
            'NEO4J_PASSWORD=$(printf %s "$NEO4J_PASSWORD" | tr -d "\\r") && '
            "export NEO4J_PASSWORD && "
            'cypher-shell -u neo4j "RETURN 1 AS ready"',
        ],
        check=False,
        input_text=f"{data['password']}\n",
    )
    if neo4j.returncode or neo4j.stdout.strip().splitlines() != ["ready", "1"]:
        raise RuntimeError("Neo4j read-only query did not succeed")
    # The CLI in the Temporal image proves the namespace service is responding.
    temporal = compose(
        data,
        [
            "exec",
            "-T",
            "temporal",
            "temporal",
            "operator",
            "namespace",
            "describe",
            "default",
            "--address",
            "127.0.0.1:7233",
        ],
        check=False,
    )
    if temporal.returncode:
        raise RuntimeError("Temporal default namespace is not responding")
    print(f"READY {data['project']} ports=" + json.dumps(ports, sort_keys=True))


def up() -> None:
    docker_ready()
    data = load_state() or new_state()
    owned_containers(data)
    owned_resources(data)
    compose(data, ["config", "--quiet"])
    save_state(data)
    try:
        if not owned_containers(data):
            print("Pulling pinned official images for the owned RM-136 project...", flush=True)
            compose(data, ["pull"])
        print("Starting or reconciling six isolated local services...", flush=True)
        compose(data, ["up", "-d", "--wait", "--wait-timeout", "180"])
        check_status(data)
    except Exception:
        # Preserve state for inspection and an explicit owned-only `down`.
        raise


def down() -> None:
    data = load_state()
    if data is None:
        print("No owned RM-136 stack state exists")
        return
    docker_ready()
    owned_containers(data)
    owned_resources(data)
    compose(data, ["down", "--volumes"])
    if owned_containers(data):
        raise RuntimeError("owned containers remain after Compose down")
    for kind, names in (
        ("volume", [f"{data['project']}_{name}" for name in VOLUMES]),
        ("network", [f"{data['project']}_default"]),
    ):
        existing = set(run(["docker", kind, "ls", "--format", "{{.Name}}"]).stdout.splitlines())
        for name in names:
            if name in existing:
                raise RuntimeError(f"owned {kind} {name} remains after Compose down")
    if STATE.resolve().parent != STATE_DIR.resolve():
        raise RuntimeError("state path escaped its temporary directory")
    STATE.unlink(missing_ok=True)
    print(f"Removed owned project {data['project']} and its declared volumes")


def test() -> int:
    data = load_state()
    if data is None:
        raise RuntimeError("start the owned stack first")
    check_status(data)
    ports = ports_for(data)
    env = os.environ.copy()
    env.update(
        {
            "MEMSPINE_TEST_LIVE_POSTGRES_URL": f"postgresql+psycopg://memspine_local:{data['password']}@127.0.0.1:{ports['POSTGRES']}/memspine_local",
            "MEMSPINE_TEST_LIVE_REDIS_URL": f"redis://127.0.0.1:{ports['REDIS']}/0",
            "MEMSPINE_TEST_LIVE_VALKEY_URL": f"redis://127.0.0.1:{ports['VALKEY']}/0",
            "MEMSPINE_TEST_MONGO_URL": f"mongodb://127.0.0.1:{ports['MONGO']}/?directConnection=true&replicaSet=rs0",
            "MEMSPINE_TEST_NEO4J_URL": f"bolt://127.0.0.1:{ports['NEO4J']}",
            "MEMSPINE_TEST_NEO4J_USER": "neo4j",
            "MEMSPINE_TEST_NEO4J_PASSWORD": str(data["password"]),
            "MEMSPINE_TEST_TEMPORAL_ADDRESS": f"127.0.0.1:{ports['TEMPORAL']}",
            "PYTHONPATH": str(ROOT / "src"),
        }
    )
    selected = [
        "tests/integration/providers/test_provider_live_contracts.py",
        "tests/e2e/test_mongodb_engine.py",
        "tests/integration/test_runner_parity.py",
        "tests/integration/test_temporal_resonate_recovery.py",
    ]
    args = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-rs",
        *selected,
        "-k",
        "postgres_storage_contract or neo4j_graph_contract or remote_cache_contract or mongodb or temporal",
    ]
    result = run(args, env=env, check=False)
    output = result.stdout + result.stderr
    for secret in (str(data["password"]), env["MEMSPINE_TEST_LIVE_POSTGRES_URL"]):
        output = output.replace(secret, "<redacted>")
    print(output)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("config", "up", "status", "test", "down"))
    args = parser.parse_args()
    try:
        if args.command == "config":
            docker_ready()
            compose(load_state() or new_state(), ["config", "--quiet"])
            print("Compose config valid")
        elif args.command == "up":
            up()
        elif args.command == "status":
            data = load_state()
            if data is None:
                raise RuntimeError("no owned RM-136 stack state exists")
            check_status(data)
        elif args.command == "test":
            return test()
        else:
            down()
    except (RuntimeError, TimeoutError, ValueError) as exc:
        print(f"RM-136: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
