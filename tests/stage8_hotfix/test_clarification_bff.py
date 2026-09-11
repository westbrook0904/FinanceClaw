"""使用真实根图产生中断，验证 BFF 登记、恢复和最终 Journal 写入。"""

import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.types import Command

from financeclaw.agent_server.agents.offline import OfflineFinanceModel
from financeclaw.agent_server.bootstrap import build_components
from financeclaw.bff.application.runs.bootstrap import build_bff_runs
from financeclaw.kernel.context import ExecutionContext
from financeclaw.shared.releases.interactions import CLARIFICATION_TOOL
from tests.stage6fix.test_batch_tools import BatchModel, call
from tests.stage8_hotfix.test_bff_runs import OWNER, FakeNative, admit, config, response, tick


class QuestioningRoot(OfflineFinanceModel):
    """根主动发现缺参或等待 Worker 返回缺参，两者都必须走原生交互。"""

    worker: bool

    def _generate(self, messages, *args, **kwargs):
        """真实回答返回工具消息后，完成同一任务。"""
        receipts = [m for m in messages if isinstance(m, ToolMessage)]
        if receipts and receipts[-1].name == CLARIFICATION_TOOL:
            answer = json.loads(receipts[-1].content)["answer"]["text"]
            message = AIMessage(content=f"已收到你的补充：{answer}")
        else:
            assert self.worker or CLARIFICATION_TOOL in self._bound_tool_names
            message = AIMessage(
                content="",
                tool_calls=[
                    call("call_agent__market_research_agent", 1, task="研究行情")
                    if self.worker
                    else call(CLARIFICATION_TOOL, 1, question="请问要研究哪只证券？")
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=message)])


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True])
async def test_native_root_clarification_survives_bff_resume_and_journal(tmp_path, worker):
    """没有中断时不能通过；问题不是最终答案，回答按原生 interrupt ID 恢复。"""
    settings = config(tmp_path, ziwei_enabled=False)
    native = FakeNative()
    bff = build_bff_runs(settings, native=native)
    server = build_components(settings, enable_persistence=True)
    server.agent_factory.memory_service = None
    tool = server.tool_catalog.resolve("call_agent__market_research_agent").tool
    tool.graph = server.agent_factory.build(
        tool.release,
        checkpointer=None,
        fallback_models=(),
        model=BatchModel(
            calls=[
                call(
                    "MarketResearchResult",
                    2,
                    outcome="needs_clarification",
                    question="请问要研究哪只证券？",
                    missing_fields=["symbols"],
                )
            ]
        ),
    )
    graph = server.agent_factory.build(
        server.default_agent_profile, model=QuestioningRoot(worker=worker), fallback_models=()
    )

    async def execute_native(run, kwargs):
        """只替换 SDK 传输，根图、checkpoint 和 BFF 生命周期仍执行真实代码。"""
        cfg = {"configurable": {"thread_id": run["thread_id"]}}
        value = kwargs.get("input") or Command(resume=kwargs["command"]["resume"])
        result = await graph.ainvoke(
            value, config=cfg, context=ExecutionContext.model_validate(kwargs["context"])
        )
        checkpoint = await graph.aget_state(cfg)
        run["status"] = "interrupted" if result.get("__interrupt__") else "success"
        native.threads.states[run["thread_id"]] = {
            "metadata": {"run_id": run["run_id"]},
            "checkpoint": checkpoint.config["configurable"],
            "values": {"messages": [m.model_dump(mode="json") for m in result["messages"]]},
            "next": list(checkpoint.next),
            "interrupts": [{"id": i.id, "value": i.value} for i in result.get("__interrupt__", [])],
        }

    native.runs.on_create = execute_native
    try:
        accepted = await admit(bff, message="帮我研究行情")
        await tick(bff)
        await tick(bff)
        status = await bff.runs.status(accepted.run_id, **OWNER)
        assert status.status == "interrupted" and len(status.pending_interactions) == 1
        question = status.pending_interactions[0]
        assert question["question"] == "请问要研究哪只证券？" and question["kind"] == "input"
        assert len(bff.runs.repository.list_messages(accepted.conversation_id)) == 1
        await response(bff, accepted, answer={"text": "AAPL"})
        await tick(bff)
        await tick(bff)
        assert (await bff.runs.status(accepted.run_id, **OWNER)).status == "completed"
        messages = bff.runs.repository.list_messages(accepted.conversation_id)
        assert messages[-1].content == "已收到你的补充：AAPL"
        assert len(native.runs.calls) == 2 and len(native.threads.values) == 1
        resumed = native.runs.calls[1][2]
        assert list(resumed["command"]["resume"].values()) == [
            {"kind": "input", "answer": {"text": "AAPL"}}
        ]
        assert resumed["context"]["run_id"] == accepted.run_id
    finally:
        server.database.close()
        bff.resources.database.close()
