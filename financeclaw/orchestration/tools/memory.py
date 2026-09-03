"""把长期记忆生命周期操作暴露为受治理工具。"""

from __future__ import annotations

import json
from typing import Any, Literal

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, ToolException
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from financeclaw.kernel import DataClassification, ExecutionContext
from financeclaw.modules.memory.models import MemoryDraft, MemoryType
from financeclaw.modules.memory.policy import MemoryPolicyViolation
from financeclaw.modules.memory.service import LongTermMemoryService, MemoryServiceError
from financeclaw.orchestration.tools.governance import (
    ApprovalMode,
    AuditLevel,
    Egress,
    Idempotency,
    ManagedTool,
    RetryProfile,
    RiskLevel,
    Sensitivity,
    SideEffect,
    ToolGovernance,
)


class MemoryToolInput(BaseModel):
    """定义记忆工具的校验输入。

    适用场景：
        用于在数据进入领域或图运行前完成结构校验和类型收敛的场景。

    属性：
        model_config: Pydantic 校验策略，禁止未知字段并在需要时冻结实例。
        runtime: LangChain 注入的可信工具运行上下文，不由模型生成。
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    runtime: ToolRuntime[ExecutionContext]


class SearchMemoriesInput(MemoryToolInput):
    """定义SearchMemories的校验输入。

    适用场景：
        用于在数据进入领域或图运行前完成结构校验和类型收敛的场景。

    属性：
        query: 用于检索或匹配记录的自然语言查询；为空时不做文本过滤。
        kinds: 允许返回或操作的记忆类别集合。
        limit: 单次操作最多返回的记录数量。
    """

    query: str | None = Field(default=None, max_length=512)
    kinds: tuple[MemoryType, ...] | None = None
    limit: int = Field(default=5, ge=1, le=20)


class ProposeMemoryInput(MemoryToolInput):
    """定义Propose记忆的校验输入。

    适用场景：
        用于在数据进入领域或图运行前完成结构校验和类型收敛的场景。

    属性：
        kind: 记录或目标的语义类别。
        content: 经过边界校验后保存或传递的正文内容。
        evidence_message_ids: 关联对象标识的有序集合。
    """

    kind: MemoryType
    content: str = Field(min_length=1, max_length=2_000)
    evidence_message_ids: tuple[str, ...] = Field(min_length=1, max_length=32)


class ConfirmMemoryInput(ProposeMemoryInput):
    """定义Confirm记忆的校验输入。

    适用场景：
        用于在数据进入领域或图运行前完成结构校验和类型收敛的场景。

    属性：
        proposal_id: 关联对象的稳定标识，用于查询、关联和审计追踪。
        supersedes_id: 关联对象的稳定标识，用于查询、关联和审计追踪。
    """

    proposal_id: str = Field(min_length=1, max_length=128)
    supersedes_id: str | None = Field(default=None, max_length=128)


class ForgetMemoryInput(MemoryToolInput):
    """定义Forget记忆的校验输入。

    适用场景：
        用于在数据进入领域或图运行前完成结构校验和类型收敛的场景。

    属性：
        memory_id: 长期记忆稳定标识。
        mode: 操作模式；决定撤销记录还是永久删除其内容。
    """

    memory_id: str = Field(min_length=1, max_length=128)
    mode: Literal["revoke", "delete"] = "revoke"


class _MemoryTool(BaseTool):
    """定义记忆工具。

    适用场景：
        用于把该能力纳入 LangChain/LangGraph 工具调用与统一治理链的场景。

    属性：
        handle_tool_error: 是否由 LangChain 将工具异常转换为模型可见的错误消息。
        _service: 执行该适配层所依赖的领域或应用服务。
    """

    handle_tool_error: bool = True
    _service: LongTermMemoryService = PrivateAttr()

    def __init__(self, service: LongTermMemoryService, **kwargs: Any) -> None:
        """注入并保存记忆工具所需的协作对象，同时校验构造期不变量。"""
        super().__init__(**kwargs)
        self._service = service

    @staticmethod
    def _context(runtime: ToolRuntime[ExecutionContext]) -> ExecutionContext:
        """从 LangChain 请求或工具运行时提取并校验可信执行上下文。"""
        value = runtime.context
        if isinstance(value, ExecutionContext):
            return value
        return ExecutionContext.model_validate(value)

    @staticmethod
    def _draft(
        *, kind: MemoryType, content: str, evidence_message_ids: tuple[str, ...]
    ) -> MemoryDraft:
        """把工具参数与可信运行上下文组合为长期记忆候选。"""
        return MemoryDraft(
            kind=kind,
            content=content,
            evidence_message_ids=evidence_message_ids,
        )

    @staticmethod
    def _failure(error: Exception) -> ToolException:
        """把已知记忆领域异常转换为模型可理解的稳定工具错误。"""
        if isinstance(error, MemoryServiceError | MemoryPolicyViolation):
            reason = error.reason
        else:
            reason = "memory_operation_failed"
        return ToolException(
            json.dumps(
                {"status": "rejected", "reason": reason, "message": str(error)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )


class SearchMemoriesTool(_MemoryTool):
    """定义SearchMemories工具。

    适用场景：
        用于把该能力纳入 LangChain/LangGraph 工具调用与统一治理链的场景。

    属性：
        name: 在外部接口或工具注册表中暴露的稳定名称。
        description: 供调用者、模型或运维人员理解用途的可读说明。
        args_schema: 工具入参使用的 Pydantic 校验模型类型。
    """

    name: str = "search_memories"
    description: str = (
        "Search active, user-approved long-term memories for the authenticated user. "
        "Use current-data tools instead for prices, holdings, balances or other financial facts."
    )
    args_schema: type[BaseModel] = SearchMemoriesInput

    def _run(
        self,
        query: str | None = None,
        kinds: tuple[MemoryType, ...] | None = None,
        limit: int = 5,
        *,
        runtime: ToolRuntime[ExecutionContext],
    ) -> str:
        """执行工具的同步实现，并返回可序列化结果。"""
        try:
            recalls = self._service.search(
                self._context(runtime),
                runtime.store,
                query=query,
                kinds=kinds,
                limit=limit,
            )
        except Exception as exc:
            raise self._failure(exc) from exc
        return json.dumps(
            {
                "memories": [
                    {
                        "memory_id": item.record.memory_id,
                        "memory_type": item.record.memory_type.value,
                        "content": item.record.content,
                        "schema_version": item.record.schema_version,
                        "reason": item.reason,
                    }
                    for item in recalls
                ]
            },
            ensure_ascii=False,
            sort_keys=True,
        )


class ProposeMemoryTool(_MemoryTool):
    """定义Propose记忆工具。

    适用场景：
        用于把该能力纳入 LangChain/LangGraph 工具调用与统一治理链的场景。

    属性：
        name: 在外部接口或工具注册表中暴露的稳定名称。
        description: 供调用者、模型或运维人员理解用途的可读说明。
        args_schema: 工具入参使用的 Pydantic 校验模型类型。
    """

    name: str = "propose_memory"
    description: str = (
        "Validate a possible stable preference, goal, constraint or confirmed decision. "
        "This does not persist memory. Pass evidence_message_ids=['current'] to bind the current "
        "user Journal message; never propose prices, holdings, balances, credentials or news."
    )
    args_schema: type[BaseModel] = ProposeMemoryInput

    def _run(
        self,
        kind: MemoryType,
        content: str,
        evidence_message_ids: tuple[str, ...],
        *,
        runtime: ToolRuntime[ExecutionContext],
    ) -> str:
        """执行工具的同步实现，并返回可序列化结果。"""
        try:
            proposal = self._service.propose(
                self._context(runtime),
                self._draft(
                    kind=kind,
                    content=content,
                    evidence_message_ids=evidence_message_ids,
                ),
            )
        except Exception as exc:
            raise self._failure(exc) from exc
        return proposal.model_dump_json()


class ConfirmMemoryTool(_MemoryTool):
    """定义Confirm记忆工具。

    适用场景：
        用于把该能力纳入 LangChain/LangGraph 工具调用与统一治理链的场景。

    属性：
        name: 在外部接口或工具注册表中暴露的稳定名称。
        description: 供调用者、模型或运维人员理解用途的可读说明。
        args_schema: 工具入参使用的 Pydantic 校验模型类型。
    """

    name: str = "confirm_memory"
    description: str = (
        "Persist the exact output of propose_memory after explicit user approval. "
        "The proposal ID, content and resolved evidence must remain unchanged."
    )
    args_schema: type[BaseModel] = ConfirmMemoryInput

    def _run(
        self,
        kind: MemoryType,
        content: str,
        evidence_message_ids: tuple[str, ...],
        proposal_id: str,
        supersedes_id: str | None = None,
        *,
        runtime: ToolRuntime[ExecutionContext],
    ) -> str:
        """执行工具的同步实现，并返回可序列化结果。"""
        try:
            record = self._service.confirm(
                self._context(runtime),
                runtime.store,
                proposal_id=proposal_id,
                draft=self._draft(
                    kind=kind,
                    content=content,
                    evidence_message_ids=evidence_message_ids,
                ),
                user_confirmed=True,
                supersedes_id=supersedes_id,
            )
        except Exception as exc:
            raise self._failure(exc) from exc
        return json.dumps(
            {
                "status": "committed",
                "memory_id": record.memory_id,
                "memory_type": record.memory_type.value,
                "schema_version": record.schema_version,
            },
            sort_keys=True,
        )


class ForgetMemoryTool(_MemoryTool):
    """定义Forget记忆工具。

    适用场景：
        用于把该能力纳入 LangChain/LangGraph 工具调用与统一治理链的场景。

    属性：
        name: 在外部接口或工具注册表中暴露的稳定名称。
        description: 供调用者、模型或运维人员理解用途的可读说明。
        args_schema: 工具入参使用的 Pydantic 校验模型类型。
    """

    name: str = "forget_memory"
    description: str = (
        "Revoke or logically delete one long-term memory owned by the authenticated user."
    )
    args_schema: type[BaseModel] = ForgetMemoryInput

    def _run(
        self,
        memory_id: str,
        mode: Literal["revoke", "delete"] = "revoke",
        *,
        runtime: ToolRuntime[ExecutionContext],
    ) -> str:
        """执行工具的同步实现，并返回可序列化结果。"""
        try:
            record = self._service.forget(
                self._context(runtime), runtime.store, memory_id, mode=mode
            )
        except Exception as exc:
            raise self._failure(exc) from exc
        return json.dumps(
            {"status": record.status.value, "memory_id": record.memory_id},
            sort_keys=True,
        )


def default_memory_tools(service: LongTermMemoryService) -> tuple[ManagedTool, ...]:
    """构造检索、提议、确认和遗忘四个受治理长期记忆工具。"""
    readable_classes = frozenset({DataClassification.INTERNAL, DataClassification.CONFIDENTIAL})
    internal = dict(
        version="1.0.0",
        direct_invocation=False,
        egress=Egress.INTERNAL,
        sensitivity=Sensitivity.CONFIDENTIAL,
        retry_profile=RetryProfile.NONE,
        audit_level=AuditLevel.FULL,
        allowed_data_classes=readable_classes,
    )
    return (
        ManagedTool(
            SearchMemoriesTool(service),
            ToolGovernance(
                tool_id="search_memories",
                side_effect=SideEffect.READ,
                idempotency=Idempotency.IDEMPOTENT,
                risk_level=RiskLevel.LOW,
                required_scopes=frozenset({"memory:read"}),
                approval=ApprovalMode.NONE,
                **internal,
            ),
        ),
        ManagedTool(
            ProposeMemoryTool(service),
            ToolGovernance(
                tool_id="propose_memory",
                side_effect=SideEffect.READ,
                idempotency=Idempotency.IDEMPOTENT,
                risk_level=RiskLevel.LOW,
                required_scopes=frozenset({"memory:write"}),
                approval=ApprovalMode.NONE,
                **internal,
            ),
        ),
        ManagedTool(
            ConfirmMemoryTool(service),
            ToolGovernance(
                tool_id="confirm_memory",
                side_effect=SideEffect.WRITE,
                idempotency=Idempotency.KEY_REQUIRED,
                risk_level=RiskLevel.MEDIUM,
                required_scopes=frozenset({"memory:write"}),
                approval=ApprovalMode.ALWAYS,
                **internal,
            ),
        ),
        ManagedTool(
            ForgetMemoryTool(service),
            ToolGovernance(
                tool_id="forget_memory",
                side_effect=SideEffect.WRITE,
                idempotency=Idempotency.IDEMPOTENT,
                risk_level=RiskLevel.MEDIUM,
                required_scopes=frozenset({"memory:delete"}),
                approval=ApprovalMode.ALWAYS,
                **internal,
            ),
        ),
    )
