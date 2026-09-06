"""Conversation→真实根图→委派→真实紫微图→父恢复，只有 Agent Server 传输为本地适配。"""

import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from financeclaw.application import (
    ConversationService,
    DelegationService,
    ServerRun,
    WorkflowService,
)
from financeclaw.application.execution_service import json_value
from financeclaw.kernel import ConversationTurnRequest, ExecutionContext
from financeclaw.orchestration.agents import OfflineFinanceModel
from financeclaw.orchestration.agents.ziwei_offline import OfflineZiweiModel
from financeclaw.orchestration.graphs.ziwei_agent import build_ziwei_agent
from tests.stage4.test_delegation import FakeDelegationClient
from tests.stage7.support import components, request


class RootZiweiModel(OfflineFinanceModel):
    """仅测试使用的确定性根路由，不将离线关键词解析当作产品模型能力。"""

    parameters: dict

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """第一次委派固定合成任务，恢复时原样表达 outcome。"""
        if isinstance(messages[-1], ToolMessage):
            payload = json.loads(messages[-1].content)
            message = AIMessage(content="紫微子任务结果：" + payload["output"]["outcome"])
        else:
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "delegate_agent__ziwei_doushu_agent",
                        "id": "root-ziwei",
                        "args": {"task": "查看合成样例的日盘", "arguments": self.parameters},
                        "type": "tool_call",
                    }
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=message)])


class LocalGraphClient(FakeDelegationClient):
    """执行真实图和 checkpoint，沿用现有 fake 的运行身份／回执查询。"""

    def __init__(self, graphs):
        """不同 assistant 使用不同图，thread 由会话／委派服务分配。"""
        super().__init__()
        self.graphs = graphs

    async def create_run(self, **kwargs):
        """实际执行模型、工具与 interrupt，不手写伪造的成功盘面。"""
        self.create_calls.append(kwargs)
        graph = self.graphs[kwargs["assistant_id"]]
        output = json_value(
            await graph.ainvoke(
                kwargs["input"],
                context=ExecutionContext.model_validate(kwargs["context"]),
                config={"configurable": {"thread_id": kwargs["thread_id"]}},
            )
        )
        server_id = f"native-{len(self.runs) + 1}"
        interrupts = output.get("__interrupt__", ())
        status = "interrupted" if interrupts else "success"
        self.runs[server_id] = {
            **kwargs,
            "run_id": server_id,
            "status": status,
            "output": output,
            "interrupts": interrupts,
        }
        return ServerRun(server_id, status)

    async def resume_run(self, **kwargs):
        """恢复同一原生中断，委派 Tool 会校验输入摘要、父身份和目标版本。"""
        self.resume_calls.append(kwargs)
        graph = self.graphs[kwargs["assistant_id"]]
        return json_value(
            await graph.ainvoke(
                Command(resume=kwargs["command"]["resume"]),
                context=ExecutionContext.model_validate(kwargs["context"]),
                config={"configurable": {"thread_id": kwargs["thread_id"]}},
            )
        )


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.asyncio
async def test_native_root_child_delivery_and_persistent_budget(tmp_path, missing):
    """验证真实 input envelope、独立线程、完整结果、原授权恢复和重复轮询幂等。"""
    stack = components(tmp_path)
    query = request(mode="interpretation", **({"birth": {}} if missing else {}))
    root = stack.agent_factory.build(
        stack.default_agent_profile, model=RootZiweiModel(parameters=query.model_dump(mode="json"))
    )
    profile = stack.agent_profiles.resolve("ziwei_doushu_agent")
    child = build_ziwei_agent(
        stack.agent_factory,
        profile,
        stack.ziwei_service,
        model=OfflineZiweiModel(),
        checkpointer=InMemorySaver(),
    )
    client = LocalGraphClient({"finance_agent_v1_3_0": root, "ziwei_doushu_agent_v1_0_0": child})
    workflow = WorkflowService(
        client, stack.workflow_repository, stack.workflow_catalog, stack.audit
    )
    delegation = DelegationService(
        client,
        stack.delegation_repository,
        workflow,
        stack.agent_profiles,
        stack.audit,
        conversation_repository=stack.conversation_repository,
        artifact_service=stack.artifact_service,
    )
    service = ConversationService(
        client, stack.conversation_repository, stack.agent_profiles, delegation_service=delegation
    )
    owner = {"tenant_id": "synthetic-tenant", "subject_id": "synthetic-owner"}
    scopes = frozenset({"ziwei:read", "artifacts:read"})
    try:
        conversation = await service.create(**owner)
        accepted = await service.start_turn(
            conversation.conversation_id,
            ConversationTurnRequest(message="请查看合成样例的日盘"),
            idempotency_key="stage7-native",
            scopes=scopes,
            **owner,
        )
        result = None
        for _ in range(5):
            result = await service.status(accepted.run_id, scopes=scopes, **owner)
            if result.status in {"completed", "failed"}:
                break
        assert result.status == "completed"
        payload = client.resume_calls[-1]["command"]["resume"]
        assert len(payload) == 1
        payload = next(iter(payload.values()))
        assert payload["output"]["outcome"] == ("needs_clarification" if missing else "answer")
        assert len(client.create_calls) == 2 and len(client.resume_calls) == 1
        assert client.create_calls[0]["thread_id"] != client.create_calls[1]["thread_id"]
        assert all(
            c["context"]["data_classification"] == "confidential" for c in client.create_calls
        )
        assert all(
            set(c["context"]["scopes"]) == scopes for c in client.create_calls + client.resume_calls
        )
        execution = service.execution.get(accepted.run_id)
        # 根委派与根汇总 2 次；正常子取证 2 次＋finalization 1 次。
        assert execution["model_calls"] == (2 if missing else 5)
        await service.status(accepted.run_id, scopes=scopes, **owner)
        assert len(client.resume_calls) == 1
        assert len(stack.conversation_repository.list_messages(conversation.conversation_id)) == 2
    finally:
        stack.database.close()
