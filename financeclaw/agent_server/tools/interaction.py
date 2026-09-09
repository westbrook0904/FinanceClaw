"""声明式用户交互工具与节点辅助函数；原生 interrupt 保存并恢复当前检查点。"""

import json
from typing import Any

from jsonschema import Draft202012Validator
from langchain_core.tools import BaseTool
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, Field

from financeclaw.agent_server.tools.governance import ManagedTool
from financeclaw.kernel.interactions import InteractionPoint
from financeclaw.kernel.tools import (
    ApprovalMode,
    Egress,
    Idempotency,
    RetryProfile,
    RiskLevel,
    Sensitivity,
    SideEffect,
    ToolGovernance,
)
from financeclaw.shared.execution_ledger.repository import digest


def request_user_interaction(
    point: InteractionPoint, *, question: str | None = None, action: dict[str, Any] | None = None
) -> dict[str, Any]:
    """只恢复该节点实例，回答再次校验；此函数本身不执行用户批准的副作用。

    已有进度放在前序节点，interrupt 所在节点重入前的代码必须可重放。
    修改 action 应重新预检并产生新的交互实例，不能把旧批准用于新参数。
    """
    payload = {
        "kind": "user_interaction",
        "schema_version": 1,
        "point_id": point.point_id,
        "interaction_kind": point.kind,
        "question": question or point.question,
    }
    if not isinstance(payload["question"], str) or not 1 <= len(payload["question"]) <= 2000:
        raise ValueError("interaction question must be bounded")
    if point.kind == "approval":
        if not action or len(json.dumps(action).encode()) > 16384:
            raise ValueError("approval requires a bounded action snapshot")
        payload["action"] = action
    from financeclaw.agent_server.tools.subgraph_scope import active_scope

    scope = active_scope.get()
    if scope is not None:
        payload.update(scope.interaction_binding())
    result = interrupt(payload)
    if not isinstance(result, dict) or result.get("kind") != point.kind:
        raise ValueError("response does not match the declared interaction kind")
    if scope is not None and result.get("invocation_id") != scope.identity:
        raise ValueError("response does not match this Worker invocation")
    if point.kind == "approval":
        if result.get("decision") not in {"approve", "reject"} or result.get(
            "action_hash"
        ) != digest(action):
            raise ValueError("approval response does not match this concrete action")
    elif point.kind == "choice":
        if result.get("answer") not in point.options:
            raise ValueError("response is not a published choice")
    else:
        Draft202012Validator(point.response_schema).validate(result.get("answer"))
    return result


class UserQuestionInput(BaseModel):
    """模型只能提供有界的问题正文，不能指定回答 Schema、权限或审批决定。"""

    # 回答结构取自装配工具时固定的 InteractionPoint；模型生成的问题不授予权限。

    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=2000)


class UserQuestionTool(BaseTool):
    """资料／选项提问独占工具批次，恢复后继续当前 Agent 的工具循环。"""

    # interrupt 暂停当前 owner，用户回答经应用服务验证后恢复同一工具；
    # 问题通过原生 interrupt 传播到根，由用户回答后恢复当前子图。

    point: InteractionPoint
    args_schema: type[BaseModel] = UserQuestionInput

    def _run(self, question: str) -> str:
        """同步节点原位挂起并将已校验的用户回答返回模型。"""
        return json.dumps(
            request_user_interaction(self.point, question=question), ensure_ascii=False
        )

    async def _arun(self, question: str) -> str:
        """Interrupt 不做阻塞 I/O，异步节点共用原生恢复语义。"""
        return self._run(question)


def question_tools(points: tuple[InteractionPoint, ...]) -> tuple[ManagedTool, ...]:
    """仅为 input/choice 生成模型工具；动作审批走受治理 HITL 或显式发布的节点代码。"""
    result = []
    for point in points:
        if point.kind == "approval":
            continue
        name = "request_user__" + point.point_id
        contract = point.response_schema if point.kind == "input" else list(point.options)
        result.append(
            ManagedTool(
                tool=UserQuestionTool(
                    name=name,
                    point=point,
                    description=(
                        f"Ask the user at {point.point_id}: {point.question}. "
                        f"Answer contract: {json.dumps(contract, ensure_ascii=False)}"
                    ),
                ),
                governance=ToolGovernance(
                    tool_id=name,
                    version="1.0.0",
                    side_effect=SideEffect.INTERACTION,
                    idempotency=Idempotency.KEY_REQUIRED,
                    risk_level=RiskLevel.LOW,
                    required_scopes=frozenset({point.required_scope})
                    if point.required_scope
                    else frozenset(),
                    approval=ApprovalMode.NONE,
                    egress=Egress.INTERNAL,
                    sensitivity=Sensitivity.INTERNAL,
                    retry_profile=RetryProfile.NONE,
                ),
            )
        )
    return tuple(result)
