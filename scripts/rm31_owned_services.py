"""Exact-ID ownership for the two isolated RM-31 search containers only."""

from __future__ import annotations

import json
import re
import secrets
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, cast
from urllib.request import urlopen

PINS = {
    "opensearch": "opensearchproject/opensearch@sha256:4ee82ecb35d837a6186c81aaa64c8a5bce71aa956edbd87f1f684ab56af52c44",
    "weaviate": "cr.weaviate.io/semitechnologies/weaviate@sha256:920463cf9a9b2b8d38e5b925d63f6928fa703277c93df11d5a75fc75e1fff758",
}
INTERNAL = {"opensearch": (9200,), "weaviate": (8080, 50051)}
LABEL_OWNER = "memspine.rm31.owner"
LABEL_PROJECT = "memspine.rm31.project"
STATE_DIR = Path(tempfile.gettempdir()) / "memspine-rm31"


def docker(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, check=False)
    if check and result.returncode:
        raise RuntimeError(f"Docker {args[0]} failed with exit {result.returncode}")
    return result


def new_state() -> dict[str, Any]:
    run_id = secrets.token_hex(6)
    return {
        "run_id": run_id,
        "owner": secrets.token_hex(16),
        "project": f"memspine-rm31-{run_id}",
        "containers": {},
    }


def state_path(state: dict[str, Any]) -> Path:
    return STATE_DIR / f"{state['project']}.json"


def save(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = state_path(state)
    temporary = path.with_suffix(".new")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    temporary.replace(path)


def load(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if resolved.parent != STATE_DIR.resolve() or not re.fullmatch(
        r"memspine-rm31-[0-9a-f]{12}\.json", resolved.name
    ):
        raise RuntimeError("invalid RM-31 ownership state path")
    value = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not re.fullmatch(
        r"[0-9a-f]{12}", str(value.get("run_id", ""))
    ):
        raise RuntimeError("invalid RM-31 ownership state")
    if value.get("project") != f"memspine-rm31-{value['run_id']}":
        raise RuntimeError("RM-31 project mismatch")
    if not re.fullmatch(r"[0-9a-f]{32}", str(value.get("owner", ""))):
        raise RuntimeError("invalid RM-31 owner")
    if not isinstance(value.get("containers"), dict) or not set(value["containers"]).issubset(PINS):
        raise RuntimeError("invalid RM-31 container inventory")
    return value


def _inspect(identity: str) -> dict[str, Any] | None:
    result = docker(["inspect", identity], check=False)
    if result.returncode:
        is_id = re.fullmatch(r"[0-9a-f]{64}", identity) is not None
        exact_filter = f"id={identity}" if is_id else f"name=^/{identity}$"
        inventory = docker(
            ["ps", "-a", "--no-trunc", "--filter", exact_filter, "--format", "{{.ID}}"],
            check=False,
        )
        if inventory.returncode:
            raise RuntimeError("Docker inspect failed and exact container inventory is unavailable")
        if inventory.stdout.strip():
            raise RuntimeError("Docker inspect failed for a container still in exact inventory")
        return None
    values = json.loads(result.stdout)
    if not isinstance(values, list) or len(values) != 1:
        raise RuntimeError("Docker inspect returned unexpected count")
    return cast(dict[str, Any], values[0])


def verify(state: dict[str, Any], service: str) -> dict[str, Any] | None:
    entry = state["containers"].get(service)
    if entry is None:
        return None
    expected_name = f"/{state['project']}-{service}"
    cidfile = Path(entry["cidfile"])
    if (
        cidfile.resolve().parent != STATE_DIR.resolve()
        or cidfile.name != f"{state['project']}-{service}.cid"
    ):
        raise RuntimeError("RM-31 cidfile ownership mismatch")
    container_id = entry.get("id")
    if container_id is None and cidfile.exists():
        container_id = cidfile.read_text(encoding="utf-8").strip()
    if container_id is not None and not re.fullmatch(r"[0-9a-f]{64}", str(container_id)):
        raise RuntimeError("invalid RM-31 container ID")
    # A killed create can precede cidfile persistence. Resolve only the exact expected
    # name, then require both random ownership labels and the exact pinned image.
    inspected = _inspect(str(container_id) if container_id else expected_name.lstrip("/"))
    if inspected is None:
        return None
    labels = inspected.get("Config", {}).get("Labels") or {}
    if (
        (container_id is not None and inspected.get("Id") != container_id)
        or inspected.get("Name") != expected_name
        or labels.get(LABEL_OWNER) != state["owner"]
        or labels.get(LABEL_PROJECT) != state["project"]
        or inspected.get("Config", {}).get("Image") != PINS[service]
    ):
        raise RuntimeError(f"RM-31 {service} exact ID/name/label/image ownership mismatch")
    entry["id"] = inspected["Id"]
    save(state)
    return inspected


def _published_port(inspected: dict[str, Any], internal: int) -> int:
    bindings = (inspected.get("NetworkSettings", {}).get("Ports") or {}).get(f"{internal}/tcp")
    if (
        not isinstance(bindings, list)
        or len(bindings) != 1
        or bindings[0].get("HostIp") != "127.0.0.1"
    ):
        raise RuntimeError("RM-31 port is not bound only to loopback")
    port = int(bindings[0]["HostPort"])
    if not 1 <= port <= 65535:
        raise RuntimeError("invalid RM-31 published port")
    return port


def _wait_http(port: int, path: str, deadline: float) -> None:
    while time.monotonic() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.5)
    raise TimeoutError("RM-31 isolated HTTP service was not ready")


def _wait_tcp(port: int, deadline: float) -> None:
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.5)
    raise TimeoutError("RM-31 isolated gRPC port was not ready")


def start(state: dict[str, Any], service: str, *, deadline: float) -> dict[str, Any]:
    if service not in PINS or service in state["containers"]:
        raise RuntimeError("RM-31 service already owned or unknown")
    name = f"{state['project']}-{service}"
    if _inspect(name) is not None:
        raise RuntimeError("RM-31 exact container name already exists")
    if docker(["image", "inspect", PINS[service]], check=False).returncode != 0:
        raise RuntimeError(f"RM-31 pinned {service} image is not cached")
    cidfile = STATE_DIR / f"{name}.cid"
    if cidfile.exists():
        raise RuntimeError("RM-31 exact cidfile already exists")
    state["containers"][service] = {"id": None, "cidfile": str(cidfile)}
    save(state)
    args = [
        "create",
        "--cidfile",
        str(cidfile),
        "--name",
        name,
        "--label",
        f"{LABEL_OWNER}={state['owner']}",
        "--label",
        f"{LABEL_PROJECT}={state['project']}",
    ]
    for internal in INTERNAL[service]:
        args += ["--publish", f"127.0.0.1::{internal}"]
    if service == "opensearch":
        env = {
            "discovery.type": "single-node",
            "plugins.security.disabled": "true",
            "DISABLE_INSTALL_DEMO_CONFIG": "true",
            "OPENSEARCH_JAVA_OPTS": "-Xms512m -Xmx512m",
        }
    else:
        env = {
            "AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED": "true",
            "PERSISTENCE_DATA_PATH": "/var/lib/weaviate",
            "CLUSTER_HOSTNAME": "node1",
            "DEFAULT_VECTORIZER_MODULE": "none",
        }
    for key, value in env.items():
        args += ["--env", f"{key}={value}"]
    docker([*args, PINS[service]])
    container_id = cidfile.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise RuntimeError("Docker did not write an exact RM-31 container ID")
    state["containers"][service]["id"] = container_id
    save(state)
    verify(state, service)
    docker(["start", container_id])
    inspected = verify(state, service)
    if inspected is None:
        raise RuntimeError("owned RM-31 container vanished")
    ports = {str(internal): _published_port(inspected, internal) for internal in INTERNAL[service]}
    state["containers"][service]["ports"] = ports
    save(state)
    _wait_http(
        ports["9200"] if service == "opensearch" else ports["8080"],
        "/" if service == "opensearch" else "/v1/.well-known/ready",
        deadline,
    )
    if service == "weaviate":
        _wait_tcp(ports["50051"], deadline)
    return {
        "container_id": container_id,
        "image_pin": PINS[service],
        "image_id": inspected.get("Image"),
        "ports": ports,
    }


def cleanup(state: dict[str, Any]) -> dict[str, Any]:
    outcomes: dict[str, Any] = {}
    for service in ("weaviate", "opensearch"):
        entry = state["containers"].get(service)
        if entry is None:
            continue
        inspected = verify(state, service)
        if inspected is not None:
            result = docker(["rm", "--force", "--volumes", str(inspected["Id"])], check=False)
            if result.returncode:
                raise RuntimeError(f"RM-31 owned {service} removal failed")
        cidfile = Path(entry["cidfile"])
        if cidfile.resolve().parent != STATE_DIR.resolve():
            raise RuntimeError("RM-31 cidfile escaped state directory")
        cidfile.unlink(missing_ok=True)
        outcomes[service] = "removed" if inspected is not None else "already_absent"
    state_path(state).unlink(missing_ok=True)
    return outcomes
