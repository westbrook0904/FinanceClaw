"""Versioned immutable AgentProfile configuration."""

from collections.abc import Iterable, Iterator, Mapping
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field

from financeclaw.models import ModelProfileRef


class ToolRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_id: str
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")


class AgentProfile(BaseModel):
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
        return self.agent_id, self.version


class AgentProfileCatalog(Mapping[tuple[str, str], AgentProfile]):
    def __init__(self, profiles: Iterable[AgentProfile]) -> None:
        entries: dict[tuple[str, str], AgentProfile] = {}
        for profile in profiles:
            if profile.key in entries:
                raise ValueError(f"duplicate agent profile: {profile.agent_id}@{profile.version}")
            entries[profile.key] = profile
        self._entries = MappingProxyType(entries)

    def __getitem__(self, key: tuple[str, str]) -> AgentProfile:
        return self._entries[key]

    def __iter__(self) -> Iterator[tuple[str, str]]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def resolve(self, agent_id: str, version: str | None = None) -> AgentProfile:
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
