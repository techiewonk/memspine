# Live provider contract gates

`python scripts/run_provider_live_contracts.py` runs the installed embedded
provider contract and runs remote-provider cases only when their local service
environment variables are configured. Missing services appear as pytest
`SKIPPED` results that name the missing environment variable; they are never
counted as successful provider checks.

The embedded case opens SQLite, LanceDB, Tantivy, Ladybug and disk-cache
providers in one Engine. It writes a synthetic memory record, checks semantic
search, restarts the Engine and searches again, then verifies that provider
bindings persisted unchanged. It also checks graph traversal and namespace
isolation, edge deletion, a cache round trip, and that shutdown closes all owned
resources. The PostgreSQL case performs the same write/reopen/search and
binding-persistence checks. Neo4j exercises traversal, namespace isolation and
deletion. Redis and Valkey exercise TTL and cross-deployment cache isolation.
All records and keys are synthetic and scoped to generated live contract
deployment IDs.

Set these variables to opt into external live cases; only the variable names
and presence are reported. The runner redacts configured URLs and credentials
from the pytest output it displays.

| Provider | Required environment variables |
| --- | --- |
| PostgreSQL storage | `MEMSPINE_TEST_LIVE_POSTGRES_URL` |
| Neo4j graph | `MEMSPINE_TEST_NEO4J_URL`, `MEMSPINE_TEST_NEO4J_USER`, `MEMSPINE_TEST_NEO4J_PASSWORD` |
| Redis cache | `MEMSPINE_TEST_LIVE_REDIS_URL` |
| Valkey cache | `MEMSPINE_TEST_LIVE_VALKEY_URL` |

The runner accepts connection strings from the current process but never prints
them. Without explicit service configuration, the corresponding tests skip.

To exercise disposable services with Docker, run:

```powershell
python scripts/run_provider_live_contracts.py --docker
```

The runner uses cached images only by default. Add `--pull-missing` to fetch
missing public images. Containers receive unique names, publish random ports on
`127.0.0.1`, use no host mounts, contain only synthetic data and are removed by
the runner even when the contract suite fails. Existing containers are not
modified. The JSON evidence report is optional:

```powershell
python scripts/run_provider_live_contracts.py --docker --pull-missing --report "$env:TEMP/provider-live-report.json"
```

The report records image identities, started provider kinds, pytest counts and
skip reasons; it contains no URLs, usernames or passwords. The gate does not
copy databases, re-embed existing data, call paid model APIs or change external
service configuration.

The pgvector adapter has a separate focused contract. Start a disposable
PostgreSQL server with the pgvector extension, then set
`MEMSPINE_TEST_LIVE_POSTGRES_URL` and run:

```powershell
$env:MEMSPINE_TEST_LIVE_POSTGRES_URL = "postgresql+psycopg://USER:PASSWORD@127.0.0.1:5432/DATABASE"
python -m pytest -p no:deepeval -o addopts='' -q -rs --tb=short tests/integration/providers/test_pgvector_contract.py
Remove-Item Env:MEMSPINE_TEST_LIVE_POSTGRES_URL
```

The test uses synthetic vectors. It bootstraps its owned projection with
`provision_projection: true` and `create_extension: true`, then restarts with
both settings false to verify normal serving reuses the same table without
startup DDL. The sanitized run record, including the exact image digest and
server version, is in
[`docs/provider_verification/pgvector-live-contract.json`](../provider_verification/pgvector-live-contract.json).
The test verifies deletion from the current table, but PostgreSQL MVCC, WAL,
replicas and backups remain outside its physical-erasure proof.

OpenSearch and Weaviate also have a focused isolated gate:

```powershell
python scripts/run_remote_provider_contracts.py --report "$env:TEMP/remote-provider-report.json"
```

This gate starts only the pinned OpenSearch 2.19.5 and Weaviate 1.32.4 images.
It verifies lexical CRUD and namespace/deployment isolation in OpenSearch;
Weaviate vector and BM25 CRUD, namespace/deployment isolation, and an Engine
startup/write/search/shutdown path using both selected remote providers. The
latest run passed all three cases. Sanitized image digests and runtime versions
are recorded alongside the existing live evidence in
[`docs/provider_verification/live-provider-contracts.json`](../provider_verification/live-provider-contracts.json).

The recorded Docker run passed all five cases. Its sanitized image digests,
service versions, test summary and environment-variable presence are in
[`docs/provider_verification/live-provider-contracts.json`](../provider_verification/live-provider-contracts.json).
