"""紫微离线链路模型：只用于编排验证，不伪装为真实命理解读。"""

import json

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from .offline import OfflineFinanceModel


class OfflineZiweiModel(OfflineFinanceModel):
    """使用真实计算 Tool，解读文字明确标识为离线测试输出。"""

    @property
    def _llm_type(self) -> str:
        """独立于旧金融关键词路由，避免误调用 market_snapshot。"""
        return "financeclaw-stage7-offline"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        """模拟取证与 JSON finalization，不包含外部请求。"""
        if kwargs.get("response_format"):
            payload = next(
                json.loads(message.content)
                for message in messages
                if message.type == "human" and str(message.content).startswith("{")
            )
            chart = payload["charts"][0]
            content = json.dumps(
                {
                    "answer_summary": "离线测试取得真实盘面，仅验证证据引用，不是正式命理解读。",
                    "interpretations": [
                        {
                            "topic": payload["focus"],
                            "text": "请在规则批准和真实模型评测后进行传统文化解读。",
                            "evidence_refs": [
                                f"{chart['chart_id']}/{chart['facts'][0]['fact_id']}"
                            ],
                        }
                    ],
                },
                ensure_ascii=False,
            )
            message = AIMessage(content=content)
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
