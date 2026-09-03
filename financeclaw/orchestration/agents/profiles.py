"""定义版本化 Agent 配置及其只读目录。"""

from collections.abc import Iterable, Iterator, Mapping
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field

from financeclaw.infrastructure.llm import ModelProfileRef


class ToolRef(BaseModel):
    """定义工具Ref。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        model_config: Pydantic 校验策略，禁止未知字段并在需要时冻结实例。
        tool_id: 工具的稳定标识。
        version: 语义版本，用于固定运行行为并支持审计复现。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_id: str
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")


class AgentProfile(BaseModel):
    """定义固定模型、工具和中间件行为的 Agent 版本配置。

    适用场景：
        用于以版本化配置固定运行行为，确保审计与结果可复现。

    属性：
        model_config: Pydantic 校验策略，禁止未知字段并在需要时冻结实例。
        agent_id: Agent 配置的稳定标识。
        version: 语义版本，用于固定运行行为并支持审计复现。
        description: 供调用者、模型或运维人员理解用途的可读说明。
        delegatable: 该 Agent 是否允许作为父运行的委派目标。
        required_scopes: 执行目标必须具备的权限域集合。
        model_profile: Agent 固定使用的模型配置引用。
        system_prompt_template: 定义 Agent 职责、限制和输出要求的系统提示模板。
        allowed_tools: 当前配置明确允许的值集合。
        middleware_profile: 选择 Agent 运行时中间件组合的配置名称。
        context_policy: 选择会话上下文截取与摘要策略的配置名称。
        memory_policy: 选择长期记忆检索与写入策略的配置名称。
        max_model_calls: 限制该资源或操作的最大允许值。
        max_tool_calls: 限制该资源或操作的最大允许值。
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
        """返回由稳定标识与版本组成的目录复合键。"""
        return self.agent_id, self.version


class AgentProfileCatalog(Mapping[tuple[str, str], AgentProfile]):
    """保存不可变 Agent 配置，并支持解析指定版本或最新版本。

    适用场景：
        用于运行时必须按显式版本复现配置，或选择最新兼容版本的场景。

    属性：
        _entries: 按复合键索引的只读目录内容。
    """

    def __init__(self, profiles: Iterable[AgentProfile]) -> None:
        """注入并保存Agent配置Catalog所需的协作对象，同时校验构造期不变量。"""
        entries: dict[tuple[str, str], AgentProfile] = {}
        for profile in profiles:
            if profile.key in entries:
                raise ValueError(f"duplicate agent profile: {profile.agent_id}@{profile.version}")
            entries[profile.key] = profile
        self._entries = MappingProxyType(entries)

    def __getitem__(self, key: tuple[str, str]) -> AgentProfile:
        """按键读取目录项，保持 Mapping 接口语义。"""
        return self._entries[key]

    def __iter__(self) -> Iterator[tuple[str, str]]:
        """按目录内部的稳定顺序迭代所有键。"""
        return iter(self._entries)

    def __len__(self) -> int:
        """返回目录当前登记的条目数量。"""
        return len(self._entries)

    def resolve(self, agent_id: str, version: str | None = None) -> AgentProfile:
        """解析并校验Agent配置Catalog，返回固定版本的运行对象。"""
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
