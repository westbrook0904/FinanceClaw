"""固定发布、完整 AgentFactory 治理及原生澄清恢复的回归。"""

import json
import subprocess
import sys

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command

from financeclaw.agent_server.bootstrap import build_components
from financeclaw.agent_server.tools.policy import TransientToolError
from financeclaw.agent_server.tools.taibu import taibu_tools
from financeclaw.api.application.turns.bootstrap import build_turns
from financeclaw.kernel.context import ExecutionContext
from financeclaw.shared.infrastructure.resources import build_resources
from financeclaw.shared.infrastructure.settings import FinanceClawSettings
from financeclaw.shared.releases.catalog import build_release_catalogs
from financeclaw.shared.turns.snapshots import agent_snapshot
from tests.taibu.conftest import BAZI, StubRemote
from tests.turn_support import seed_execution


class ScriptedModel(FakeMessagesListChatModel):
    """只控制模型输出，所有工具和中间件执行真实代码。"""

    def bind_tools(self, tools, **kwargs):
        """固定测试计划，工具可见性仍由治理中间件决定。"""
        return self


def test_api_import_and_release_are_network_free():
    """冷进程启用太卜时仍不导入执行包，不连接任何 socket。"""
    script = """
import socket, sys
def deny(*args, **kwargs):
    raise AssertionError("release attempted network I/O")
socket.socket.connect = deny
from financeclaw.shared.infrastructure.settings import FinanceClawSettings
from financeclaw.shared.releases.catalog import build_release_catalogs
s = FinanceClawSettings(_env_file=None, taibu_enabled=True, debug_full_io=False,
    langsmith_hide_inputs=True, langsmith_hide_outputs=True)
r = build_release_catalogs(s, enable_persistence=True)
assert ("taibu_bazi", "1.0.0") in r.tool_catalog
assert not any(k.startswith("financeclaw.agent_server") for k in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_disabled_feature_does_not_construct_client_or_require_artifacts(settings, monkeypatch):
    """关闭能力时零网络、零依赖实例，不影响原有运行。"""
    settings.taibu_enabled = False

    def denied(_):
        """客户端构造也属于关闭能力不得发生的动作。"""
        raise AssertionError("disabled Taibu constructed a client")

    monkeypatch.setattr("financeclaw.agent_server.tools.taibu.TaibuMCPClient", denied)
    assert taibu_tools(settings, None) == ()


def test_api_and_worker_publish_same_tools_and_fingerprint(settings):
    """按配置独立装配 API 与 Worker，所有发布声明保持相等。"""
    resources = build_resources(settings, enable_persistence=True)
    try:
        api = build_turns(settings, resources, client=object())
        worker = build_components(resources=resources)
        assert worker.default_agent_profile == api.releases.agents.resolve("finance_agent")
        assert set(worker.tool_catalog) == set(api.releases.tools)
        for key in worker.tool_catalog:
            assert worker.tool_catalog[key].governance == api.releases.tools[key].governance
        for name in ("taibu_almanac", "taibu_bazi"):
            assert (
                "runtime"
                not in worker.tool_catalog.resolve(name).tool.tool_call_schema.model_json_schema()[
                    "properties"
                ]
            )
    finally:
        resources.database.close()


def test_disabling_and_endpoint_changes_have_explicit_release_effects(settings):
    """关闭时不发布工具，开启后的端点/预算变更参与发布指纹。"""
    profile = build_release_catalogs(settings).agent_profiles.resolve("finance_agent")
    off = FinanceClawSettings(_env_file=None, **{**settings.model_dump(), "taibu_enabled": False})
    before = build_release_catalogs(off)
    assert not any(name.startswith("taibu_") for name, _ in before.tool_catalog)
    assert "taibu" not in before.agent_profiles.resolve("finance_agent").deployment_revision
    assert profile.deployment_revision.endswith("taibu-mcp/1")
    changed = FinanceClawSettings(
        _env_file=None, **{**settings.model_dump(), "taibu_timeout_seconds": 12}
    )
    after = build_release_catalogs(changed).agent_profiles.resolve("finance_agent")
    assert after.configuration_fingerprint != profile.configuration_fingerprint
    assert profile.data_classification.value == "confidential"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_factory_executes_governed_tool_and_counts_retries(
    settings, samples, monkeypatch, failure
):
    """真实根图的重试仍逐次计入持久 Turn 预算，服务端没有第二层重试。"""
    remote = StubRemote(samples)
    if failure:
        remote.error = TransientToolError("TAIBU_UNAVAILABLE: synthetic")
    monkeypatch.setattr("financeclaw.agent_server.tools.taibu.TaibuMCPClient", lambda _: remote)
    resources = build_resources(settings, enable_persistence=True)
    try:
        stack = build_components(resources=resources)
        profile = stack.default_agent_profile
        context = ExecutionContext(
            tenant_id="tenant",
            subject_id="subject",
            turn_id="graph-tool",
            scopes={"taibu:read", "artifacts:read"},
            data_classification="confidential",
            request_clock="2026-09-12T10:00:00+08:00",
        )
        context = seed_execution(
            resources.conversation_repository.execution,
            context,
            agent_snapshot(profile, context, thread_id="taibu-graph", input_hash="input"),
        )
        snapshot = resources.conversation_repository.execution.get(context.turn_id)[
            "release_snapshot"
        ]
        model = ScriptedModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[{"name": "taibu_bazi", "args": BAZI, "id": "bazi-graph"}],
                ),
                AIMessage(content="太卜本次未取得结果。" if failure else "已根据排盘结果回答。"),
            ]
        )
        graph = stack.agent_factory.build(
            profile, model=model, checkpointer=InMemorySaver(), store=InMemoryStore()
        )
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content="合成样例八字", id=snapshot["user_message_id"])]},
            {"configurable": {"thread_id": "taibu-graph"}},
            context=context,
        )
        messages = [
            m for m in result["messages"] if isinstance(m, ToolMessage) and m.name == "taibu_bazi"
        ]
        assert len(messages) == 1
        assert messages[0].status == ("error" if failure else "success")
        expected = 3 if failure else 1
        assert len(remote.calls) == expected
        assert (
            resources.conversation_repository.execution.get(context.turn_id)["tool_calls"]
            == expected
        )
    finally:
        resources.database.close()


@pytest.mark.asyncio
async def test_question_resume_stays_on_same_turn_and_only_then_calculates(
    settings, samples, monkeypatch
):
    """原生 interrupt 保存原问题，用户回答后只执行一次八字工具。"""
    remote = StubRemote(samples)
    monkeypatch.setattr("financeclaw.agent_server.tools.taibu.TaibuMCPClient", lambda _: remote)
    resources = build_resources(settings, enable_persistence=True)
    try:
        stack = build_components(resources=resources)
        profile = stack.default_agent_profile
        context = ExecutionContext(
            tenant_id="tenant",
            subject_id="subject",
            turn_id="graph-clarify",
            scopes={"taibu:read", "artifacts:read"},
            data_classification="confidential",
            request_clock="2026-09-12T10:00:00+08:00",
        )
        context = seed_execution(
            resources.conversation_repository.execution,
            context,
            agent_snapshot(profile, context, thread_id="taibu-clarify", input_hash="input"),
        )
        snapshot = resources.conversation_repository.execution.get(context.turn_id)[
            "release_snapshot"
        ]
        responses = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "request_user__clarification",
                        "args": {"question": "请确认出生分钟及中国标准时间口径。"},
                        "id": "birth-question",
                    }
                ],
            ),
            AIMessage(
                content="", tool_calls=[{"name": "taibu_bazi", "args": BAZI, "id": "birth-result"}]
            ),
            AIMessage(content="已按确认的出生信息完成八字。"),
        ]
        graph = stack.agent_factory.build(
            profile,
            model=ScriptedModel(responses=responses),
            checkpointer=InMemorySaver(),
            store=InMemoryStore(),
        )
        config = {"configurable": {"thread_id": "taibu-clarify"}}
        paused = await graph.ainvoke(
            {
                "messages": [
                    HumanMessage(content="请排八字，分钟待确认。", id=snapshot["user_message_id"])
                ]
            },
            config,
            context=context,
        )
        assert paused["__interrupt__"] and not remote.calls
        done = await graph.ainvoke(
            Command(resume={"kind": "input", "answer": {"text": "确认 09:00，中国标准时间。"}}),
            config,
            context=context,
        )
        assert not done.get("__interrupt__") and len(remote.calls) == 1
        result = next(
            m for m in done["messages"] if isinstance(m, ToolMessage) and m.name == "taibu_bazi"
        )
        assert json.loads(result.content)["artifact_ref"]["source_turn_id"] == context.turn_id
    finally:
        resources.database.close()
