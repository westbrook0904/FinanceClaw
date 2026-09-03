"""长期记忆 Tool 的实现：检索、提案、受控确认与遗忘四类记忆操作。

属于 orchestration/tools 治理层的实现模块，包装 LongTermMemoryService
为受治理 Tool；写入路径遵循 propose_memory → 用户确认（HITL）→
confirm_memory 的受控流程，记忆工具仅允许经 Agent 调用。
"""

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
    """记忆类 Tool 入参的公共基类：注入运行时上下文字段。

    使用场景：各记忆 Tool 的入参模型继承本类，由 LangChain 运行时在
    调用时自动填充 runtime 字段，Agent 无需（也不允许）显式提供。

    Attributes:
        runtime: LangChain 运行时句柄，提供执行上下文、tool_call_id
            与 LangGraph Store 等执行期信息。

    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    runtime: ToolRuntime[ExecutionContext]


class SearchMemoriesInput(MemoryToolInput):
    """search_memories Tool 的入参模型：长期记忆检索条件。

    使用场景：LangChain 依据该模型校验与生成 Tool 的参数 schema，
    全部检索条件均可省略以取回最相关的少量记忆。

    Attributes:
        query: 可选的自然语言检索词，最长 512 字符；None 时按相关性
            取默认结果。
        kinds: 可选的记忆类型过滤集合；None 表示不限类型。
        limit: 返回条数上限，取值 1~20，默认 5。

    """

    query: str | None = Field(default=None, max_length=512)
    kinds: tuple[MemoryType, ...] | None = None
    limit: int = Field(default=5, ge=1, le=20)


class ProposeMemoryInput(MemoryToolInput):
    """propose_memory Tool 的入参模型：待校验的记忆草案。

    使用场景：LangChain 依据该模型校验与生成 Tool 的参数 schema；
    提案仅做校验不落库，确认后才能进入 confirm_memory。

    Attributes:
        kind: 记忆的语义类别，见 MemoryType（偏好、目标、约束、决策）。
        content: 记忆正文，1~2000 字符。
        evidence_message_ids: 支撑该记忆的消息标识，1~32 个；传
            ``['current']`` 可绑定当前用户消息。

    """

    kind: MemoryType
    content: str = Field(min_length=1, max_length=2_000)
    evidence_message_ids: tuple[str, ...] = Field(min_length=1, max_length=32)


class ConfirmMemoryInput(ProposeMemoryInput):
    """confirm_memory Tool 的入参模型：确认写入已批准的记忆提案。

    使用场景：LangChain 依据该模型校验与生成 Tool 的参数 schema；
    调用前必须取得用户对相应提案的显式批准。

    Attributes:
        kind: 记忆的语义类别，必须与提案时的草案一致。
        content: 记忆正文，必须与提案时的草案完全一致。
        evidence_message_ids: 证据消息标识，必须与提案解析结果一致。
        proposal_id: propose_memory 返回的提案标识，1~128 字符。
        supersedes_id: 可选的被取代旧记忆标识；用于在确认时以新记忆
            取代某条旧记忆。

    """

    proposal_id: str = Field(min_length=1, max_length=128)
    supersedes_id: str | None = Field(default=None, max_length=128)


class ForgetMemoryInput(MemoryToolInput):
    """forget_memory Tool 的入参模型：指定要遗忘的记忆与方式。

    使用场景：LangChain 依据该模型校验与生成 Tool 的参数 schema，
    仅能操作已认证主体自己的记忆。

    Attributes:
        memory_id: 目标记忆标识，1~128 字符。
        mode: 遗忘方式，``revoke`` 为撤销（保留记录），
            ``delete`` 为逻辑删除，默认 revoke。

    """

    memory_id: str = Field(min_length=1, max_length=128)
    mode: Literal["revoke", "delete"] = "revoke"


class _MemoryTool(BaseTool):
    """记忆类 Tool 的公共基类：注入记忆服务并统一错误转译。

    使用场景：四个记忆 Tool 继承本类获得共享的服务句柄与失败处理；
    服务层抛出的策略与业务错误被转译为携带原因码的 ToolException，
    由 LangChain 作为工具失败结果回传给 Agent。

    Attributes:
        handle_tool_error: 允许 LangChain 捕获 ToolException 并把
            错误消息回传给 Agent，而不是让调用直接崩溃。
        _service: 注入的长期记忆服务实例，承担实际的记忆读写。

    """

    handle_tool_error: bool = True
    _service: LongTermMemoryService = PrivateAttr()

    def __init__(self, service: LongTermMemoryService, **kwargs: Any) -> None:
        """初始化记忆 Tool。

        Args:
            service: 长期记忆服务实例，供各子类执行记忆读写。
            **kwargs: 透传给 ``BaseTool`` 的其余字段。

        """
        super().__init__(**kwargs)
        self._service = service

    @staticmethod
    def _context(runtime: ToolRuntime[ExecutionContext]) -> ExecutionContext:
        """从运行时句柄中取出规范化的执行上下文。

        Args:
            runtime: LangChain 运行时句柄。

        Returns:
            已是 ``ExecutionContext`` 时原样返回，否则从映射校验重建。

        """
        value = runtime.context
        if isinstance(value, ExecutionContext):
            return value
        return ExecutionContext.model_validate(value)

    @staticmethod
    def _draft(
        *, kind: MemoryType, content: str, evidence_message_ids: tuple[str, ...]
    ) -> MemoryDraft:
        """把入参组装为服务层接受的记忆草案。

        Args:
            kind: 记忆的语义类别。
            content: 记忆正文。
            evidence_message_ids: 支撑该记忆的消息标识集合。

        Returns:
            可提交给 LongTermMemoryService 的 MemoryDraft。

        """
        return MemoryDraft(
            kind=kind,
            content=content,
            evidence_message_ids=evidence_message_ids,
        )

    @staticmethod
    def _failure(error: Exception) -> ToolException:
        """把服务层异常转译为带原因码的 Tool 失败结果。

        Args:
            error: 服务层或策略层抛出的原始异常。

        Returns:
            携带 JSON 错误载荷（状态、原因码与消息）的 ToolException。

        """
        # 1. 已知的服务与策略异常提取机器可读原因码，其余统一归因。
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
    """检索已批准长期记忆的 Tool（只读）。

    使用场景：供具备 memory:read 作用域的 Agent 调用，用于回溯用户
    已确认的偏好、目标、约束与决策；价格、持仓、余额等时效性金融
    事实必须改用实时数据工具。输入为检索词、类型过滤与条数上限，
    输出为按键排序的 JSON 记忆列表（含标识、类型、正文与命中理由）。

    Attributes:
        name: Tool 名称，固定为 ``search_memories``。
        description: 展示给 Agent 的工具用途与适用边界说明。
        args_schema: 入参模型，见 SearchMemoriesInput。

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
        """在当前主体的命名空间内检索活跃记忆，返回 JSON 结果列表。

        Args:
            query: 可选的自然语言检索词。
            kinds: 可选的记忆类型过滤集合。
            limit: 返回条数上限。
            runtime: LangChain 运行时句柄，提供上下文与 Store。

        Returns:
            JSON 字符串；``memories`` 数组内每项含 memory_id、
            memory_type、content、schema_version 与命中 reason。

        Raises:
            ToolException: 服务检索失败时转为带原因码的拒绝载荷。

        """
        try:
            # 1. 委托记忆服务在当前主体命名空间内执行检索。
            recalls = self._service.search(
                self._context(runtime),
                runtime.store,
                query=query,
                kinds=kinds,
                limit=limit,
            )
        except Exception as exc:
            # 2. 服务失败统一转译为工具失败结果，避免打断 Agent 循环。
            raise self._failure(exc) from exc
        # 3. 只输出白名单字段，避免把租户、密级等内部信息暴露给模型。
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
    """校验记忆草案的 Tool（不落库），是受控写入流程的第一步。

    使用场景：供具备 memory:write 作用域的 Agent 在沉淀用户稳定偏好、
    目标、约束或已确认决策时调用；提案经服务层策略校验后返回确定性
    提案标识与确认要求，必须待用户批准后才能调用 confirm_memory 落库。
    输入为记忆类型、正文与证据消息标识，输出为提案的 JSON 序列化。

    Attributes:
        name: Tool 名称，固定为 ``propose_memory``。
        description: 展示给 Agent 的工具用途与禁止事项说明。
        args_schema: 入参模型，见 ProposeMemoryInput。

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
        """校验记忆草案并生成待确认提案，返回提案的 JSON 序列化。

        Args:
            kind: 记忆的语义类别。
            content: 记忆正文。
            evidence_message_ids: 支撑该记忆的消息标识集合。
            runtime: LangChain 运行时句柄，提供执行上下文。

        Returns:
            MemoryProposal 的 JSON 序列化，含 proposal_id、敏感级别与
            是否需要确认等字段。

        Raises:
            ToolException: 草案被策略拒绝或服务失败时转为带原因码的
                拒绝载荷。

        """
        try:
            # 1. 组装草案并委托服务层做策略校验与证据归属解析。
            proposal = self._service.propose(
                self._context(runtime),
                self._draft(
                    kind=kind,
                    content=content,
                    evidence_message_ids=evidence_message_ids,
                ),
            )
        except Exception as exc:
            # 2. 策略拒绝或服务失败统一转译为工具失败结果。
            raise self._failure(exc) from exc
        # 3. 提案不落库，直接把完整提案返回给 Agent 与用户确认。
        return proposal.model_dump_json()


class ConfirmMemoryTool(_MemoryTool):
    """把已获用户批准的记忆提案写入长期记忆的 Tool（写类，需审批）。

    使用场景：供具备 memory:write 作用域的 Agent 在用户显式批准某个
    propose_memory 提案后调用；提案标识、正文与证据必须与提案时完全
    一致，写入成功返回记忆标识。治理上标记为 WRITE 且 ALWAYS 审批，
    执行前会再经过一次人工二次授权。

    Attributes:
        name: Tool 名称，固定为 ``confirm_memory``。
        description: 展示给 Agent 的工具用途与一致性要求说明。
        args_schema: 入参模型，见 ConfirmMemoryInput。

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
        """在用户批准的前提下把提案写入长期记忆，返回写入确认。

        Args:
            kind: 记忆的语义类别，必须与提案草案一致。
            content: 记忆正文，必须与提案草案一致。
            evidence_message_ids: 证据消息标识，必须与提案解析一致。
            proposal_id: 待确认的提案标识。
            supersedes_id: 可选的被取代旧记忆标识。
            runtime: LangChain 运行时句柄，提供上下文与 Store。

        Returns:
            按键排序的 JSON 字符串，含 status、memory_id、memory_type
            与 schema_version。

        Raises:
            ToolException: 提案不匹配、缺少确认或服务失败时转为带
                原因码的拒绝载荷。

        """
        try:
            # 1. 委托服务层复验提案一致性并执行受控写入（user_confirmed=True）。
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
            # 2. 复验失败或服务失败统一转译为工具失败结果。
            raise self._failure(exc) from exc
        # 3. 返回最小确认载荷，便于 Agent 向用户汇报写入结果。
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
    """撤销或删除一条本人长期记忆的 Tool（写类，需审批）。

    使用场景：供具备 memory:delete 作用域的 Agent 在用户要求"忘掉某
    事"时调用；仅能操作已认证主体自己的记忆，revoke 保留记录但使其
    失效，delete 做逻辑删除。输入为记忆标识与遗忘方式，输出为 JSON
    状态确认。治理上标记为 WRITE 且 ALWAYS 审批。

    Attributes:
        name: Tool 名称，固定为 ``forget_memory``。
        description: 展示给 Agent 的工具用途说明。
        args_schema: 入参模型，见 ForgetMemoryInput。

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
        """按指定方式遗忘一条本人记忆，返回 JSON 状态确认。

        Args:
            memory_id: 目标记忆标识。
            mode: 遗忘方式，revoke 撤销或 delete 逻辑删除。
            runtime: LangChain 运行时句柄，提供上下文与 Store。

        Returns:
            按键排序的 JSON 字符串，含记忆的生命周期状态与标识。

        Raises:
            ToolException: 记忆不存在、状态迁移非法或服务失败时转为
                带原因码的拒绝载荷。

        """
        try:
            # 1. 委托服务层做所有权校验并执行生命周期迁移。
            record = self._service.forget(
                self._context(runtime), runtime.store, memory_id, mode=mode
            )
        except Exception as exc:
            # 2. 失败统一转译为工具失败结果，避免打断 Agent 循环。
            raise self._failure(exc) from exc
        return json.dumps(
            {"status": record.status.value, "memory_id": record.memory_id},
            sort_keys=True,
        )


def default_memory_tools(service: LongTermMemoryService) -> tuple[ManagedTool, ...]:
    """装配默认的记忆 Tool 集合：检索、提案、确认与遗忘。

    使用场景：编排层为具备记忆能力的 Agent 装配工具集时调用；四个
    Tool 共享同一记忆服务实例，治理上 direct_invocation 均关闭，
    只允许经 Agent 调用。

    Args:
        service: 全部记忆 Tool 共用的长期记忆服务实例。

    Returns:
        四个 ManagedTool 组成的元组，顺序为 search_memories、
        propose_memory、confirm_memory、forget_memory。

    """
    # 1. 四个 Tool 共用的数据密级范围：仅内部与机密两级。
    readable_classes = frozenset({DataClassification.INTERNAL, DataClassification.CONFIDENTIAL})
    # 2. 记忆 Tool 的公共治理默认值：内部出域、机密级、禁重试、全量审计、禁止直连。
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
        # 3. 检索：只读、无需审批、需 memory:read 作用域。
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
        # 4. 提案：只读校验、无需审批、需 memory:write 作用域。
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
        # 5. 确认写入：写类、强制人工审批、需 memory:write 作用域。
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
        # 6. 遗忘：写类、强制人工审批、需 memory:delete 作用域。
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
