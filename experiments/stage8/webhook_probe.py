"""真实 HTTP 回调能力探针；只报告有实际断言支持的能力。"""

import asyncio
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import httpx

from experiments.stage8.backend import ProbeBackend, release
from experiments.stage8.environment import WebhookSink, agent_server
from financeclaw.kernel.coordination import TaskSubmission, bounded_digest


async def until(predicate: Callable[[], bool], *, timeout: float = 20) -> None:
    """有界等待验证事实，超时算失败而不是跳过。"""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.03)


def submission(target: str) -> TaskSubmission:
    """构造不含模型或身份凭据的固定探针操作。"""
    task_id = "probe-" + uuid4().hex
    value = {"task_id": task_id}
    return TaskSubmission(
        task_id=task_id,
        root_task_id=task_id,
        operation_id="start:" + task_id,
        backend_instance_id="probe",
        release=release(target),
        input=value,
        input_hash=bounded_digest(value),
    )


async def probe_webhooks(backend: ProbeBackend, sink: WebhookSink) -> dict:
    """实测成功、错误、鉴权、重试上限、丢失补偿和 thread ensure 幂等。"""
    report = {}
    for name in ("success", "failure"):
        reference = await backend.submit(submission(name))
        await until(
            lambda reference=reference: any(
                event["notification"]["execution_id"] == reference.execution_id
                for event in sink.events
            )
        )
        event = next(
            event
            for event in sink.events
            if event["notification"]["execution_id"] == reference.execution_id
        )
        report[name] = event["notification"]["status_hint"]
        assert {"run_id", "thread_id", "status", "webhook_sent_at"} <= set(event["fields"])
        assert "values" not in event["notification"] and "kwargs" not in event["notification"]
    assert report == {"success": "success", "failure": "error"}
    report["static_header_and_ingress_minimization"] = True
    async with httpx.AsyncClient(trust_env=False) as client:
        response = await client.post(sink.url, json={"tenant_id": "forged"})
        assert response.status_code == 401
    report["unauthenticated_rejected"] = True
    original_token = sink.token
    sink.token = "intentional-wrong-receiver-token"
    before = sink.unauthorized
    command = submission("success")
    reference = await backend.submit(command)
    await until(lambda: sink.unauthorized > before)
    await asyncio.sleep(0.3)
    sink.token = original_token
    assert not any(
        event["notification"]["execution_id"] == reference.execution_id for event in sink.events
    )
    assert (await backend.lookup(command)) == reference
    assert (await backend.observe(reference)).status == "completed"
    report["auth_failure_leaves_reconcilable_run"] = True
    sink.failures_left = 2
    before = sink.attempts
    reference = await backend.submit(submission("success"))
    await until(
        lambda reference=reference: any(
            event["notification"]["execution_id"] == reference.execution_id for event in sink.events
        )
    )
    assert sink.attempts - before == 3
    report["503_then_success_attempts"] = 3
    sink.failures_left = 10
    before = sink.attempts
    reference = await backend.submit(submission("success"))
    await until(lambda: sink.attempts - before >= 3)
    await asyncio.sleep(0.5)
    assert sink.attempts - before == 3
    sink.failures_left = 0
    assert (await backend.observe(reference)).status == "completed"
    report["503_exhausted_attempts"] = 3
    report["exhausted_callback_requires_compensation"] = True
    # Ensuring the same preallocated thread twice is safe; no run receipt is inferred.
    from uuid import NAMESPACE_URL, uuid5

    command = submission("success")
    thread_id = str(uuid5(NAMESPACE_URL, "stage8:" + command.task_id))
    await backend.native.create_thread(thread_id)
    await backend.native.create_thread(thread_id)
    assert await backend.lookup(command) is None
    report["thread_ensure_is_idempotent_but_not_submission_proof"] = True
    return report


async def probe_outbound_filter(directory: Path, sink: WebhookSink) -> dict:
    """实际启动配置验证，未来版本修复后返回 supported，不硬编码失败结论。"""
    try:
        with agent_server(directory, sink, filter_fields=True) as url:
            backend = ProbeBackend(url, sink.url, directory / "bindings")
            reference = await backend.submit(submission("success"))
            await until(
                lambda: any(
                    event["notification"]["execution_id"] == reference.execution_id
                    for event in sink.events
                )
            )
            event = next(
                event
                for event in sink.events
                if event["notification"]["execution_id"] == reference.execution_id
            )
            assert set(event["fields"]) == {"run_id", "thread_id", "status", "webhook_sent_at"}
            return {"supported": True}
    except RuntimeError:
        log = (directory / "server.log").read_text()
        defect = "webhooks.allowed_fields must be a list of strings. Got: set"
        if defect not in log:
            raise
        return {"supported": False, "reason": defect, "evidence": "actual startup rejected"}
