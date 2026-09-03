"""Agent 档案与档案目录：声明可装配 Agent 的静态规格。

属于 orchestration.agents 的声明层：AgentProfile 描述一个可装配 Agent（如
finance_agent、只读领域 Agent）的允许工具、模型档案、系统提示与限额；
AgentProfileCatalog 以不可变映射管理全部档案并提供版本解析。

"""

from collections.abc import Iterable, Iterator, Mapping
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field

from financeclaw.infrastructure.llm import ModelProfileRef


class ToolRef(BaseModel):
    """档案内引用的工具条目，按工具 id 与语义化版本锁定。

    使用场景：AgentProfile.allowed_tools 的元素类型；AgentFactory 装配时据此
    从工具目录解析受治理的受管工具。

    Attributes:
        tool_id: 工具在工具目录中的唯一标识。
        version: 工具版本，必须为 ``主.次.修订`` 语义化版本格式。

    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_id: str
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")


class AgentProfile(BaseModel):
    """一个可装配 Agent 的完整静态档案声明。

    使用场景：由启动期配置构造并注册进 AgentProfileCatalog；AgentFactory.build
    依据它解析工具、模型、系统提示、调用限额与记忆策略，装配对应 Agent。

    Attributes:
        agent_id: Agent 唯一标识，如 ``finance_agent``。
        version: 档案版本，必须为 ``主.次.修订`` 语义化版本格式。
        description: 档案的人类可读描述，默认空串。
        delegatable: 是否允许被其他 Agent 委托调用，默认 False。
        required_scopes: 调用该 Agent 所需的权限作用域集合，默认为空集。
        model_profile: 引用的模型档案（含模型 id 与版本）。
        system_prompt_template: Agent 的系统提示模板文本。
        allowed_tools: 允许使用的工具引用序列，装配时逐个解析。
        middleware_profile: 中间件组合策略标识，默认 ``governed-v1``。
        context_policy: 上下文组装策略标识，默认 ``stage2-journal-v1``。
        memory_policy: 记忆策略标识；``none`` 表示不挂载记忆召回中间件。
        max_model_calls: 单次运行的模型调用上限，取值 1-64，默认 8。
        max_tool_calls: 单次运行的工具调用上限，取值 1-128，默认 12。

    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str = ""
    delegatable: bool = False
    required_scopes: frozenset[str] = Field(default_factory=frozenset)
    model_profile: ModelProfileRef
    system_prompt_template: str
    allowed_tools: tuple[ToolRef, ...]
    middleware_profile: str = "governed-v1"
    context_policy: str = "stage2-journal-v1"
    memory_policy: str = "none"
    max_model_calls: int = Field(default=8, ge=1, le=64)
    max_tool_calls: int = Field(default=12, ge=1, le=128)

    @property
    def key(self) -> tuple[str, str]:
        """档案的目录键：``(agent_id, version)`` 二元组。"""
        return self.agent_id, self.version


class AgentProfileCatalog(Mapping[tuple[str, str], AgentProfile]):
    """只读的 Agent 档案目录，按键 ``(agent_id, version)`` 存取档案。

    使用场景：启动期收集全部 AgentProfile 构造一次；装配与委托解析时通过
    resolve 按 id 取指定版本或最高版本的档案。

    """

    def __init__(self, profiles: Iterable[AgentProfile]) -> None:
        """收录档案并构建不可变索引。

        Args:
            profiles: 待收录的 Agent 档案序列。

        Raises:
            ValueError: 存在重复的 ``(agent_id, version)`` 档案时抛出。

        """
        entries: dict[tuple[str, str], AgentProfile] = {}
        for profile in profiles:
            if profile.key in entries:
                raise ValueError(f"duplicate agent profile: {profile.agent_id}@{profile.version}")
            entries[profile.key] = profile
        self._entries = MappingProxyType(entries)

    def __getitem__(self, key: tuple[str, str]) -> AgentProfile:
        """按键 ``(agent_id, version)`` 取档案，缺失时抛 KeyError。"""
        return self._entries[key]

    def __iter__(self) -> Iterator[tuple[str, str]]:
        """返回目录键的迭代器。"""
        return iter(self._entries)

    def __len__(self) -> int:
        """返回收录的档案数量。"""
        return len(self._entries)

    def resolve(self, agent_id: str, version: str | None = None) -> AgentProfile:
        """按 id 解析档案：指定版本取精确匹配，未指定取最高语义化版本。

        Args:
            agent_id: Agent 唯一标识。
            version: 目标档案版本；None 表示取该 id 下的最高版本。

        Returns:
            AgentProfile: 解析到的档案。

        Raises:
            LookupError: 指定版本不存在或该 id 下无任何档案时抛出。

        """
        if version is not None:
            try:
                return self._entries[(agent_id, version)]
            except KeyError as exc:
                raise LookupError(f"unknown agent profile: {agent_id}@{version}") from exc
        matches = [
            profile for (candidate, _), profile in self._entries.items() if candidate == agent_id
        ]
        if not matches:
            raise LookupError(f"unknown agent profile: {agent_id}")
        return max(
            matches, key=lambda profile: tuple(int(part) for part in profile.version.split("."))
        )
