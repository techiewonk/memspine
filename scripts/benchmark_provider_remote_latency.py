"""Measure selected remote provider round trips against owned local containers.

The runner provisions pinned images on loopback and removes only containers it
created. Results describe local disposable services, not production traffic.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import socket
import statistics
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import run_provider_live_contracts as live


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, int(len(ordered) * fraction + 0.999999) - 1)]


def unused_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def measure(
    name: str,
    connect: Callable[[], Any],
    op: Callable[[], Any],
    close: Callable[[], Any],
    samples: int,
    timeout_settings: dict[str, Any],
) -> dict[str, Any]:
    started = time.perf_counter()
    client = await connect()
    connect_ms = (time.perf_counter() - started) * 1000
    durations: list[float] = []
    try:
        for _ in range(samples):
            started = time.perf_counter()
            await op(client)
            durations.append((time.perf_counter() - started) * 1000)
    finally:
        await close(client)
    return {
        "samples": len(durations),
        "connect_ms": connect_ms,
        "operation": name,
        "client_timeout_settings": timeout_settings,
        "p50_ms": statistics.median(durations),
        "p95_ms": percentile(durations, 0.95),
        "p99_ms": percentile(durations, 0.99),
        "min_ms": min(durations),
        "max_ms": max(durations),
    }


async def run_operations(environment: dict[str, str], samples: int) -> dict[str, Any]:
    results: dict[str, Any] = {}

    import psycopg
    import weaviate
    from neo4j import AsyncGraphDatabase
    from opensearchpy import AsyncOpenSearch
    from redis.asyncio import Redis
    from sqlalchemy.engine import make_url
    from weaviate.config import AdditionalConfig, Timeout

    postgres_url = (
        make_url(environment["MEMSPINE_TEST_LIVE_POSTGRES_URL"])
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )

    async def pg_connect() -> Any:
        return await asyncio.to_thread(psycopg.connect, postgres_url, connect_timeout=5)

    async def pg_op(client: Any) -> None:
        def query() -> None:
            with client.cursor() as cursor:
                cursor.execute("SELECT 1")
                if cursor.fetchone()[0] != 1:
                    raise AssertionError("PostgreSQL SELECT 1 returned an unexpected value")

        await asyncio.to_thread(query)

    async def pg_close(client: Any) -> None:
        await asyncio.to_thread(client.close)

    results["postgresql"] = await measure(
        "SQL SELECT 1 round trip",
        pg_connect,
        pg_op,
        pg_close,
        samples,
        {"connect_timeout_s": 5},
    )

    neo_driver: Any = None

    async def neo_connect() -> Any:
        nonlocal neo_driver
        neo_driver = AsyncGraphDatabase.driver(
            environment["MEMSPINE_TEST_NEO4J_URL"],
            auth=(
                environment["MEMSPINE_TEST_NEO4J_USER"],
                environment["MEMSPINE_TEST_NEO4J_PASSWORD"],
            ),
            connection_timeout=5.0,
        )
        await neo_driver.verify_connectivity()
        return neo_driver

    async def neo_op(client: Any) -> None:
        records, _, _ = await client.execute_query("RETURN 1 AS value")
        if records[0]["value"] != 1:
            raise AssertionError("Neo4j RETURN 1 returned an unexpected value")

    async def neo_close(client: Any) -> None:
        await client.close()

    results["neo4j"] = await measure(
        "Cypher RETURN 1 round trip",
        neo_connect,
        neo_op,
        neo_close,
        samples,
        {"connection_timeout_s": 5.0},
    )

    async def redis_measure(provider: str, url: str) -> dict[str, Any]:
        async def connect() -> Any:
            client = Redis.from_url(url, socket_connect_timeout=5.0, socket_timeout=5.0)
            await client.ping()
            return client

        async def op(client: Any) -> None:
            if not await client.ping():
                raise AssertionError(f"{provider} PING returned false")

        async def close(client: Any) -> None:
            await client.aclose()

        return await measure(
            "Redis protocol PING round trip",
            connect,
            op,
            close,
            samples,
            {"socket_connect_timeout_s": 5.0, "socket_timeout_s": 5.0},
        )

    results["redis"] = await redis_measure("Redis", environment["MEMSPINE_TEST_LIVE_REDIS_URL"])
    results["valkey"] = await redis_measure("Valkey", environment["MEMSPINE_TEST_LIVE_VALKEY_URL"])

    async def os_connect() -> Any:
        client = AsyncOpenSearch(
            hosts=[environment["MEMSPINE_TEST_LIVE_OPENSEARCH_URL"]],
            timeout=5.0,
            max_retries=0,
            retry_on_timeout=False,
        )
        if not await client.ping():
            await client.close()
            raise ConnectionError("OpenSearch ping failed")
        return client

    async def os_op(client: Any) -> None:
        if not await client.ping():
            raise AssertionError("OpenSearch ping returned false")

    async def os_close(client: Any) -> None:
        await client.close()

    results["opensearch"] = await measure(
        "OpenSearch HTTP ping",
        os_connect,
        os_op,
        os_close,
        samples,
        {"request_timeout_s": 5.0, "max_retries": 0},
    )

    import urllib.parse

    parsed = urllib.parse.urlsplit(environment["MEMSPINE_TEST_LIVE_WEAVIATE_URL"])
    http_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    grpc_port = int(environment["MEMSPINE_TEST_LIVE_WEAVIATE_GRPC_PORT"])

    async def weaviate_connect() -> Any:
        client = weaviate.use_async_with_local(
            host=parsed.hostname or "127.0.0.1",
            port=http_port,
            grpc_port=grpc_port,
            additional_config=AdditionalConfig(timeout=Timeout(init=5, query=5, insert=5)),
        )
        await client.connect()
        if not await client.is_ready():
            await client.close()
            raise ConnectionError("Weaviate is not ready")
        return client

    async def weaviate_op(client: Any) -> None:
        if not await client.is_ready():
            raise AssertionError("Weaviate readiness check failed")

    async def weaviate_close(client: Any) -> None:
        await client.close()

    results["weaviate"] = await measure(
        "Weaviate SDK readiness round trip",
        weaviate_connect,
        weaviate_op,
        weaviate_close,
        samples,
        {"init_timeout_s": 5, "query_timeout_s": 5, "insert_timeout_s": 5},
    )
    return results


async def measure_unavailable_endpoint() -> dict[str, Any]:
    """Check bounded error return for each configured client against a closed local port."""
    import psycopg
    import weaviate
    from neo4j import AsyncGraphDatabase
    from opensearchpy import AsyncOpenSearch
    from redis.asyncio import Redis
    from weaviate.config import AdditionalConfig, Timeout

    port = unused_loopback_port()
    outcomes: dict[str, Any] = {}

    async def postgres_failure() -> None:
        await asyncio.to_thread(
            psycopg.connect,
            f"postgresql://unused:unused@127.0.0.1:{port}/unused",
            connect_timeout=1,
        )

    async def check(name: str, attempt: Callable[[], Any], timeout_s: float = 1.0) -> None:
        started = time.perf_counter()
        try:
            outcome = await attempt()
        except Exception as exc:
            elapsed = time.perf_counter() - started
            outcomes[name] = {
                "expected_error": type(exc).__name__,
                "elapsed_ms": elapsed * 1000,
                "configured_timeout_bound_s": timeout_s,
                "returned_within_bound": elapsed <= timeout_s + 0.5,
            }
        else:
            elapsed = time.perf_counter() - started
            outcomes[name] = {
                "expected_error": "health probe returned false"
                if outcome is False
                else "unexpected success",
                "elapsed_ms": elapsed * 1000,
                "configured_timeout_bound_s": timeout_s,
                "returned_within_bound": outcome is False and elapsed <= timeout_s + 0.5,
            }

    async def redis_failure() -> None:
        client = Redis.from_url(
            f"redis://127.0.0.1:{port}/0",
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
        )
        try:
            await client.ping()
        finally:
            await client.aclose()

    async def valkey_failure() -> None:
        await redis_failure()

    async def opensearch_failure() -> bool:
        client = AsyncOpenSearch(hosts=[f"http://127.0.0.1:{port}"], timeout=1.0, max_retries=0)
        try:
            return bool(await client.ping())
        finally:
            await client.close()

    async def neo4j_failure() -> None:
        driver = AsyncGraphDatabase.driver(
            f"bolt://127.0.0.1:{port}", auth=("unused", "unused"), connection_timeout=1.0
        )
        try:
            await driver.verify_connectivity()
        finally:
            await driver.close()

    async def weaviate_failure() -> None:
        client = weaviate.use_async_with_local(
            host="127.0.0.1",
            port=port,
            grpc_port=unused_loopback_port(),
            additional_config=AdditionalConfig(timeout=Timeout(init=1, query=1, insert=1)),
        )
        try:
            await client.connect()
        finally:
            await client.close()

    await check("postgresql", postgres_failure, timeout_s=2.5)
    await check("redis", redis_failure)
    await check("valkey", valkey_failure)
    await check("neo4j", neo4j_failure)
    await check("opensearch", opensearch_failure)
    await check("weaviate", weaviate_failure, timeout_s=1.5)
    return {
        "target": "unused loopback port, no service contacted",
        "attempts": outcomes,
        "all_bounded": all(
            value.get("returned_within_bound", False) for value in outcomes.values()
        ),
        "postgresql_driver_setting_s": 1.0,
        "postgresql_observation": "driver connect_timeout=1s returned in about 2s; bound uses 2.5s to capture its effective multi-attempt failure",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=50)
    args = parser.parse_args()
    if not 30 <= args.samples <= 500:
        parser.error("--samples must be in 30..500")
    if subprocess.run(
        ["docker", "version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode:
        parser.error("Docker daemon is unavailable")

    suffix = uuid.uuid4().hex[:10]
    services: list[live.Service] = []
    service_env: dict[str, str] = {}
    images: list[str] = []
    saved_env = {name: os.environ.pop(name, None) for name in live.REQUIRED_SERVICE_ENV}
    started = time.perf_counter()
    try:
        live.provision(pull_missing=False, suffix=suffix, services=services)
        # Recompute environment values from the exact task-owned containers.
        for service in services:
            port = live.container_port(service.container_name, service.internal_port)
            live.wait_tcp(port, timeout_s=180.0 if service.key in {"neo4j", "opensearch"} else 90.0)
            if service.key in {"opensearch", "weaviate"}:
                live.wait_http(
                    port,
                    "/" if service.key == "opensearch" else "/v1/.well-known/ready",
                    timeout_s=180.0 if service.key == "opensearch" else 90.0,
                )
            if service.extra_ports:
                for extra in service.extra_ports:
                    live.wait_tcp(live.container_port(service.container_name, extra), timeout_s=90)
            version = live.service_version(service)
            images.append(f"{service.key}: {live.digest(service.image)} ({version})")
            if service.key == "postgres":
                # The runner keeps its generated user/password only in Service.env.
                from urllib.parse import quote

                user = quote(service.env["POSTGRES_USER"], safe="")
                password = quote(service.env["POSTGRES_PASSWORD"], safe="")
                database = quote(service.env["POSTGRES_DB"], safe="")
                service_env["MEMSPINE_TEST_LIVE_POSTGRES_URL"] = (
                    f"postgresql://{user}:{password}@127.0.0.1:{port}/{database}"
                )
            elif service.key == "neo4j":
                service_env.update(
                    {
                        "MEMSPINE_TEST_NEO4J_URL": f"bolt://127.0.0.1:{port}",
                        "MEMSPINE_TEST_NEO4J_USER": "neo4j",
                        "MEMSPINE_TEST_NEO4J_PASSWORD": service.env["NEO4J_AUTH"].split("/", 1)[1],
                    }
                )
            elif service.key == "redis":
                service_env["MEMSPINE_TEST_LIVE_REDIS_URL"] = f"redis://127.0.0.1:{port}/0"
            elif service.key == "valkey":
                service_env["MEMSPINE_TEST_LIVE_VALKEY_URL"] = f"redis://127.0.0.1:{port}/0"
            elif service.key == "opensearch":
                service_env["MEMSPINE_TEST_LIVE_OPENSEARCH_URL"] = f"http://127.0.0.1:{port}"
            elif service.key == "weaviate":
                service_env["MEMSPINE_TEST_LIVE_WEAVIATE_URL"] = f"http://127.0.0.1:{port}"
                service_env["MEMSPINE_TEST_LIVE_WEAVIATE_GRPC_PORT"] = str(
                    live.container_port(service.container_name, 50051)
                )
        expected = {"postgres", "neo4j", "redis", "valkey", "opensearch", "weaviate"}
        if {service.key for service in services} != expected:
            raise RuntimeError("one or more pinned disposable service images were unavailable")
        operations = asyncio.run(run_operations(service_env, args.samples))
        failures = asyncio.run(measure_unavailable_endpoint())
        report = {
            "schema_version": 1,
            "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "host_controlled": False,
            "services": sorted(expected),
            "service_images_and_versions": images,
            "owned_container_suffix": suffix,
            "samples_per_service": args.samples,
            "operation_results": operations,
            "unavailable_endpoint_results": failures,
            "container_setup_and_measurement_wall_seconds": time.perf_counter() - started,
            "credentials_recorded": False,
            "source_revision": subprocess.run(
                ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
            ).stdout.strip(),
            "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "limitations": [
                "Disposable local Docker services on shared Windows host; these results are not production tail latency or an SLO.",
                "Each measured operation is a small read/health round trip, not the full provider workload.",
                "Unavailable-port tests exercise all six provider clients. PostgreSQL configured connect_timeout=1s and returned its multi-attempt timeout in about 2s; Redis/Valkey, Neo4j, OpenSearch and Weaviate returned within the configured 1s attempt bound (plus 500ms measurement allowance).",
            ],
        }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {key: value for key, value in report.items() if key != "operation_results"},
                indent=2,
            )
        )
        return 0
    finally:
        for service in reversed(services):
            subprocess.run(
                ["docker", "rm", "--force", "--volumes", service.container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        for name, value in saved_env.items():
            if value is not None:
                os.environ[name] = value


if __name__ == "__main__":
    raise SystemExit(main())
