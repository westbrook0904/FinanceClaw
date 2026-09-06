"""B 阶段任务隔离、引用版本、结构化结果和低成本提槽的验收。"""

import json
from hashlib import sha256

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel, ConfigDict, ValidationError

from financeclaw.application.delegation_context import resolve_context_refs
from financeclaw.application.execution_service import agent_snapshot, verify_agent_snapshot
from financeclaw.kernel import ConversationTurnRequest, DataClassification, ExecutionContext
from financeclaw.modules.delegation import HANDOFF_ADAPTER, AgentHandoff, AgentHandoffV2
from financeclaw.modules.delegation.market_research import MarketResearchResult
from financeclaw.modules.execution import ExecutionConflict, snapshot_context
from financeclaw.orchestration.agents import AgentProfileCatalog
from tests.stage4.test_delegation import SCOPES
from tests.stage6fix.test_execution_recovery import OWNER, stack, started_child


def test_v1_v2_discriminator_and_outcome_contracts():
    """同 kind 的协议靠 schema_version 分流；损坏数据不是成功或合法提槽。"""
    legacy = AgentHandoff(
        handoff_id="d",
        parent_run_id="p",
        parent_turn_id="t",
        conversation_id="c",
        agent_id="research",
        task="bounded",
    )
    assert isinstance(HANDOFF_ADAPTER.validate_python(legacy.model_dump()), AgentHandoff)
    typed = {
        **legacy.model_dump(),
        "schema_version": 2,
        "target_version": "1.2.0",
        "arguments": {"symbol": "AAPL"},
    }
    assert isinstance(HANDOFF_ADAPTER.validate_python(typed), AgentHandoffV2)
    with pytest.raises(ValidationError):
        HANDOFF_ADAPTER.validate_python({**typed, "target_version": None})
    for invalid in (
        {"outcome": "success", "summary": "unsupported claim"},
        {"outcome": "needs_clarification", "question": "?"},
        {"outcome": "partial"},
    ):
        with pytest.raises(ValidationError):
            MarketResearchResult.model_validate(invalid)


@pytest.mark.asyncio
async def test_context_refs_are_owned_bounded_versioned_and_forwarded(tmp_path):
    """B01/B02：任务只收显式片段；猜测跨会话 ID、过期摘要和 URL 都不能读取。"""
    components, fake, delegation, service = stack(tmp_path)
    accepted, _, child = await started_child(service, fake)
    context = snapshot_context(service.execution.get(accepted.run_id)["snapshot"])
    message = components.conversation_repository.list_messages(accepted.conversation_id)[0]
    reference = f"message:{message.message_id}@{sha256(message.content.encode()).hexdigest()}"
    resolver = dict(
        context=context,
        conversations=components.conversation_repository,
        artifacts=components.artifact_service,
    )
    assert resolve_context_refs((reference,), **resolver)[0]["content"] == message.content
    for invalid in ("https://example.com", "/etc/passwd", reference[:-64] + "0" * 64):
        with pytest.raises(ValueError):
            resolve_context_refs((invalid,), **resolver)
    with pytest.raises(ValueError, match="size budget"):
        resolve_context_refs((reference,), max_bytes=1, **resolver)
    other = await service.create(**OWNER)
    with pytest.raises(ValueError, match="outside"):
        resolve_context_refs(
            (reference,),
            **{
                **resolver,
                "context": context.model_copy(update={"conversation_id": other.conversation_id}),
            },
        )
    classified = context.model_copy(update={"data_classification": DataClassification.PUBLIC})
    with pytest.raises(PermissionError):
        resolve_context_refs((reference,), **{**resolver, "context": classified})
    # 另一个 handoff 使用准确引用；任务 JSON 包含来源，不包含完整 Journal 字段。
    child["status"] = "success"
    await service.status(accepted.run_id, scopes=SCOPES, **OWNER)
    second = await service.start_turn(
        accepted.conversation_id,
        ConversationTurnRequest(message="use the cited task"),
        scopes=SCOPES,
        idempotency_key="refs",
        **OWNER,
    )
    parent = next(
        r for r in fake.runs.values() if r["metadata"].get("application_run_id") == second.run_id
    )
    parent["interrupts"][0]["value"]["context_refs"] = [reference]
    waiting = await service.status(second.run_id, scopes=SCOPES, **OWNER)
    task = json.loads(fake.create_calls[-1]["input"]["messages"][0]["content"])
    assert set(task) == {"task", "arguments", "authorized_context"}
    assert task["authorized_context"][0]["ref"] == reference
    assert task["authorized_context"][0]["source"]["turn_id"] == message.turn_id
    assert waiting.status == "waiting_child"
    components.database.close()


@pytest.mark.parametrize(
    "outcome", ["partial", "unsupported", "needs_clarification", "invalid", "text_json"]
)
@pytest.mark.asyncio
async def test_structured_result_preserves_domain_semantics(tmp_path, outcome):
    """B04：只校验声明字段；最后一句貌似 JSON 的自然语言不能充当结构化结果。"""
    components, fake, delegation, service = stack(tmp_path)
    accepted, waiting, child = await started_child(service, fake)
    candidate = {
        "outcome": outcome,
        "limitations": ["unavailable historical source"],
        "question": "Which period?",
        "missing_fields": ["analysis_period"],
    }
    child["status"] = "success"
    child["output"] = {"messages": [AIMessage(content=json.dumps(candidate))]}
    if outcome != "text_json":
        child["output"]["structured_response"] = candidate
    await service.status(accepted.run_id, scopes=SCOPES, **OWNER)
    delivered = delegation.repository.get_owned(
        waiting.output["delegation"]["delegation_id"], **OWNER
    )
    assert delivered.delivery_status == "delivered"
    assert delivered.execution_status == (
        "failed" if outcome in {"invalid", "text_json"} else "completed"
    )
    payload = fake.resume_calls[-1]["command"]["resume"]
    if delivered.execution_status == "completed":
        assert payload["output"]["outcome"] == outcome
        assert payload["output"]["limitations"] == candidate["limitations"]
    components.database.close()


class RequiredResearch(BaseModel):
    """新领域所需的精确对象和时间字段；缺字段可以便宜地返回提槽。"""

    model_config = ConfigDict(extra="forbid")
    symbol: str
    analysis_period: str


@pytest.mark.asyncio
async def test_missing_input_completes_child_then_new_turn_redelegates(tmp_path):
    """B03：确定性缺字段预检不启动模型，下一条用户输入产生新 Turn 和 child。"""
    components, fake, delegation, service = stack(tmp_path)
    domain = components.agent_profiles.resolve("market_research_agent").model_copy(
        update={"input_schema": RequiredResearch}
    )
    catalog = AgentProfileCatalog((components.default_agent_profile, domain))
    delegation.agent_profiles = service.agent_profiles = catalog
    conversation = await service.create(**OWNER)
    first = await service.start_turn(
        conversation.conversation_id,
        ConversationTurnRequest(message="research"),
        scopes=SCOPES,
        idempotency_key="missing",
        **OWNER,
    )
    assert (await service.status(first.run_id, scopes=SCOPES, **OWNER)).status == "completed"
    result = fake.resume_calls[-1]["command"]["resume"]
    assert result["status"] == "completed" and result["output"]["outcome"] == "needs_clarification"
    assert result["output"]["missing_fields"] == ["symbol", "analysis_period"]
    assert len(fake.create_calls) == 1  # 没有为缺失输入花费子模型调用。
    second = await service.start_turn(
        conversation.conversation_id,
        ConversationTurnRequest(message="AAPL in 2025"),
        scopes=SCOPES,
        idempotency_key="clarified",
        **OWNER,
    )
    parent = next(
        r for r in fake.runs.values() if r["metadata"].get("application_run_id") == second.run_id
    )
    parent["interrupts"][0]["value"].update(
        schema_version=2,
        target_version=domain.version,
        arguments={"symbol": "AAPL", "analysis_period": "2025"},
    )
    waiting = await service.status(second.run_id, scopes=SCOPES, **OWNER)
    assert waiting.status == "waiting_child"
    assert waiting.output["delegation"]["child_run_id"] != result["child_run_id"]
    task = json.loads(fake.create_calls[-1]["input"]["messages"][0]["content"])
    assert task["arguments"] == {"symbol": "AAPL", "analysis_period": "2025"}
    components.database.close()


def test_release_and_authorization_cannot_drift(tmp_path):
    """A03/B05：新增当前权限不能扩大原授权，代码／配置改版不能恢复旧快照。"""
    components, _, _, service = stack(tmp_path)
    context = ExecutionContext(
        **OWNER,
        scopes=frozenset({"market:read"}),
        turn_id="turn",
        run_id="root",
        root_run_id="root",
    )
    snapshot = agent_snapshot(
        components.default_agent_profile, context, thread_id="thread", input_hash="hash"
    )
    assert snapshot_context(snapshot, frozenset({"*"})).scopes == context.scopes
    assert not snapshot_context(snapshot, frozenset()).scopes
    with pytest.raises(ExecutionConflict):
        snapshot_context({})
    with pytest.raises(ExecutionConflict):
        verify_agent_snapshot(
            components.default_agent_profile.model_copy(
                update={"deployment_revision": "different"}
            ),
            snapshot,
        )
    with pytest.raises(ExecutionConflict):
        verify_agent_snapshot(
            components.default_agent_profile.model_copy(
                update={"configuration_fingerprint": "different"}
            ),
            snapshot,
        )
    with pytest.raises(ExecutionConflict):
        service.execution.get("legacy-without-snapshot")
    components.database.close()


def test_artifact_refs_require_ownership_permission_classification_and_hash(tmp_path):
    """B02：Artifact 归属和内容版本不能通过传入 ID 绕过。"""
    components, _, _, _ = stack(tmp_path)
    context = ExecutionContext(
        **OWNER,
        run_id="artifact-run",
        turn_id="artifact-turn",
        scopes=frozenset({"artifacts:read"}),
    )
    artifact = components.artifact_service.persist(
        {"evidence": "bounded"},
        context=context,
        source_type="test",
        source_id="evidence",
        idempotency_key="one",
    )
    reference = f"artifact:{artifact.artifact_id}@{artifact.content_hash}"
    kwargs = dict(conversations=None, artifacts=components.artifact_service)
    assert "bounded" in resolve_context_refs((reference,), context=context, **kwargs)[0]["content"]
    with pytest.raises(PermissionError):
        resolve_context_refs(
            (reference,), context=context.model_copy(update={"scopes": frozenset()}), **kwargs
        )
    with pytest.raises(LookupError):
        resolve_context_refs(
            (reference,), context=context.model_copy(update={"subject_id": "another"}), **kwargs
        )
    with pytest.raises(ValueError, match="version changed"):
        resolve_context_refs((reference[:-64] + "0" * 64,), context=context, **kwargs)
    with pytest.raises(PermissionError):
        resolve_context_refs(
            (reference,),
            context=context.model_copy(update={"data_classification": DataClassification.PUBLIC}),
            **kwargs,
        )
    components.database.close()
