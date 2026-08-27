"""Provider 级重试、Fallback 与执行安全协调。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from harness_contracts import (
    ErrorCode,
    HarnessError,
    IdempotencyType,
    ProviderAttempt,
    ProviderError,
    ResultEnvelope,
    ResultStatus,
    RetryPolicy,
    SelectionContext,
    SelectionDecision,
    SideEffectType,
)
from harness_registry import ProviderRegistration, ResolvedCapability
from harness_selection import ProviderSelector

type ProviderInvocation = Callable[["SelectedProvider"], Awaitable[ResultEnvelope]]
type AttemptStartedCallback = Callable[[ProviderAttempt], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class SelectedProvider:
    """一次 Selection 解析出的稳定 Provider 调用目标。"""

    registration: ProviderRegistration
    decision: SelectionDecision

    @property
    def resolved(self) -> ResolvedCapability:
        return ResolvedCapability(
            descriptor=self.registration.capability,
            plugin_id=self.registration.plugin_id,
            provider=self.registration.provider,
            provider_id=self.registration.provider_id,
        )


class ProviderExecutionCoordinator:
    """在一个已选 Provider 内重试，并只在明确允许时切换 Provider。

    ``RetryPolicy.max_attempts`` 作用于每个 ProviderAttempt。当前 Provider 的
    retry 全部耗尽后，只有最后错误声明 ``fallbackable`` 才会重新 Selection。
    """

    def __init__(
        self,
        selector: ProviderSelector,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(selector, ProviderSelector):
            raise TypeError("selector must implement ProviderSelector")
        self._selector = selector
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def selector(self) -> ProviderSelector:
        return self._selector

    def select(
        self,
        candidates: Sequence[ProviderRegistration],
        context: SelectionContext,
    ) -> SelectedProvider:
        """执行一次 Selection，并拒绝 Selector 返回候选集外 Provider。"""

        candidate_tuple = tuple(candidates)
        decision = self._selector.select(candidate_tuple, context)
        registration = next(
            (
                candidate
                for candidate in candidate_tuple
                if candidate.provider_id == decision.selected_provider_id
            ),
            None,
        )
        if registration is None:
            raise ProviderError(
                "selector returned a provider outside the candidate set",
                code=ErrorCode.PROVIDER_SELECTION_FAILED,
                details={
                    "capability_id": context.capability_id,
                    "selected_provider_id": decision.selected_provider_id,
                    "selector": decision.selector,
                },
            )
        return SelectedProvider(registration=registration, decision=decision)

    async def execute(
        self,
        candidates: Sequence[ProviderRegistration],
        context: SelectionContext,
        invoke_provider: ProviderInvocation,
        *,
        retry_policy: RetryPolicy | None = None,
        idempotency_key: str | None = None,
        deadline_at: datetime | None = None,
        initial_selection: SelectedProvider | None = None,
        retry_start_attempt: int = 1,
        attempt_started: AttemptStartedCallback | None = None,
    ) -> ResultEnvelope:
        """执行完整的 same-provider retry / cross-provider fallback 流程。"""

        if not isinstance(context, SelectionContext):
            raise TypeError("context must be SelectionContext")
        if not callable(invoke_provider):
            raise TypeError("invoke_provider must be callable")
        policy = retry_policy or RetryPolicy()
        if not isinstance(policy, RetryPolicy):
            raise TypeError("retry_policy must be RetryPolicy when provided")
        if (
            not isinstance(retry_start_attempt, int)
            or isinstance(retry_start_attempt, bool)
            or retry_start_attempt < 1
            or retry_start_attempt > policy.max_attempts
        ):
            raise ValueError("retry_start_attempt must be within RetryPolicy.max_attempts")
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str) or not idempotency_key.strip()
        ):
            raise TypeError("idempotency_key must be a non-empty string when provided")
        if attempt_started is not None and not callable(attempt_started):
            raise TypeError("attempt_started must be callable when provided")

        candidate_tuple = tuple(candidates)
        if not candidate_tuple:
            raise ProviderError(
                "no provider candidates supplied for execution",
                code=ErrorCode.PROVIDER_NO_ELIGIBLE_CANDIDATE,
                details={"capability_id": context.capability_id},
            )
        if any(not isinstance(item, ProviderRegistration) for item in candidate_tuple):
            raise TypeError("candidates must contain ProviderRegistration values")

        selected = initial_selection or self.select(candidate_tuple, context)
        candidate_ids = {item.provider_id for item in candidate_tuple}
        if selected.registration.provider_id not in candidate_ids:
            raise ProviderError(
                "initial provider is outside the candidate set",
                code=ErrorCode.PROVIDER_SELECTION_FAILED,
                details={
                    "capability_id": context.capability_id,
                    "provider_id": selected.registration.provider_id,
                },
            )

        attempted_provider_ids: set[str] = set()
        provider_attempt = 1
        first_retry_attempt = retry_start_attempt

        while True:
            attempted_provider_ids.add(selected.registration.provider_id)
            result = await self._execute_selected(
                selected,
                invoke_provider,
                policy=policy,
                idempotency_key=idempotency_key,
                deadline_at=deadline_at,
                provider_attempt=provider_attempt,
                retry_start_attempt=first_retry_attempt,
                attempt_started=attempt_started,
            )
            first_retry_attempt = 1

            if not self._should_fallback(result, deadline_at):
                return result

            remaining = tuple(
                candidate
                for candidate in candidate_tuple
                if candidate.provider_id not in attempted_provider_ids
            )
            if not remaining:
                return result

            try:
                fallback_candidates = self._fallback_candidates(
                    selected.registration,
                    remaining,
                    idempotency_key=idempotency_key,
                )
                selected = self.select(fallback_candidates, context)
            except asyncio.CancelledError:
                raise
            except HarnessError as exc:
                return ResultEnvelope.failure(exc.to_detail())

            provider_attempt += 1

    async def _execute_selected(
        self,
        selected: SelectedProvider,
        invoke_provider: ProviderInvocation,
        *,
        policy: RetryPolicy,
        idempotency_key: str | None,
        deadline_at: datetime | None,
        provider_attempt: int,
        retry_start_attempt: int,
        attempt_started: AttemptStartedCallback | None,
    ) -> ResultEnvelope:
        result = ResultEnvelope.cancelled()
        for retry_attempt in range(retry_start_attempt, policy.max_attempts + 1):
            attempt = ProviderAttempt(
                provider_id=selected.registration.provider_id,
                selection_key=selected.decision.selection_key,
                provider_attempt=provider_attempt,
                retry_attempt=retry_attempt,
                equivalence_group=selected.registration.descriptor.equivalence_group,
                started_at=self._now(),
            )
            if attempt_started is not None:
                await attempt_started(attempt)

            try:
                result = await invoke_provider(selected)
            except asyncio.CancelledError:
                raise
            except HarnessError as exc:
                result = ResultEnvelope.failure(exc.to_detail())
            except Exception as exc:
                error = ProviderError(
                    "provider execution failed",
                    code=ErrorCode.PROVIDER_EXECUTION_FAILED,
                    details={
                        "capability_id": selected.registration.capability.id,
                        "provider_id": selected.registration.provider_id,
                        "cause_type": type(exc).__name__,
                    },
                )
                result = ResultEnvelope.failure(error.to_detail())

            if not isinstance(result, ResultEnvelope):
                error = ProviderError(
                    "provider execution must return ResultEnvelope",
                    code=ErrorCode.PROVIDER_EXECUTION_FAILED,
                    details={
                        "capability_id": selected.registration.capability.id,
                        "provider_id": selected.registration.provider_id,
                    },
                )
                result = ResultEnvelope.failure(error.to_detail())

            if not self._should_retry(
                selected.registration,
                result,
                retry_attempt,
                policy,
                idempotency_key=idempotency_key,
                deadline_at=deadline_at,
            ):
                break

            backoff_seconds = self._backoff_seconds(policy, retry_attempt)
            if not self._deadline_allows(deadline_at, backoff_seconds):
                break
            if backoff_seconds:
                await asyncio.sleep(backoff_seconds)
        return result

    def _should_retry(
        self,
        registration: ProviderRegistration,
        result: ResultEnvelope,
        retry_attempt: int,
        policy: RetryPolicy,
        *,
        idempotency_key: str | None,
        deadline_at: datetime | None,
    ) -> bool:
        if retry_attempt >= policy.max_attempts:
            return False
        if result.status is not ResultStatus.FAILED or result.error is None:
            return False
        if not result.error.retryable or not self._deadline_allows(deadline_at, 0):
            return False
        return self._replay_safe(registration, idempotency_key)

    def _should_fallback(
        self,
        result: ResultEnvelope,
        deadline_at: datetime | None,
    ) -> bool:
        return (
            result.status is ResultStatus.FAILED
            and result.error is not None
            and result.error.fallbackable
            and self._deadline_allows(deadline_at, 0)
        )

    def _fallback_candidates(
        self,
        source: ProviderRegistration,
        remaining: tuple[ProviderRegistration, ...],
        *,
        idempotency_key: str | None,
    ) -> tuple[ProviderRegistration, ...]:
        profile = source.capability.execution_profile
        if profile.side_effect in {SideEffectType.NONE, SideEffectType.READ}:
            return remaining

        if profile.side_effect is not SideEffectType.WRITE:
            raise self._fallback_unsafe(source, remaining, "unsupported_side_effect")
        if not self._replay_safe(source, idempotency_key):
            raise self._fallback_unsafe(source, remaining, "write_not_idempotent")

        source_group = source.descriptor.equivalence_group
        if source_group is None:
            raise self._fallback_unsafe(source, remaining, "source_group_missing")
        equivalent = tuple(
            candidate
            for candidate in remaining
            if candidate.descriptor.equivalence_group is not None
            and candidate.descriptor.equivalence_group == source_group
        )
        if not equivalent:
            raise self._fallback_unsafe(source, remaining, "equivalent_target_missing")
        return equivalent

    @staticmethod
    def _replay_safe(
        registration: ProviderRegistration,
        idempotency_key: str | None,
    ) -> bool:
        profile = registration.capability.execution_profile
        if profile.side_effect in {SideEffectType.NONE, SideEffectType.READ}:
            return True
        return (
            profile.side_effect is SideEffectType.WRITE
            and profile.idempotency in {IdempotencyType.OPTIONAL, IdempotencyType.REQUIRED}
            and idempotency_key is not None
        )

    @staticmethod
    def _fallback_unsafe(
        source: ProviderRegistration,
        remaining: tuple[ProviderRegistration, ...],
        reason: str,
    ) -> ProviderError:
        return ProviderError(
            "cross-provider fallback is unsafe",
            code=ErrorCode.PROVIDER_FALLBACK_UNSAFE,
            details={
                "capability_id": source.capability.id,
                "source_provider_id": source.provider_id,
                "source_equivalence_group": source.descriptor.equivalence_group,
                "candidate_provider_ids": [item.provider_id for item in remaining],
                "reason": reason,
            },
        )

    @staticmethod
    def _backoff_seconds(policy: RetryPolicy, retry_attempt: int) -> float:
        milliseconds = min(
            policy.max_backoff_ms,
            policy.initial_backoff_ms * policy.multiplier ** (retry_attempt - 1),
        )
        return milliseconds / 1000

    def _deadline_allows(self, deadline_at: datetime | None, delay_seconds: float) -> bool:
        return deadline_at is None or (deadline_at - self._now()).total_seconds() > delay_seconds

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value
