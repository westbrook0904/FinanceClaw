"""External service probes for PostgreSQL and Redis compatibility."""

from dataclasses import dataclass

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.redis.aio import AsyncRedisStore
from redis.asyncio import Redis

from financeclaw_spike.settings import SpikeSettings


class MissingServiceConfiguration(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ServiceProbeResult:
    postgres: bool
    redis: bool
    checkpoint: bool
    store: bool


async def probe_postgres(dsn: str) -> bool:
    async with await psycopg.AsyncConnection.connect(dsn, connect_timeout=5) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute("SELECT 1")
            row = await cursor.fetchone()
            return row == (1,)


async def probe_redis(url: str) -> bool:
    client = Redis.from_url(url, socket_connect_timeout=5, socket_timeout=5)
    try:
        return bool(await client.ping())
    finally:
        await client.aclose()


async def probe_services(settings: SpikeSettings) -> ServiceProbeResult:
    if settings.postgres_dsn is None or settings.redis_url is None:
        raise MissingServiceConfiguration(
            "FINANCECLAW_SPIKE_POSTGRES_DSN and FINANCECLAW_SPIKE_REDIS_URL are required"
        )
    postgres = await probe_postgres(settings.postgres_dsn.get_secret_value())
    redis = await probe_redis(settings.redis_url.get_secret_value())
    postgres_dsn = settings.postgres_dsn.get_secret_value()
    redis_url = settings.redis_url.get_secret_value()
    async with AsyncPostgresSaver.from_conn_string(postgres_dsn) as checkpointer:
        await checkpointer.setup()
        checkpoint = True
    async with AsyncRedisStore.from_conn_string(redis_url) as store:
        await store.setup()
        await store.aput(("stage0", "probe"), "compatibility", {"ok": True})
        item = await store.aget(("stage0", "probe"), "compatibility")
        store_ok = item is not None and item.value == {"ok": True}
    return ServiceProbeResult(
        postgres=postgres,
        redis=redis,
        checkpoint=checkpoint,
        store=store_ok,
    )
