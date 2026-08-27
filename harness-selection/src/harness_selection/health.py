"""Provider HealthSource 与最小内存实现。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import UTC, datetime
from threading import RLock

from harness_contracts import (
    ErrorCode,
    ProviderError,
    ProviderHealthSnapshot,
    ProviderHealthStatus,
)


class HealthSource(ABC):
    """Selection 获取 Provider 健康快照的只读接口。"""

    @abstractmethod
    def snapshot(self, provider_id: str) -> ProviderHealthSnapshot:
        """返回 Provider 当前健康快照；无观测数据时应返回 UNKNOWN。"""


class StaticHealthSource(HealthSource):
    """由静态配置提供健康状态，未配置 Provider 默认为 UNKNOWN。"""

    def __init__(
        self,
        statuses: Mapping[str, ProviderHealthStatus] | None = None,
        *,
        default_status: ProviderHealthStatus = ProviderHealthStatus.UNKNOWN,
        observed_at: datetime | None = None,
        source: str = "static",
    ) -> None:
        if not isinstance(default_status, ProviderHealthStatus):
            raise TypeError("default_status must be ProviderHealthStatus")
        if not isinstance(source, str) or not source.strip():
            raise TypeError("source must be a non-empty string")
        effective_observed_at = observed_at or datetime.now(UTC)
        if (
            effective_observed_at.tzinfo is None
            or effective_observed_at.utcoffset() is None
        ):
            raise TypeError("observed_at must be timezone-aware")

        normalized: dict[str, ProviderHealthStatus] = {}
        for provider_id, status in (statuses or {}).items():
            _validate_provider_id(provider_id)
            if not isinstance(status, ProviderHealthStatus):
                raise TypeError("health status must be ProviderHealthStatus")
            normalized[provider_id.strip()] = status

        self._statuses = normalized
        self._default_status = default_status
        self._observed_at = effective_observed_at
        self._source = source.strip()

    def snapshot(self, provider_id: str) -> ProviderHealthSnapshot:
        normalized_id = _validate_provider_id(provider_id)
        status = self._statuses.get(normalized_id, self._default_status)
        return ProviderHealthSnapshot(
            provider_id=normalized_id,
            status=status,
            observed_at=self._observed_at,
            source=self._source,
            reason_code=(
                "STATIC_HEALTH_CONFIGURED"
                if normalized_id in self._statuses
                else (
                    "HEALTH_UNKNOWN"
                    if self._default_status is ProviderHealthStatus.UNKNOWN
                    else "STATIC_HEALTH_DEFAULT"
                )
            ),
        )


class TestHealthSource(HealthSource):
    """测试和故障注入使用的可变、线程安全 HealthSource。"""

    __test__ = False

    def __init__(
        self,
        initial: Mapping[str, ProviderHealthStatus] | None = None,
    ) -> None:
        self._snapshots: dict[str, ProviderHealthSnapshot] = {}
        self._lock = RLock()
        for provider_id, status in (initial or {}).items():
            self.set_status(provider_id, status)

    def set_status(
        self,
        provider_id: str,
        status: ProviderHealthStatus,
        *,
        reason_code: str | None = None,
        observed_at: datetime | None = None,
    ) -> None:
        normalized_id = _validate_provider_id(provider_id)
        if not isinstance(status, ProviderHealthStatus):
            raise TypeError("status must be ProviderHealthStatus")
        effective_observed_at = observed_at or datetime.now(UTC)
        if (
            effective_observed_at.tzinfo is None
            or effective_observed_at.utcoffset() is None
        ):
            raise TypeError("observed_at must be timezone-aware")
        if reason_code is not None and (
            not isinstance(reason_code, str) or not reason_code.strip()
        ):
            raise TypeError("reason_code must be a non-empty string when provided")

        snapshot = ProviderHealthSnapshot(
            provider_id=normalized_id,
            status=status,
            observed_at=effective_observed_at,
            source="test",
            reason_code=reason_code or f"TEST_{status.value.upper()}",
        )
        with self._lock:
            self._snapshots[normalized_id] = snapshot

    def snapshot(self, provider_id: str) -> ProviderHealthSnapshot:
        normalized_id = _validate_provider_id(provider_id)
        with self._lock:
            snapshot = self._snapshots.get(normalized_id)
        if snapshot is not None:
            return snapshot
        return ProviderHealthSnapshot(
            provider_id=normalized_id,
            status=ProviderHealthStatus.UNKNOWN,
            observed_at=datetime.now(UTC),
            source="test",
            reason_code="HEALTH_UNKNOWN",
        )


def _validate_provider_id(provider_id: str) -> str:
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise TypeError("provider_id must be a non-empty string")
    return provider_id.strip()
