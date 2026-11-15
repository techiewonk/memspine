# ADR-096 local service stack (RM-136)

This is disposable **local-service** evidence for contracts that need Temporal dev,
Postgres, Redis, Valkey, MongoDB, or Neo4j. It does not prove cluster failover,
canary deployment, or production readiness.

## Run

Start Docker Desktop's Linux daemon, then use the repository environment's Python:

```powershell
.venv\Scripts\python.exe scripts\local_services.py config
.venv\Scripts\python.exe scripts\local_services.py up
.venv\Scripts\python.exe scripts\local_services.py status
.venv\Scripts\python.exe scripts\local_services.py test
.venv\Scripts\python.exe scripts\local_services.py down
```

`up` is repeatable: if this repository's project is already running, it checks
readiness instead of creating another stack. `down` checks the owner label before
removing the project's containers and declared volumes. It never prunes Docker,
stops another project, or clears global Docker state. The launcher keeps a private
state file in the user's temporary directory. Its credentials are passed only to
Docker and the child test process; command output does not print them. `down`
removes that state file. Keep it until cleanup, including after a failed `up`.

All ports bind to `127.0.0.1` and are chosen from currently unused ports. Check
`status` for the assigned ports. The MongoDB URL uses `directConnection=true`
because the one-member replica set advertises its Compose-network hostname.
The launcher initiates `rs0` once and waits for PRIMARY on every startup.

`test` runs selected native Postgres, Redis, Valkey, Neo4j and MongoDB contracts,
plus the Temporal integration cases. Optional SDK tests report pytest skips when
their SDK extras are absent; a skip is not a pass. The selector is scoped to
service-owned tests and is not the full strict native or deployment gate.

## Images and provenance

Postgres (pgvector), Neo4j, Redis and Valkey use the exact digest pins in
`.github/workflows/provider-native.yml`. MongoDB uses the Docker Official Image
`mongo:7.0.41-jammy`, digest
`sha256:52ba5332636758f2375228827d5cf7460e25c02063ba19c243e5d262cf4e7924`.
Temporal uses the publisher's `temporalio/temporal:1.10.1`, digest
`sha256:b228dadf9cdb6c7c02a1394b89dd76f827aac9760e458fce42f0899a10141225`.
The latter two digests were read from the Docker Hub repository tag API on
2026-11-15; the [MongoDB image](https://hub.docker.com/_/mongo) and
[Temporal CLI image](https://hub.docker.com/r/temporalio/temporal) pages identify
their publishers. [Temporal's CLI README](https://github.com/temporalio/cli/blob/main/README.md)
documents the `server start-dev --ip 0.0.0.0` invocation.

The stack is intentionally independent of the existing CI runner. It uses only
its own Compose project, network, and named volumes. The launcher does not write
an `.env` file or modify the Engine configuration.
