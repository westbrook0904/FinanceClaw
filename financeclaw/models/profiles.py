"""Immutable model profile configuration."""

from collections.abc import Iterable, Iterator, Mapping
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field

from financeclaw.contracts import DataClassification


class ModelProfileRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    model: str
    temperature: float = Field(default=0, ge=0, le=2)
    timeout_seconds: float = Field(default=60, gt=0, le=600)
    max_tokens: int = Field(default=4096, ge=64)
    fallback_profiles: tuple[ModelProfileRef, ...] = ()
    allowed_data_classes: frozenset[DataClassification] = Field(
        default_factory=lambda: frozenset(DataClassification)
    )
    allowed_regions: frozenset[str] = Field(default_factory=lambda: frozenset({"global"}))
    supports_tool_calling: bool = True
    supports_structured_output: bool = True

    @property
    def key(self) -> tuple[str, str]:
        return self.profile_id, self.version


class ModelProfileCatalog(Mapping[tuple[str, str], ModelProfile]):
    def __init__(self, profiles: Iterable[ModelProfile]) -> None:
        entries: dict[tuple[str, str], ModelProfile] = {}
        for profile in profiles:
            if profile.key in entries:
                raise ValueError(f"duplicate model profile: {profile.profile_id}@{profile.version}")
            entries[profile.key] = profile
        self._entries = MappingProxyType(entries)

    def __getitem__(self, key: tuple[str, str]) -> ModelProfile:
        return self._entries[key]

    def __iter__(self) -> Iterator[tuple[str, str]]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def resolve(self, ref: ModelProfileRef) -> ModelProfile:
        try:
            return self._entries[(ref.profile_id, ref.version)]
        except KeyError as exc:
            raise LookupError(f"unknown model profile: {ref.profile_id}@{ref.version}") from exc
