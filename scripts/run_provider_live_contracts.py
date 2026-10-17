#!/usr/bin/env python3
"""Run the provider-live contract matrix, optionally with disposable Docker services.

Without ``--docker`` only explicitly configured ``MEMSPINE_TEST_*`` services are
tested; each missing service is reported by pytest as a skip. ``--docker`` starts
isolated containers on loopback-only ephemeral ports and removes only containers
created by this invocation. Missing images are skipped unless ``--pull-missing``
is supplied.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TESTS = (
    "tests/integration/providers/test_provider_live_contracts.py",
    "tests/integration/providers/test_remote_search_live_contracts.py",
)
MANAGED_LABEL = "memspine.live-provider-contract=true"


@dataclass
class Service:
    key: str
    image: str
    container_name: str
    internal_port: int
    env: dict[str, str]
    extra_ports: tuple[int, ...] = ()


def run(args: list[str], *, check: bool = True, capture: bool = True) -> str:
    result = subprocess.run(
        args,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        check=False,
    )
    if check and result.returncode:
        raise RuntimeError(f"command failed with exit code {result.returncode}: {args[0]}")
    return result.stdout.strip() if result.stdout else ""


def image_present(image: str) -> bool:
    return subprocess.run(
        ["docker", "image", "inspect", image],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def select_image(primary: str, fallback: str | None, *, pull_missing: bool) -> str | None:
    for image in (primary, fallback):
        if image and image_present(image):
            return image
    if not pull_missing:
        return None
    target = primary
    result = subprocess.run(
        ["docker", "image", "pull", target],
        cwd=ROOT,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return target if result.returncode == 0 else None


def container_port(name: str, internal_port: int) -> int:
    output = run(["docker", "port", name, f"{internal_port}/tcp"])
    match = re.search(r":(\d+)$", output.splitlines()[0])
    if match is None:
        raise RuntimeError("Docker did not report a published loopback port")
    return int(match.group(1))


def wait_tcp(port: int, *, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return
        except OSError:
            time.sleep(0.5)
    raise TimeoutError(f"local container port {port} did not become ready")


def wait_http(port: int, path: str, *, timeout_s: float) -> None:
    from urllib.request import urlopen

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}{path}", timeout=2):
                return
        except Exception:
            time.sleep(1.0)
    raise TimeoutError(f"local provider HTTP health endpoint on port {port} did not become ready")


def start_service(
    key: str,
    image: str,
    internal_port: int,
    env: dict[str, str],
    suffix: str,
    *,
    extra_ports: tuple[int, ...] = (),
) -> Service:
    name = f"memspine-live-{suffix}-{key}"
    args = ["docker", "run", "--detach", "--name", name, "--label", MANAGED_LABEL]
    args += ["--publish", f"127.0.0.1::{internal_port}"]
    for extra_port in extra_ports:
        args += ["--publish", f"127.0.0.1::{extra_port}"]
    for name_key, value in env.items():
        args += ["--env", f"{name_key}={value}"]
    args.append(image)
    try:
        run(args)
    except RuntimeError:
        subprocess.run(
            ["docker", "rm", "--force", name],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        raise
    return Service(key, image, name, internal_port, env, extra_ports)


def digest(image: str) -> str:
    result = run(["docker", "image", "inspect", "--format", "{{.Id}} {{.RepoDigests}}", image])
    return result


def service_version(service: Service) -> str:
    if service.key in {"opensearch", "weaviate"}:
        from urllib.request import urlopen

        port = container_port(service.container_name, service.internal_port)
        path = "/" if service.key == "opensearch" else "/v1/meta"
        try:
            with urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
                payload = json.load(response)
            return str(payload.get("version") or payload.get("Version") or "runtime version unavailable")
        except Exception:
            return "runtime version unavailable"
    command_by_service = {
        "postgres": ["postgres", "--version"],
        "neo4j": ["neo4j", "--version"],
        "redis": ["redis-server", "--version"],
        "valkey": ["valkey-server", "--version"],
    }
    output = run(
        ["docker", "exec", service.container_name, *command_by_service[service.key]],
        check=False,
    )
    return output.splitlines()[0] if output else "runtime version unavailable"


def provision(
    *, pull_missing: bool, suffix: str, services: list[Service]
) -> tuple[list[Service], dict[str, str], list[str]]:
    specs = (
        ("postgres", "MEMSPINE_TEST_LIVE_POSTGRES_URL", "pgvector/pgvector:pg16", "postgres:16-alpine", 5432),
        ("neo4j", "MEMSPINE_TEST_NEO4J_URL", "neo4j:5", "neo4j:5-community", 7687),
        ("redis", "MEMSPINE_TEST_LIVE_REDIS_URL", "redis:7-alpine", None, 6379),
        ("valkey", "MEMSPINE_TEST_LIVE_VALKEY_URL", "valkey/valkey:8.1", "valkey/valkey:8-alpine", 6379),
        ("opensearch", "MEMSPINE_TEST_LIVE_OPENSEARCH_URL", "opensearchproject/opensearch:2.19.5", None, 9200),
        ("weaviate", "MEMSPINE_TEST_LIVE_WEAVIATE_URL", "cr.weaviate.io/semitechnologies/weaviate:1.32.4", None, 8080),
    )
    child_env: dict[str, str] = {}
    images: list[str] = []
    for key, url_var, primary, fallback, port in specs:
        if os.environ.get(url_var):
            continue
        if key == "neo4j" and any(
            os.environ.get(name)
            for name in ("MEMSPINE_TEST_NEO4J_USER", "MEMSPINE_TEST_NEO4J_PASSWORD")
        ):
            continue
        image = select_image(primary, fallback, pull_missing=pull_missing)
        if image is None:
            print(f"UNAVAILABLE {key}: Docker image is not cached; pass --pull-missing to fetch the public image")
            continue
        images.append(f"{key}: {digest(image)}")
        if key == "postgres":
            user, password, database = "memspine_live", secrets.token_hex(16), "memspine_live"
            service = start_service(
                key,
                image,
                port,
                {"POSTGRES_USER": user, "POSTGRES_PASSWORD": password, "POSTGRES_DB": database},
                suffix,
            )
            services.append(service)
            host_port = container_port(service.container_name, port)
            child_env[url_var] = f"postgresql+psycopg://{user}:{password}@127.0.0.1:{host_port}/{database}"
        elif key == "neo4j":
            password = secrets.token_hex(16)
            service = start_service(
                key,
                image,
                port,
                {
                    "NEO4J_AUTH": f"neo4j/{password}",
                    "NEO4J_server_memory_heap_initial__size": "256m",
                    "NEO4J_server_memory_heap_max__size": "512m",
                    "NEO4J_server_memory_pagecache_size": "256m",
                },
                suffix,
            )
            services.append(service)
            host_port = container_port(service.container_name, port)
            child_env.update(
                {
                    "MEMSPINE_TEST_NEO4J_URL": f"bolt://127.0.0.1:{host_port}",
                    "MEMSPINE_TEST_NEO4J_USER": "neo4j",
                    "MEMSPINE_TEST_NEO4J_PASSWORD": password,
                }
            )
        elif key == "opensearch":
            service = start_service(
                key,
                image,
                port,
                {
                    "discovery.type": "single-node",
                    "plugins.security.disabled": "true",
                    "DISABLE_INSTALL_DEMO_CONFIG": "true",
                    "OPENSEARCH_JAVA_OPTS": "-Xms512m -Xmx512m",
                },
                suffix,
            )
            services.append(service)
            host_port = container_port(service.container_name, port)
            child_env[url_var] = f"http://127.0.0.1:{host_port}"
        elif key == "weaviate":
            service = start_service(
                key,
                image,
                port,
                {
                    "AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED": "true",
                    "PERSISTENCE_DATA_PATH": "/var/lib/weaviate",
                    "CLUSTER_HOSTNAME": "node1",
                    "DEFAULT_VECTORIZER_MODULE": "none",
                },
                suffix,
                extra_ports=(50051,),
            )
            services.append(service)
            host_port = container_port(service.container_name, port)
            child_env[url_var] = f"http://127.0.0.1:{host_port}"
            child_env["MEMSPINE_TEST_LIVE_WEAVIATE_GRPC_PORT"] = str(
                container_port(service.container_name, 50051)
            )
        else:
            service = start_service(key, image, port, {}, suffix)
            services.append(service)
            host_port = container_port(service.container_name, port)
            child_env[url_var] = f"redis://127.0.0.1:{host_port}/0"
    return services, child_env, images


def redact(text: str, env: dict[str, str]) -> str:
    secrets_to_hide = [
        value
        for name, value in env.items()
        if any(token in name for token in ("URL", "PASSWORD", "TOKEN", "KEY")) and value
    ]
    for value in sorted(secrets_to_hide, key=len, reverse=True):
        text = text.replace(value, "<redacted>")
    text = re.sub(
        r"\b((?:postgres(?:ql)?|redis|valkey|bolt(?:\+s)?)://)[^/@\s]+@",
        r"\1<redacted>@",
        text,
        flags=re.IGNORECASE,
    )
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker", action="store_true", help="start disposable local provider containers")
    parser.add_argument("--pull-missing", action="store_true", help="pull missing public Docker images (implies --docker)")
    parser.add_argument("--report", type=Path, help="write a JSON evidence report to this path")
    options = parser.parse_args()
    if options.pull_missing:
        options.docker = True

    services: list[Service] = []
    provided_env: dict[str, str] = {}
    image_report: list[str] = []
    suffix = uuid.uuid4().hex[:10]
    child_env = os.environ.copy()
    child_env.update(
        {
            "PYTHONPATH": os.pathsep.join(("src", "evals")),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }
    )
    report: dict[str, Any] = {
        "docker": options.docker,
        "images": [],
        "services_started": [],
        "pytest_exit_code": None,
        "environment_variables": {
            name: bool(os.environ.get(name))
            for name in (
                "MEMSPINE_TEST_LIVE_POSTGRES_URL",
                "MEMSPINE_TEST_NEO4J_URL",
                "MEMSPINE_TEST_NEO4J_USER",
                "MEMSPINE_TEST_NEO4J_PASSWORD",
                "MEMSPINE_TEST_LIVE_REDIS_URL",
                "MEMSPINE_TEST_LIVE_VALKEY_URL",
                "MEMSPINE_TEST_LIVE_OPENSEARCH_URL",
                "MEMSPINE_TEST_LIVE_WEAVIATE_URL",
                "MEMSPINE_TEST_LIVE_WEAVIATE_GRPC_PORT",
            )
        },
    }
    try:
        if options.docker:
            if subprocess.run(["docker", "version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode:
                print("UNAVAILABLE Docker daemon; remote provider cases will be pytest SKIP")
            else:
                services, provided_env, image_report = provision(
                    pull_missing=options.pull_missing, suffix=suffix, services=services
                )
                for image_line in image_report:
                    print(f"IMAGE {image_line}")
                child_env.update(provided_env)
                print(
                    "CHILD_ENV "
                    + ", ".join(
                        f"{name}={'present' if child_env.get(name) else 'missing'}"
                        for name in (
                            "MEMSPINE_TEST_LIVE_POSTGRES_URL",
                            "MEMSPINE_TEST_NEO4J_URL",
                            "MEMSPINE_TEST_NEO4J_USER",
                            "MEMSPINE_TEST_NEO4J_PASSWORD",
                            "MEMSPINE_TEST_LIVE_REDIS_URL",
                            "MEMSPINE_TEST_LIVE_VALKEY_URL",
                            "MEMSPINE_TEST_LIVE_OPENSEARCH_URL",
                            "MEMSPINE_TEST_LIVE_WEAVIATE_URL",
                            "MEMSPINE_TEST_LIVE_WEAVIATE_GRPC_PORT",
                        )
                    )
                )
                for service in services:
                    host_port = container_port(service.container_name, service.internal_port)
                    wait_tcp(
                        host_port,
                        timeout_s=180.0 if service.key in {"neo4j", "opensearch"} else 90.0,
                    )
                    for extra_port in service.extra_ports:
                        wait_tcp(container_port(service.container_name, extra_port), timeout_s=90.0)
                    if service.key in {"opensearch", "weaviate"}:
                        wait_http(
                            host_port,
                            "/" if service.key == "opensearch" else "/v1/.well-known/ready",
                            timeout_s=180.0 if service.key == "opensearch" else 90.0,
                        )
                    report["services_started"].append(service.key)
                    version_line = f"{service.key} runtime: {service_version(service)}"
                    image_report.append(version_line)
                    print(f"VERSION {version_line}")
                report["images"] = image_report
                report["environment_variables"] = {
                    name: bool(child_env.get(name))
                    for name in (
                        "MEMSPINE_TEST_LIVE_POSTGRES_URL",
                        "MEMSPINE_TEST_NEO4J_URL",
                        "MEMSPINE_TEST_NEO4J_USER",
                        "MEMSPINE_TEST_NEO4J_PASSWORD",
                        "MEMSPINE_TEST_LIVE_REDIS_URL",
                        "MEMSPINE_TEST_LIVE_VALKEY_URL",
                        "MEMSPINE_TEST_LIVE_OPENSEARCH_URL",
                        "MEMSPINE_TEST_LIVE_WEAVIATE_URL",
                        "MEMSPINE_TEST_LIVE_WEAVIATE_GRPC_PORT",
                    )
                }

        command = [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:deepeval",
            "-o",
            "addopts=",
            "-q",
            "-rs",
            "--tb=short",
            "-m",
            "provider_live",
            *TESTS,
        ]
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=child_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        output = redact(result.stdout or "", {**os.environ, **provided_env})
        print(output, end="" if output.endswith("\n") else "\n")
        report["pytest_exit_code"] = result.returncode
        report["pytest_summary"] = next(
            (line for line in reversed(output.splitlines()) if " passed" in line or " failed" in line),
            "NO SUMMARY",
        )
        report["pytest_skips"] = [line for line in output.splitlines() if "SKIPPED" in line or "Skipped:" in line]
        report["image_digests"] = image_report
        if options.report:
            options.report.parent.mkdir(parents=True, exist_ok=True)
            options.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        return result.returncode
    finally:
        for service in reversed(services):
            subprocess.run(
                ["docker", "rm", "--force", service.container_name],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )


if __name__ == "__main__":
    raise SystemExit(main())
