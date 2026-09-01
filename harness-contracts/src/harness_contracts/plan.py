"""ExecutionPlan、DAG、数据绑定与调度策略的公共契约。

本文件只描述“计划长什么样”，不负责执行计划。它提供：

* 节点和边组成的有向无环图（DAG）数据结构；
* Request、上游节点输出和字面量之间的显式数据绑定；
* 不依赖 Python ``eval`` 的结构化条件表达式；
* Plan/Node 级预算、重试与失败处理策略。

所有模型都继承 :class:`ContractModel`，因此默认拒绝未知字段、创建后不可修改，
并能安全地在模块之间序列化传递。这里仅执行单模型内可以确定的结构校验；
跨节点引用是否存在、图中是否有环、输出是否可达等全图语义由后续
``PlanValidator`` 负责。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, Field, PlainSerializer, field_validator, model_validator

from .base import (
    ContractModel,
    FrozenJsonMapping,
    FrozenJsonValue,
    NonEmptyString,
)
from .context import _require_timezone
from .exploration import ExplorationProfileSnapshot


class PlanNodeKind(StrEnum):
    """计划节点类型。

    ``CAPABILITY`` 会通过 Registry 解析并调用 Agent/Tool；``APPROVAL`` 是执行引擎
    原生的人工审批等待点，不是 Registry 中注册的 Capability。
    """

    # 普通能力调用节点，必须填写 PlanNode.capability。
    CAPABILITY = "capability"
    # 人工审批节点；到达后 Plan 进入 WAITING，等待外部审批结果再恢复。
    APPROVAL = "approval"
    # Harness-owned 最小探索节点；模型 PlanDraft 不能创建。
    EXPLORATION = "exploration"


class EdgeTrigger(StrEnum):
    """前驱节点满足何种终态时允许评估一条边。

    Trigger 先于 ``PlanEdge.condition`` 判断：只有前驱状态匹配 Trigger，Scheduler
    才继续计算可选 Condition。这样失败分支和正常成功分支可以在同一个 DAG 中
    显式表示。
    """

    # 仅当前驱节点成功结束时触发。
    SUCCESS = "success"
    # 仅当前驱节点以普通执行失败结束时触发，不包含治理拒绝。
    FAILED = "failed"
    # 仅当前驱节点被 Policy 或审批明确拒绝时触发。
    DENIED = "denied"
    # 前驱进入任意已完成终态时触发；取消/跳过是否匹配由 Scheduler
    # 统一定义。
    COMPLETED = "completed"
    # 不区分前驱结果类别；常用于 finally/收尾性质的依赖边。
    ALWAYS = "always"


class FailurePolicy(StrEnum):
    """Plan 与 Node 共用的失败传播策略集合。

    ``FAIL_FAST`` 主要是 Plan 级策略；``FAIL_PLAN`` 和 ``CONTINUE`` 主要是 Node 级
    策略。使用一个枚举可以让计划 JSON 保持简单，具体字段允许哪些取值由
    ``PlanValidator`` 结合所在层级进一步检查。
    """

    # 任一不可恢复失败出现后尽快停止启动新节点，并让整个 Plan 失败。
    FAIL_FAST = "fail_fast"
    # 当前节点最终失败时将失败传播给整个 Plan。
    FAIL_PLAN = "fail_plan"
    # 记录当前节点问题后继续调度仍可执行的分支；最终结果通常可能为
    # PARTIAL。
    CONTINUE = "continue"


class BindingKind(StrEnum):
    """InputBinding 的判别字段，决定绑定值从哪里取得。"""

    # 值直接内嵌在计划中，不依赖运行时数据。
    LITERAL = "literal"
    # 值来自当前计划对应的原始 Request。
    REQUEST = "request"
    # 值来自某个已完成节点保存的 ResultEnvelope。
    NODE_OUTPUT = "node_output"


# 阶段二统一使用 JSON Pointer 风格路径。当前契约要求路径以“/”开头，
# 例如
# ``/input/content`` 或 ``/output/data/items``，避免使用不可控的字符串表达式 DSL。
JsonPointer = Annotated[str, Field(pattern=r"^/")]


class LiteralBinding(ContractModel):
    """把一个 JSON 安全的常量直接绑定到节点输入字段。

    ``value`` 会被深度冻结；其中的 dict/list 在模型内部不能被调用方
    原地修改。
    """

    # Literal 类型既用于 JSON 判别联合，也防止调用方传入与模型不符的 kind。
    kind: Literal[BindingKind.LITERAL] = BindingKind.LITERAL
    # 可为 null、标量、数组或对象，但不能包含 Python 专有对象。
    value: FrozenJsonValue


class RequestBinding(ContractModel):
    """从当前 Request 快照中读取数据并绑定到节点输入。

    例如 ``pointer="/input/content"`` 指向 ``Request.input.content``。绑定解析失败时
    的节点失败/跳过行为属于 ExecutionEngine，而不是本契约模型的职责。
    """

    kind: Literal[BindingKind.REQUEST] = BindingKind.REQUEST
    # 相对于 Request JSON 表示的绝对 JSON Pointer。
    pointer: JsonPointer


class NodeOutputBinding(ContractModel):
    """从指定上游节点的持久化结果中读取数据。

    ``node_id`` 是显式依赖引用，``pointer`` 通常从 ResultEnvelope 根开始，例如
    ``/output/data/amount``。该引用是否存在、是否指向上游节点由
    PlanValidator 校验。
    """

    kind: Literal[BindingKind.NODE_OUTPUT] = BindingKind.NODE_OUTPUT
    # 提供数据的节点稳定 ID。
    node_id: NonEmptyString
    # 相对于该节点 ResultEnvelope JSON 表示的绝对 JSON Pointer。
    pointer: JsonPointer


# ``kind`` 是 Pydantic 判别字段。反序列化 JSON 时会据此选择具体 Binding 模型，
# 因而不会靠“哪些字段碰巧存在”来猜测类型。
InputBinding = Annotated[
    LiteralBinding | RequestBinding | NodeOutputBinding,
    Field(discriminator="kind"),
]
# Plan 的最终输出必须显式取自节点结果，不能直接读取共享可变对象或
# 隐式上下文。当前阶段 OutputBinding 与 NodeOutputBinding 结构相同，
# 但保留独立公共命名，便于
# 后续在不改变 ExecutionPlan.outputs 语义名称的情况下扩展输出约束。
OutputBinding = NodeOutputBinding


def _freeze_bindings(value: dict[str, InputBinding]) -> Any:
    """复制并冻结输入映射，避免创建模型后通过原 dict 修改计划。"""

    return MappingProxyType(dict(value))


# 校验时把普通 dict 转成只读 MappingProxy；序列化时再还原成标准 JSON object。
FrozenBindingMapping = Annotated[
    dict[str, InputBinding],
    AfterValidator(_freeze_bindings),
    PlainSerializer(dict, return_type=dict[str, InputBinding]),
]


class ExplorationNodeSpec(ContractModel):
    """随 Plan 持久化的 standalone Exploration 可信配置快照。"""

    exploration_id: NonEmptyString
    goal_bindings: FrozenBindingMapping
    profile: ExplorationProfileSnapshot

    @model_validator(mode="after")
    def validate_goal(self) -> ExplorationNodeSpec:
        if not self.goal_bindings:
            raise ValueError("exploration node requires goal_bindings")
        return self


def _freeze_outputs(value: dict[str, OutputBinding]) -> Any:
    """复制并冻结 Plan 最终输出映射。"""

    return MappingProxyType(dict(value))


# 与 FrozenBindingMapping 相同，运行时只读，但 model_dump(mode="json")
# 仍输出普通对象。
FrozenOutputMapping = Annotated[
    dict[str, OutputBinding],
    AfterValidator(_freeze_outputs),
    PlainSerializer(dict, return_type=dict[str, OutputBinding]),
]


class ValueReference(ContractModel):
    """ConditionExpr 读取某个节点结果值的结构化引用。"""

    # 被条件读取的节点 ID；引用完整性在全图校验阶段检查。
    node_id: NonEmptyString
    # 相对于节点 ResultEnvelope 的 JSON Pointer。
    pointer: JsonPointer


class ConditionOperator(StrEnum):
    """阶段二允许的条件运算符白名单。"""

    # 二元相等/不相等比较。
    EQ = "eq"
    NE = "ne"
    # 数值或其他有序 JSON 标量的大小比较。
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    # 只检查 ref 指向的路径是否存在，不接收 value。
    EXISTS = "exists"
    # 检查 ref 的值是否属于 value 数组。
    IN = "in"
    # 递归逻辑组合；AND/OR 至少两个子表达式，NOT 恰好一个。
    AND = "and"
    OR = "or"
    NOT = "not"


class ConditionExpr(ContractModel):
    """受限、递归且可序列化的条件表达式。

    比较型表达式使用 ``ref + operator + value``，其中 ``EXISTS`` 不使用 value；
    逻辑型表达式使用 ``operator + operands``。两种形态互斥。执行方必须按
    枚举语义
    解释表达式，禁止把任何字段拼接成 Python 表达式后交给 ``eval``。
    """

    # 决定当前表达式采用“比较”还是“逻辑组合”形态。
    operator: ConditionOperator
    # 比较型表达式的数据来源；逻辑型表达式必须为空。
    ref: ValueReference | None = None
    # 比较目标值；EXISTS 和逻辑型表达式必须为空。
    value: FrozenJsonValue = None
    # 逻辑子表达式；比较型表达式必须为空元组。
    operands: tuple[ConditionExpr, ...] = ()

    @model_validator(mode="after")
    def validate_shape(self) -> ConditionExpr:
        """保证比较表达式和逻辑表达式不会混用两套字段。"""

        if self.operator in {
            ConditionOperator.EQ,
            ConditionOperator.NE,
            ConditionOperator.LT,
            ConditionOperator.LTE,
            ConditionOperator.GT,
            ConditionOperator.GTE,
            ConditionOperator.EXISTS,
            ConditionOperator.IN,
        }:
            if self.ref is None or self.operands:
                raise ValueError("comparison condition requires ref and forbids operands")
            if self.operator is ConditionOperator.EXISTS and self.value is not None:
                raise ValueError("exists condition forbids value")
            if self.operator is ConditionOperator.IN and not isinstance(self.value, tuple | list):
                raise ValueError("in condition requires an array value")
            return self

        if self.ref is not None or self.value is not None:
            raise ValueError("logical condition forbids ref and value")
        expected = 1 if self.operator is ConditionOperator.NOT else 2
        if len(self.operands) < expected:
            raise ValueError(
                f"{self.operator.value} condition requires at least {expected} operand(s)"
            )
        if self.operator is ConditionOperator.NOT and len(self.operands) != 1:
            raise ValueError("not condition requires exactly one operand")
        return self


class RetryPolicy(ContractModel):
    """节点的确定性指数退避重试参数。

    ``max_attempts`` 包含第一次执行，因此默认值 1 表示不重试。第 n 次重试的
    等待
    时间由 ExecutionEngine 根据 initial、multiplier 和 max 上限计算。阶段二不包含
    jitter，以便测试和恢复后的行为可复现。是否允许重试还必须结合
    ErrorDetail、
    CapabilityExecutionProfile、idempotency_key 和剩余 Deadline 判断。
    """

    # 包含首次调用在内的最大总尝试次数，至少为 1。
    max_attempts: int = Field(default=1, ge=1)
    # 第一次重试前的等待毫秒数；允许为 0。
    initial_backoff_ms: int = Field(default=100, ge=0)
    # 单次退避等待的硬上限，防止指数增长失控。
    max_backoff_ms: int = Field(default=10_000, ge=0)
    # 每次重试相对前一次的退避倍率，不允许小于 1。
    multiplier: float = Field(default=2.0, ge=1.0)

    @model_validator(mode="after")
    def validate_backoff_range(self) -> RetryPolicy:
        """确保退避上限不会小于首次退避值。"""

        if self.max_backoff_ms < self.initial_backoff_ms:
            raise ValueError("max_backoff_ms must be greater than or equal to initial_backoff_ms")
        return self


class PlanBudget(ContractModel):
    """作用于整个 ExecutionPlan 的资源和时间预算。

    阶段二实际强制 ``deadline_at`` 和 ``max_concurrency``。token/cost 字段先冻结
    协议，等后续 ModelProvider/Budget Engine 能可靠计量后再执行额度扣减。
    """

    # Plan 的绝对截止时间；必须包含时区，Resume 不会重置该时间。
    deadline_at: datetime | None = None
    # Scheduler 同时处于 RUNNING 的节点数上限，至少为 1。
    max_concurrency: int = Field(default=1, ge=1)
    # 预留的 Plan 总 token 上限；None 表示未指定。
    token_limit: int | None = Field(default=None, gt=0)
    # 预留的 Plan 总成本上限；币种/计量单位由未来预算协议补充。
    cost_limit: float | None = Field(default=None, gt=0)

    _validate_deadline = field_validator("deadline_at")(_require_timezone)


class PlanNode(ContractModel):
    """ExecutionPlan 中一个可调度、可持久化状态的执行单元。

    CAPABILITY 节点描述一次 Agent/Tool 调用；APPROVAL 节点描述一个不占用 asyncio
    Task 的人工等待点；EXPLORATION 节点是 Harness-owned standalone Explore wrapper。
    普通节点只通过 input_mapping 接收数据，Exploration 使用 typed goal_bindings；两者都
    禁止依赖跨节点共享的可变对象。节点执行结果由 NodeExecutionState 单独保存，不回写本模型。
    """

    # Plan 内唯一且跨 checkpoint 稳定的节点 ID。
    node_id: NonEmptyString
    # 节点执行类型；默认为普通 Capability 调用。
    kind: PlanNodeKind = PlanNodeKind.CAPABILITY
    # Registry 使用的 Capability ID；CAPABILITY 必填，其他 kind 禁止填写。
    capability: NonEmptyString | None = None
    # 仅 EXPLORATION 节点携带；完整 ProfileSnapshot 不得放入 metadata。
    exploration: ExplorationNodeSpec | None = None
    # 目标输入字段名到结构化 Binding 的只读映射。
    input_mapping: FrozenBindingMapping = Field(default_factory=dict)
    # 单节点相对超时预算；有效 Deadline 仍不能超过 Request/Plan 的
    # 绝对 Deadline。
    timeout_ms: int | None = Field(default=None, gt=0)
    # 节点失败后的最大尝试次数与退避参数。
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    # 节点最终失败时是否终止 Plan 或允许其他可执行分支继续。
    failure_policy: FailurePolicy = FailurePolicy.FAIL_PLAN
    # 写操作安全重试使用的稳定幂等键；是否必须提供取决于 Capability
    # execution profile。
    idempotency_key: NonEmptyString | None = None
    # 提供给治理策略的业务无关标签，不参与 Capability Registry 解析。
    policy_tags: frozenset[str] = Field(default_factory=frozenset)
    # 扩展元数据；不得用于隐藏输入、Secret 或绕过正式调度字段。
    metadata: FrozenJsonMapping = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_kind_fields(self) -> PlanNode:
        """校验三种 node kind 的 typed 字段互斥关系。"""

        if self.kind is PlanNodeKind.CAPABILITY:
            if self.capability is None:
                raise ValueError("capability node requires capability")
            if self.exploration is not None:
                raise ValueError("capability node forbids exploration spec")
        if self.kind is PlanNodeKind.APPROVAL:
            if self.capability is not None:
                raise ValueError("approval node forbids capability")
            if self.input_mapping:
                raise ValueError("approval node forbids input_mapping")
            if self.idempotency_key is not None:
                raise ValueError("approval node forbids idempotency_key")
            if self.exploration is not None:
                raise ValueError("approval node forbids exploration spec")
        if self.kind is PlanNodeKind.EXPLORATION:
            if self.exploration is None:
                raise ValueError("exploration node requires exploration spec")
            if self.capability is not None:
                raise ValueError("exploration node forbids capability")
            if self.input_mapping:
                raise ValueError("exploration node uses goal_bindings, not input_mapping")
            if self.idempotency_key is not None:
                raise ValueError("exploration node forbids idempotency_key")
            if self.retry_policy.max_attempts != 1:
                raise ValueError("exploration node requires scheduler max_attempts=1")
            if self.metadata:
                raise ValueError("exploration node forbids free metadata")
        return self


class PlanEdge(ContractModel):
    """连接两个节点的有向依赖边。

    Scheduler 先判断 ``trigger``，再判断可选 ``condition``；两者都满足时目标节点
    才获得一条已满足的入边。端点是否存在、多个入边采用何种 Join 语义以及
    整图
    是否有环，属于 PlanValidator/Scheduler 的全局职责。
    """

    # 前驱节点 ID。
    from_node: NonEmptyString
    # 后继节点 ID。
    to_node: NonEmptyString
    # 前驱需要以何种结果结束，默认只沿成功分支传播。
    trigger: EdgeTrigger = EdgeTrigger.SUCCESS
    # 对节点输出做的额外结构化判断；None 表示仅判断 trigger。
    condition: ConditionExpr | None = None

    @model_validator(mode="after")
    def reject_self_edge(self) -> PlanEdge:
        """尽早拒绝必然构成一节点环的自连接。"""

        if self.from_node == self.to_node:
            raise ValueError("plan edge cannot reference the same source and destination")
        return self


class ExecutionPlan(ContractModel):
    """一次逻辑计划的不可变 DAG 定义。

    ``plan_id`` 在 checkpoint、进程重启和 resume 期间保持不变；``revision`` 标识
    计划定义版本。阶段二不支持运行时 Patch，但提前保留 revision，避免
    持久化格式
    将来无法演进。计划本身不保存运行状态，状态由 PlanExecutionState 管理。
    """

    # 逻辑计划的稳定主键，用于 StateStore、Trace、Continuation 和 Resume 关联。
    plan_id: NonEmptyString
    # 计划定义版本，从 1 开始；同一持久化状态必须匹配对应 revision。
    revision: int = Field(default=1, ge=1)
    # 全计划共享的 Deadline、并发度及预留资源额度。
    budget: PlanBudget = Field(default_factory=PlanBudget)
    # Plan 级默认失败传播方式。
    failure_policy: FailurePolicy = FailurePolicy.FAIL_FAST
    # DAG 节点定义；tuple 确保创建后不能增删或重排。
    nodes: tuple[PlanNode, ...]
    # DAG 有向边；空元组允许单节点或彼此独立的并行节点。
    edges: tuple[PlanEdge, ...] = ()
    # 最终 ResultOutput 字段到节点输出的只读映射。
    outputs: FrozenOutputMapping = Field(default_factory=dict)
    # 业务无关扩展信息，不应承载执行状态、Secret 或可变共享数据。
    metadata: FrozenJsonMapping = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_unique_node_ids(self) -> ExecutionPlan:
        """保证节点 ID 唯一，为 Edge/Binding 引用提供稳定命名空间。"""

        node_ids = [node.node_id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("plan node_id values must be unique")
        return self
