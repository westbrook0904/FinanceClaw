"""无 LLM 的离线 Agent Server 冒烟实现。

属于 orchestration.agents 的测试辅助模块：提供确定性规则的伪对话模型，在不
调用真实大模型的情况下驱动 ReAct Agent 的工具调用与指令流程，用于冒烟测试
与离线演示。

"""

import json
from collections.abc import Sequence
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from pydantic import PrivateAttr

from .directives import InvocationKind, parse_invocation_directive


class OfflineFinanceModel(BaseChatModel):
    """基于关键词规则路由工具调用的离线伪对话模型。

    使用场景：冒烟测试与离线演示中替代真实 LLM；识别 ``/tool`` 等调用偏好
    指令与少量关键词（记忆偏好、自选股、计算、行情快照等），产出确定性的
    工具调用或应答文本，并遵守 bind_tools 绑定的能力集合。

    Attributes:
        _bound_tool_names: 经 bind_tools 绑定的工具名集合，超出集合的调用
            会被拒绝（Pydantic 私有属性）。

    """

    _bound_tool_names: set[str] = PrivateAttr(default_factory=set)

    @property
    def _llm_type(self) -> str:
        """模型类型标识，用于 LangChain 序列化与诊断输出。"""
        return "financeclaw-stage1-offline"

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[Any, AIMessage]:
        """返回绑定了工具名集合的模型副本，模拟真实模型的 bind_tools 语义。

        Args:
            tools: 待绑定的工具序列（字典或 BaseTool）。
            tool_choice: 忽略，仅为兼容签名保留。
            **kwargs: 忽略，仅为兼容签名保留。

        Returns:
            Runnable[Any, AIMessage]: 记录了绑定工具名的深拷贝模型实例。

        """
        del tool_choice, kwargs
        bound = self.model_copy(deep=True)
        bound._bound_tool_names = {
            tool["name"] if isinstance(tool, dict) else tool.name for tool in tools
        }
        return bound

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        """按确定性规则把消息列表转换为应答或工具调用。

        Args:
            messages: 当前对话消息列表，规则只关注最后一条消息。
            stop: 忽略，仅为兼容签名保留。
            run_manager: 忽略，仅为兼容签名保留。
            **kwargs: 忽略，仅为兼容签名保留。

        Returns:
            ChatResult: 包含确定性 AIMessage 的生成结果。

        """
        del stop, run_manager, kwargs
        last = messages[-1]
        # 1. 处理工具回传消息：记忆提案自动转为 confirm_memory 调用，其余回显。
        if isinstance(last, ToolMessage):
            if last.name == "propose_memory":
                proposal = json.loads(str(last.content))
                draft = proposal["draft"]
                return ChatResult(
                    generations=[
                        ChatGeneration(
                            message=AIMessage(
                                content="",
                                tool_calls=[
                                    {
                                        "name": "confirm_memory",
                                        "args": {"proposal_id": proposal["proposal_id"], **draft},
                                        "id": "offline-confirm-memory-call",
                                        "type": "tool_call",
                                    }
                                ],
                            )
                        )
                    ]
                )
            return ChatResult(
                generations=[
                    ChatGeneration(message=AIMessage(content=f"Tool result: {last.content}"))
                ]
            )
        # 2. 解析用户消息中的调用偏好指令，并计算对应能力名。
        raw_content = str(last.content)
        directive = parse_invocation_directive(raw_content)
        directive_capability = (
            None
            if directive is None
            else (
                directive.resource_id
                if directive.kind is InvocationKind.TOOL
                else f"delegate_{directive.kind.value}__{directive.resource_id}"
            )
        )
        # 3. 指令参数缺失或非法时，要求补充参数，不发起工具调用。
        if directive is not None and (directive.parse_error or not directive.payload):
            problem = directive.parse_error or "required arguments"
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content=f"Please provide the missing or invalid {problem}."
                        )
                    )
                ]
            )
        # 4. 指令能力未绑定（不可见）时，说明无法注册该委托能力。
        if directive is not None and directive_capability not in self._bound_tool_names:
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content=(
                                f"Requested {directive.kind.value} '{directive.resource_id}' "
                                "has no registered delegation capability."
                            )
                        )
                    )
                ]
            )
        # 5. 依据指令参数或关键词规则确定要调用的工具与参数。
        content = raw_content.lower()
        if directive is not None and directive.arguments is not None:
            name = (
                directive.resource_id
                if directive.kind is InvocationKind.TOOL
                else f"delegate_{directive.kind.value}__{directive.resource_id}"
            )
            args = directive.arguments
        elif directive is not None and directive.kind is InvocationKind.AGENT and directive.payload:
            name = f"delegate_agent__{directive.resource_id}"
            args = {"task": directive.payload}
        elif "remember preference" in content or "记住" in content:
            name = "propose_memory"
            args = {
                "kind": "preference",
                "content": "用户偏好低波动资产",
                "evidence_message_ids": ["current"],
            }
        elif "recall memory" in content or "回忆偏好" in content:
            name = "search_memories"
            args = {"query": "低波动", "limit": 5}
        elif "watchlist" in content or "自选" in content or "write" in content:
            name = "watchlist_add"
            args = {"symbol": "AAPL", "note": "stage1"}
        elif "calculate" in content or "计算" in content:
            name = "calculate"
            args = {"operation": "add", "left": 1, "right": 2}
        elif "mcp" in content:
            name = "get_demo_quote"
            args = {"symbol": "AAPL"}
        else:
            name = "market_snapshot"
            args = {"symbol": "AAPL"}
        # 6. 目标工具未绑定时拒绝调用，否则产出确定性工具调用消息。
        if name not in self._bound_tool_names:
            message = AIMessage(content=f"Tool {name} is not authorized for this run.")
        else:
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": name,
                        "args": args,
                        "id": f"offline-{name}-call",
                        "type": "tool_call",
                    }
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=message)])
