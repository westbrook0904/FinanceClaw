"""紫微离线链路模型：只用于编排验证，不伪装为真实命理解读。"""

import json

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from financeclaw.agent_server.agents.offline import OfflineFinanceModel


class OfflineZiweiModel(OfflineFinanceModel):
    """使用真实计算 Tool，解读文字明确标识为离线测试输出。"""

    @property
    def _llm_type(self) -> str:
        """独立于旧金融关键词路由，避免误调用 market_snapshot。"""
        return "financeclaw-stage7-offline"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        """模拟取证与文本解读，只覆盖当前文本结果协议。"""
        if not self._bound_tool_names:
            # finalize 不绑定工具，也不要求 JSON；只返回清晰标识的合成正文。
            message = AIMessage(
                content=(
                    "离线测试取得真实盘面，仅验证文本交付，不是正式命理解读。\n\n"
                    "请在规则批准和真实模型评测后进行传统文化解读。"
                )
            )
        elif isinstance(messages[-1], ToolMessage):
            message = AIMessage(content="盘面证据已取得，交由结构化节点处理。")
        else:
            task = next(json.loads(m.content) for m in reversed(messages) if m.type == "human")
            level = task["arguments"].get("level", "natal")
            name = f"ziwei_{level}_chart"
            if name not in self._bound_tool_names:
                message = AIMessage(content="当前未授权所需盘面工具。")
            else:
                message = AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": name,
                            "args": {},
                            "id": "offline-ziwei-chart",
                            "type": "tool_call",
                        }
                    ],
                )
        return ChatResult(generations=[ChatGeneration(message=message)])
