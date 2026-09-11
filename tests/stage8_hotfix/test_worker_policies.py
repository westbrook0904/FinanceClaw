"""根会话澄清门控与同一只读 Worker 的原生并发调用。"""

import json
from threading import Barrier
from typing import ClassVar

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from financeclaw.agent_server.agents.offline import OfflineFinanceModel
from financeclaw.agent_server.agents.ziwei_offline import offline_chart_call
from financeclaw.agent_server.tools.subgraph_scope import active_scope
from financeclaw.shared.artifacts.repository import ArtifactNotFound
from financeclaw.shared.execution_ledger.repository import ExecutionConflict
from financeclaw.shared.releases.subgraphs import is_parallel_read_worker
from tests.stage6fix.test_batch_tools import BatchModel, call
from tests.stage7.support import components, request
from tests.stage8_hotfix.test_production_subgraphs import root_graph


@pytest.fixture
def stack(tmp_path):
    """使用生产默认的并发容量，而非其他恢复测试刻意设置的单资源槽。"""
    value = components(tmp_path)
    value.agent_factory.memory_service = None
    yield value
    value.database.close()


class FabricatingRootModel(OfflineFinanceModel):
    """故意忽略澄清结果，第二轮补造参数；框架必须阻止第二轮发生。"""

    calls: list[dict]
    rounds: ClassVar[int] = 0

    def _generate(self, messages, *args, **kwargs):
        """第一轮缺参，后续直接使用虚构的完整出生资料。"""
        type(self).rounds += 1
        receipts = [m for m in messages if isinstance(m, ToolMessage)]
        if receipts and json.loads(receipts[-1].content).get("outcome") == "chart_only":
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="已完成排盘"))])
        if not receipts:
            items = self.calls
        else:
            items = [
                call(
                    "call_agent__ziwei_doushu_agent",
                    99,
                    task="虚构资料重试",
                    arguments=request().model_dump(mode="json"),
                )
            ]
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="", tool_calls=items))]
        )


@pytest.mark.asyncio
async def test_root_clarifies_before_model_can_invent_missing_arguments(stack):
    """根模型只有一次派发额度，澄清必须直接中断且没有第二次模型调用。"""
    FabricatingRootModel.rounds = 0
    calls = [call("call_agent__ziwei_doushu_agent", 1, task="请排盘", arguments={})]
    graph, kwargs = root_graph(
        stack, calls, model=FabricatingRootModel(calls=calls), limits={"model": 2}
    )
    result = await graph.ainvoke({"messages": [HumanMessage(content="请排盘")]}, **kwargs)
    receipts = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(receipts) == 1
    public = json.loads(receipts[0].content)
    assert public["outcome"] == "needs_clarification"
    assert result["__interrupt__"][0].value["question"] == public["question"]
    assert result["messages"][-1].tool_calls[0]["name"] == "request_user__clarification"
    assert FabricatingRootModel.rounds == 1
    assert stack.conversation_repository.execution.get("root")["tool_calls"] == 3
    assert len(result["__interrupt__"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("second_complete", [False, True])
async def test_parallel_clarifications_preserve_all_receipts_and_do_not_retry(
    stack, second_complete
):
    """批次汇合后统一提问，已成功的结果仍留在根 state，不触发新一轮工具。"""
    FabricatingRootModel.rounds = 0
    calls = [
        call("call_agent__ziwei_doushu_agent", 1, task="甲排盘", arguments={"subject_label": "甲"}),
        call(
            "call_agent__ziwei_doushu_agent",
            2,
            task="乙排盘",
            arguments=request(subject_label="乙").model_dump(mode="json")
            if second_complete
            else {"subject_label": "乙"},
        ),
    ]
    graph, kwargs = root_graph(stack, calls, model=FabricatingRootModel(calls=calls))
    result = await graph.ainvoke({"messages": [HumanMessage(content="两人排盘")]}, **kwargs)
    assert FabricatingRootModel.rounds == 1
    receipts = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert {m.tool_call_id for m in receipts} == {"call-1", "call-2"}
    public = [json.loads(m.content) for m in receipts]
    if second_complete:
        assert public[1]["outcome"] == "chart_only" and public[1]["charts_used"]
        assert result["__interrupt__"][0].value["question"] == public[0]["question"]
    else:
        assert result["__interrupt__"][0].value["question"] == (
            f"甲：{public[0]['question']}\n\n乙：{public[1]['question']}"
        )


@pytest.mark.asyncio
async def test_clarification_cannot_bypass_cancellation(stack, monkeypatch):
    """不调用第二次模型也必须复验根授权与取消状态。"""
    from financeclaw.agent_server.domains.ziwei.errors import ZiweiError

    def cancelled(*args, **kwargs):
        """在工具执行期间取消，随后让 Worker 返回澄清。"""
        stack.conversation_repository.execution.request_cancel("root")
        raise ZiweiError("ZIWEI_INPUT_INCOMPLETE", "请补充查询日期。", ("target",))

    monkeypatch.setattr(stack.ziwei_service, "validate_input", cancelled)
    calls = [call("call_agent__ziwei_doushu_agent", 1, task="排盘", arguments={})]
    graph, kwargs = root_graph(stack, calls, model=FabricatingRootModel(calls=calls))
    with pytest.raises(ExecutionConflict, match="cancel"):
        await graph.ainvoke({"messages": [HumanMessage(content="排盘")]}, **kwargs)


@pytest.mark.parametrize("capability", ["write", "approval", "interaction", "memory", "nested"])
def test_adding_unsafe_worker_capabilities_disables_parallel_admission(stack, capability):
    """并发资格从固定发布推导，增加写入或交互不能沿用原只读资格。"""
    tool = stack.tool_catalog.resolve("call_agent__ziwei_doushu_agent", "2.2.0").tool
    assert is_parallel_read_worker(tool.declaration)
    value = json.loads(tool.declaration)
    if capability == "write":
        value["tools"][0]["side_effect"] = "write"
    elif capability == "approval":
        value["tools"][0]["approval"] = "always"
    elif capability == "interaction":
        value["profile"]["interaction_points"] = [{"point_id": "question"}]
    elif capability == "memory":
        value["profile"]["memory_policy"] = "stage3-governed-v1"
    else:
        value["profile"]["worker_manifest"] = ["nested"]
    assert not is_parallel_read_worker(json.dumps(value))


@pytest.mark.asyncio
@pytest.mark.parametrize("directive", [False, True])
async def test_parallel_ziwei_workers_isolate_charts_and_share_root_budget(
    stack, monkeypatch, directive
):
    """本命与流年确实同时到达工具，分别返回盘面而不覆盖同名子图的 checkpoint。"""
    barrier = Barrier(2, timeout=5)
    seen = []
    calculate = stack.ziwei_service.calculate

    def concurrent(birth, target, level, focus, context):
        """阻止串行执行伪装为并发；记录实际调用归属。"""
        scope = active_scope.get()
        seen.append((scope.identity, scope.tool_call_id, level.value))
        barrier.wait()
        return calculate(birth, target, level, focus, context)

    monkeypatch.setattr(stack.ziwei_service, "calculate", concurrent)
    calls = [
        call(
            "call_agent__ziwei_doushu_agent",
            index,
            task=f"查询{level}命盘",
            arguments=request(level=level, target=target).model_dump(mode="json"),
        )
        for index, level, target in (
            (1, "natal", None),
            (2, "yearly", {"kind": "point", "on_date": "2026-09-09"}),
        )
    ]
    graph, kwargs = root_graph(stack, calls, model=BatchModel(calls=calls))
    message = ("/agent ziwei_doushu_agent " if directive else "") + "查询本命盘和流年盘"
    result = await graph.ainvoke({"messages": [HumanMessage(content=message)]}, **kwargs)
    receipts = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(receipts) == 2 and all(m.status == "success" for m in receipts)
    charts = {m.tool_call_id: json.loads(m.content)["charts_used"][0] for m in receipts}
    assert charts["call-1"]["level"] == "natal"
    assert charts["call-2"]["level"] == "yearly"
    assert charts["call-1"]["chart_id"] != charts["call-2"]["chart_id"]
    persisted = {}
    for checkpoint in graph.checkpointer.list(kwargs["config"]):
        public = checkpoint.checkpoint["channel_values"].get("ziwei_result")
        if public and public.get("charts_used"):
            persisted.setdefault(checkpoint.config["configurable"]["checkpoint_ns"], set()).add(
                public["charts_used"][0]["chart_id"]
            )
    assert len(persisted) == 2
    assert list(persisted.values()).count({charts["call-1"]["chart_id"]}) == 1
    assert list(persisted.values()).count({charts["call-2"]["chart_id"]}) == 1
    assert len({identity for identity, _, _ in seen}) == 2
    assert {(call_id, level) for _, call_id, level in seen} == {
        ("call-1", "natal"),
        ("call-2", "yearly"),
    }
    execution = stack.conversation_repository.execution.get("root")
    assert execution["model_calls"] == 6  # root 2 + two evidence loops of 2
    assert execution["tool_calls"] == 4  # two Worker entries + two chart Tools
    assert not active_scope.get() and not result.get("__interrupt__")


@pytest.mark.asyncio
async def test_parallel_workers_cannot_overdraw_root_tool_budget(stack):
    """两个包装调用可以获准，但叶子调用必须共享同一份原子限额。"""
    calls = [
        call(
            "call_agent__ziwei_doushu_agent",
            index,
            task="排盘",
            arguments=request().model_dump(mode="json"),
        )
        for index in (1, 2)
    ]
    graph, kwargs = root_graph(stack, calls, model=BatchModel(calls=calls), limits={"tool": 2})
    with pytest.raises(ExecutionConflict, match="budget"):
        await graph.ainvoke({"messages": [HumanMessage(content="同时排盘")]}, **kwargs)
    assert stack.conversation_repository.execution.get("root")["tool_calls"] == 2


@pytest.mark.asyncio
async def test_explicit_json_directive_still_allows_exactly_one_worker_invocation(stack):
    """只读并发不扩大用户已给出精确参数的单次指令。"""
    arguments = {"task": "排盘", "arguments": request().model_dump(mode="json")}
    calls = [call("call_agent__ziwei_doushu_agent", index, **arguments) for index in (1, 2)]
    graph, kwargs = root_graph(stack, calls, model=BatchModel(calls=calls))
    message = "/agent ziwei_doushu_agent " + json.dumps(arguments)
    result = await graph.ainvoke({"messages": [HumanMessage(content=message)]}, **kwargs)
    receipts = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(receipts) == 2
    assert all(json.loads(m.content)["error"] == "unsupported_tool_batch" for m in receipts)
    assert stack.conversation_repository.execution.get("root")["tool_calls"] == 0


@pytest.mark.asyncio
async def test_parallel_same_chart_reuses_artifact_without_duplicate_insert(stack, monkeypatch):
    """两个独立解读可引用同一命盘；同时首次生成制品时也应幂等成功。"""
    repository = stack.ziwei_service.artifacts.repository
    get_owned = repository.get_owned
    barrier = Barrier(2, timeout=5)

    def simultaneous_miss(*args):
        """确定性让两次写入都经过不存在分支，复现唯一键竞争。"""
        try:
            return get_owned(*args)
        except ArtifactNotFound:
            barrier.wait()
            raise

    monkeypatch.setattr(repository, "get_owned", simultaneous_miss)
    calls = [
        call(
            "call_agent__ziwei_doushu_agent",
            index,
            task="排盘",
            arguments=request(level="natal", target=None).model_dump(mode="json"),
        )
        for index in (1, 2)
    ]
    graph, kwargs = root_graph(stack, calls, model=BatchModel(calls=calls))
    result = await graph.ainvoke({"messages": [HumanMessage(content="两个独立排盘任务")]}, **kwargs)
    receipts = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(receipts) == 2 and all(m.status == "success" for m in receipts)
    charts = [json.loads(m.content)["charts_used"][0] for m in receipts]
    assert charts[0]["artifact"] == charts[1]["artifact"]
    assert stack.ziwei_service.artifacts.read(
        charts[0]["artifact"]["artifact_id"], context=kwargs["context"]
    )


class ContextReadingZiweiModel(OfflineFinanceModel):
    """检查子模型实际收到的上下文，再直接填写完整 function call。"""

    inputs: ClassVar[list[dict]] = []

    def _generate(self, messages, *args, **kwargs):
        """测试读取合成 JSON 事实，不把模型替身的解析能力作为自然语言验收。"""
        if isinstance(messages[-1], ToolMessage):
            answer = AIMessage(content="已取得盘面")
        else:
            value = json.loads(next(m.content for m in reversed(messages) if m.type == "human"))
            type(self).inputs.append(value)
            content = (
                value["context_refs"][0]["content"]
                if value["context_refs"]
                else value["user_context"]["content"]
            )
            answer = AIMessage(
                content="",
                tool_calls=[offline_chart_call(json.loads(content), "from-context")],
            )
        return ChatResult(generations=[ChatGeneration(message=answer)])


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["current_user", "artifact"])
async def test_child_function_call_uses_original_context_with_unstructured_parent_hints(
    stack, source
):
    """父工具未抽取出生资料，甚至 hints 类型不适于排盘，也不能阻断子图读取上下文。"""
    from financeclaw.agent_server.graphs.ziwei_agent import build_ziwei_agent
    from tests.stage7.support import context

    ContextReadingZiweiModel.inputs = []
    synthetic = request(level="natal", target=None).model_dump(mode="json")
    refs = []
    if source == "artifact":
        metadata = stack.ziwei_service.artifacts.persist(
            synthetic,
            context=context(),
            source_type="test",
            source_id="synthetic-input",
            idempotency_key="context-fixture",
        )
        refs = [f"artifact:{metadata.artifact_id}@{metadata.content_hash}"]
    original = (
        json.dumps(synthetic, ensure_ascii=False) if source == "current_user" else "按资料排本命盘"
    )
    calls = [
        call(
            "call_agent__ziwei_doushu_agent",
            1,
            task="读取所给资料排盘",
            arguments={"birth": "见用户原问题或引用", "additional_hint": "不要解读"},
            context_refs=refs,
        )
    ]
    graph, kwargs = root_graph(stack, calls)
    tool = stack.tool_catalog.resolve("call_agent__ziwei_doushu_agent").tool
    tool.graph = build_ziwei_agent(
        stack.agent_factory,
        tool.release,
        stack.ziwei_service,
        model=ContextReadingZiweiModel(),
    )
    result = await graph.ainvoke(
        {
            "messages": [
                HumanMessage(content="不属于当前任务的旧消息"),
                HumanMessage(content=original, id="original"),
            ]
        },
        **kwargs,
    )
    public = json.loads(next(m.content for m in result["messages"] if isinstance(m, ToolMessage)))
    assert public["outcome"] == "chart_only"
    assert len(ContextReadingZiweiModel.inputs) == 1
    value = ContextReadingZiweiModel.inputs[0]
    assert value["user_context"] == {"message_id": "original", "content": original}
    assert value["arguments"]["birth"] == "见用户原问题或引用"
    assert value["task"] == "读取所给资料排盘"
    assert "不属于当前任务的旧消息" not in json.dumps(value, ensure_ascii=False)
    assert [item["ref"] for item in value["context_refs"]] == refs
    assert stack.conversation_repository.execution.get("root")["model_calls"] == 4


@pytest.mark.asyncio
async def test_context_reference_cannot_read_another_subject_artifact(stack):
    """上下文扩展继续遵守原有引用归属校验，父 Agent 不能只凭 Artifact ID 扩权。"""
    from tests.stage7.support import context

    metadata = stack.ziwei_service.artifacts.persist(
        request().model_dump(mode="json"),
        context=context(subject_id="another-subject"),
        source_type="test",
        source_id="private-input",
        idempotency_key="private-fixture",
    )
    calls = [
        call(
            "call_agent__ziwei_doushu_agent",
            1,
            task="读取资料",
            arguments={},
            context_refs=[f"artifact:{metadata.artifact_id}@{metadata.content_hash}"],
        )
    ]
    graph, kwargs = root_graph(stack, calls)
    with pytest.raises(ArtifactNotFound):
        await graph.ainvoke({"messages": [HumanMessage(content="排盘")]}, **kwargs)
