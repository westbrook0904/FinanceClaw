"""组合现有记忆、归档和摘要准备节点，为技能提供候选状态提交边界。"""

from copy import deepcopy

from langgraph.graph.message import add_messages

from financeclaw.agent_server.context.planning import completed_tool_batches, projected_messages
from financeclaw.kernel.skills import SkillError
from financeclaw.shared.turns.types import ExecutionConflict


class CandidatePreparation:
    """复用已装配中间件和统一预算，不保存任何会话可变状态。"""

    def __init__(
        self,
        stages,
        *,
        planner,
        system_prompt,
        tools,
        output_schema,
        skill_projection,
        validator,
        instruction_reserve="",
    ):
        """固定与正常模型循环相同的阶段和请求区域。"""
        self.stages = tuple(stages)
        self.planner, self.system_prompt = planner, system_prompt
        self.tools, self.output_schema = tools, output_schema
        self.skill_projection, self.validator = skill_projection, validator
        self.instruction_reserve = instruction_reserve

    @staticmethod
    def apply(state, update):
        """遵守原生 messages reducer，其余准备字段按普通 state 更新语义合并。"""
        result = {**state, **(update or {})}
        if update and "messages" in update:
            result["messages"] = add_messages(state.get("messages", []), update["messages"])
        return result

    def prepare(self, state, runtime):
        """所有必要检查成功才返回更新；失败仅携带真实摘要计量字段。"""
        candidate = deepcopy(state)
        updates = {}
        try:
            self.validator(runtime, candidate, candidate["messages"])
            for stage in self.stages:
                update = stage.before_model(candidate, runtime) or {}
                candidate = self.apply(candidate, update)
                updates.update(update)
            if completed_tool_batches(candidate["messages"]) is None:
                raise SkillError("SKILL_CONTEXT_BUDGET_EXCEEDED")
            self.planner.check(
                projected_messages(
                    candidate,
                    system_prompt=self.system_prompt + self.instruction_reserve,
                    skill_projection=self.skill_projection,
                ),
                tools=self.tools,
                output_schema=self.output_schema,
            )
            self.validator(runtime, candidate, candidate["messages"])
        except Exception as exc:
            if isinstance(exc, SkillError):
                error = exc
            elif isinstance(exc, (PermissionError, ExecutionConflict)):
                error = SkillError()
            elif isinstance(exc, ValueError):
                error = SkillError("SKILL_CONTEXT_BUDGET_EXCEEDED")
            else:
                raise
            error.state_update = {
                k: candidate[k]
                for k in (
                    "summary_calls",
                    "context_compaction_fingerprint",
                    "context_compaction_attempts",
                    "context_compaction_error",
                )
                if k in candidate
            }
            raise error from (None if error is exc else exc)
        if "messages" in updates:
            from langchain_core.messages import RemoveMessage
            from langgraph.graph.message import REMOVE_ALL_MESSAGES

            updates["messages"] = [RemoveMessage(id=REMOVE_ALL_MESSAGES), *candidate["messages"]]
        return updates
