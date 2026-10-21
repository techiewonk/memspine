from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from memspine.exceptions import ConfigError
from memspine.providers.identity import DEPLOYMENT_METADATA, resolve_deployment


async def test_identity_survives_reopen_and_rejects_conflicting_explicit_owner(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'identity.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(DEPLOYMENT_METADATA.create)

    class Sql:
        pass

    sql = Sql()
    sql.engine = engine
    first = await resolve_deployment(sql, None, "development")
    assert first == await resolve_deployment(sql, None, "development")
    assert first == await resolve_deployment(sql, first, "production")
    with pytest.raises(ConfigError, match="conflicts"):
        await resolve_deployment(sql, "another-owner", "development")
    with pytest.raises(ConfigError, match="production requires"):
        await resolve_deployment(sql, None, "production")
    await engine.dispose()
    reopened = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'identity.db'}")
    sql.engine = reopened
    assert await resolve_deployment(sql, None, "development") == first
    await reopened.dispose()
