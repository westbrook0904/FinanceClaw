"""PostgreSQL 支撑的基础协调：SKIP LOCKED 到期领取、epoch fencing 和单调唤醒。"""

import asyncio
from datetime import timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import or_, select

from experiments.stage8.driver import LostReceipt, ProbeDriver
from experiments.stage8.store import Lease, ProbeStore, StaleWriter, now


def claim_due(store: ProbeStore, owner: str, *, ttl: float = 1) -> dict[str, Any] | None:
    """短事务只领取一个到期责任；不在行锁内等待 backend。"""
    with store.sessions.begin() as session:
        row = session.scalar(
            select(Lease)
            .where(
                Lease.stopped.is_(False),
                Lease.due <= now(),
                or_(Lease.until.is_(None), Lease.until <= now()),
            )
            .order_by(Lease.due)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return None
        row.owner, row.until, row.epoch = owner, now() + timedelta(seconds=ttl), row.epoch + 1
        return {"run_id": row.run_id, "owner": owner, "epoch": row.epoch, "wake": row.wake}


def finish(store: ProbeStore, claim: dict[str, Any], delay: float | None) -> bool:
    """旧 worker 不能覆盖新唤醒或续租；parked 保留到期责任。"""
    with store.sessions.begin() as session:
        row = session.scalar(select(Lease).where(Lease.run_id == claim["run_id"]).with_for_update())
        if row.owner != claim["owner"] or row.epoch != claim["epoch"] or row.until <= now():
            return False
        row.stopped = delay is None
        row.due = now() if row.wake != claim["wake"] else now() + timedelta(seconds=delay or 0)
        row.owner, row.until = None, None
        return True


async def run_worker(driver: ProbeDriver, stop: asyncio.Event) -> None:
    """连续消费到期记录；工作崩溃不重置业务命令的 claimed／uncertain。"""
    owner = str(uuid4())
    while not stop.is_set():
        claim = claim_due(driver.store, owner)
        if claim is None:
            await asyncio.sleep(0.02)
            continue
        try:
            delay = await driver.advance(claim["run_id"], claim)
            finish(driver.store, claim, delay)
        except LostReceipt:
            # Simulate a process dying before releasing the lease; a later owner takes over.
            continue
        except StaleWriter:
            continue
