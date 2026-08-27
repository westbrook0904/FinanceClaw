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
    JsonValue,
    ProviderAttempt,
    ProviderAttemptStatus,
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
type AttemptCompletedCallback = Callable[
    [ProviderAttempt, ResultEnvelope],
    Awaitable[None],
]
type ProviderEventCallback = Callable[[str, dict[str, JsonValue]], Awaitable[None]]


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


@dataclass(frozen=True, slots=True)
class ProviderResumeState:
    """从 checkpoint 恢复同一 ProviderAttempt 所需的最小 Runtime 状态。"""

    attempt: ProviderAttempt
    result: ResultEnvelope | None = None
    attempted_provider_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, ProviderAttempt):
            raise TypeError("attempt must be ProviderAttempt")
        provider_ids = self.attempted_provider_ids or frozenset({self.attempt.provider_id})
        if any(not isinstance(item, str) or not item.strip() for item in provider_ids):
            raise TypeError("attempted_provider_ids must contain non-empty strings")
        if self.attempt.provider_id not in provider_ids:
            raise ValueError("attempted_provider_ids must include the current provider")
        object.__setattr__(self, "attempted_provider_ids", frozenset(provider_ids))

        if self.attempt.status is ProviderAttemptStatus.RUNNING:
            if self.result is not None or self.attempt.completed_at is not None:
                raise ValueError("running resume attempt cannot have a completed result")
            return
        if self.result is None or self.attempt.completed_at is None:
            raise ValueError("completed resume attempt requires a completed result")
        succeeded = self.result.status in {ResultStatus.SUCCESS, ResultStatus.PARTIAL}
        if succeeded != (self.attempt.status is ProviderAttemptStatus.SUCCEEDED):
            raise ValueError("resume attempt status does not match its result")


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

    def select_for_resume(
        self,
        candidates: Sequence[ProviderRegistration],
        context: SelectionContext,
        resume_state: ProviderResumeState,
    ) -> SelectedProvider:
        """固定恢复 checkpoint 中的 Provider，不重新运行自由 Selection。"""

        if not isinstance(context, SelectionContext):
            raise TypeError("context must be SelectionContext")
        if not isinstance(resume_state, ProviderResumeState):
            raise TypeError("resume_state must be ProviderResumeState")
        candidate_tuple = tuple(candidates)
        registration = next(
            (
                candidate
                for candidate in candidate_tuple
                if candidate.provider_id == resume_state.attempt.provider_id
            ),
            None,
        )
        if registration is None:
            raise ProviderError(
                "checkpointed provider is no longer registered",
                code=ErrorCode.PROVIDER_RESUME_UNSAFE,
                details={
                    "capability_id": context.capability_id,
                    "provider_id": resume_state.attempt.provider_id,
                },
            )
        if registration.descriptor.equivalence_group != resume_state.attempt.equivalence_group:
            raise ProviderError(
                "checkpointed provider equivalence group changed",
                code=ErrorCode.PROVIDER_RESUME_UNSAFE,
                details={
                    "capability_id": context.capability_id,
                    "provider_id": registration.provider_id,
                    "checkpoint_equivalence_group": resume_state.attempt.equivalence_group,
                    "registered_equivalence_group": (registration.descriptor.equivalence_group),
                },
            )
        decision = SelectionDecision(
            capability_id=context.capability_id,
            selected_provider_id=registration.provider_id,
            eligible_candidates=(registration.provider_id,),
            selector="provider-resume",
            reason_code="CHECKPOINTED_PROVIDER",
            selection_key=resume_state.attempt.selection_key,
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
        attempt_completed: AttemptCompletedCallback | None = None,
        resume_state: ProviderResumeState | None = None,
        provider_event: ProviderEventCallback | None = None,
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
        if attempt_completed is not None and not callable(attempt_completed):
            raise TypeError("attempt_completed must be callable when provided")
        if resume_state is not None and not isinstance(resume_state, ProviderResumeState):
            raise TypeError("resume_state must be ProviderResumeState when provided")
        if provider_event is not None and not callable(provider_event):
            raise TypeError("provider_event must be callable when provided")
        if resume_state is not None and resume_state.attempt.retry_attempt > policy.max_attempts:
            raise ValueError("resume retry attempt exceeds RetryPolicy.max_attempts")

        candidate_tuple = tuple(candidates)
        if not candidate_tuple:
            raise ProviderError(
                "no provider candidates supplied for execution",
                code=ErrorCode.PROVIDER_NO_ELIGIBLE_CANDIDATE,
                details={"capability_id": context.capability_id},
            )
        if any(not isinstance(item, ProviderRegistration) for item in candidate_tuple):
            raise TypeError("candidates must contain ProviderRegistration values")

        selected = initial_selection or (
            self.select_for_resume(candidate_tuple, context, resume_state)
            if resume_state is not None
            else self.select(candidate_tuple, context)
        )
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

        attempted_provider_ids = set(
            resume_state.attempted_provider_ids if resume_state is not None else ()
        )
        provider_attempt = resume_state.attempt.provider_attempt if resume_state is not None else 1
        first_retry_attempt = (
            resume_state.attempt.retry_attempt if resume_state is not None else retry_start_attempt
        )
        initial_result = resume_state.result if resume_state is not None else None

        await self._emit_candidates(
            provider_event,
            context,
            candidate_tuple,
            selected.decision,
            phase="resume" if resume_state is not None else "initial",
        )
        await self._emit_selected(
            provider_event,
            context,
            selected,
            provider_attempt=provider_attempt,
            retry_attempt=first_retry_attempt,
            phase="resume" if resume_state is not None else "initial",
        )

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
                attempt_completed=attempt_completed,
                initial_result=initial_result,
                provider_event=provider_event,
            )
            first_retry_attempt = 1
            initial_result = None

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
                source = selected
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
            await self._emit_candidates(
                provider_event,
                context,
                fallback_candidates,
                selected.decision,
                phase="fallback",
            )
            await self._notify(
                provider_event,
                "provider.fallback",
                {
                    "capability_id": context.capability_id,
                    "source_provider_id": source.registration.provider_id,
                    "target_provider_id": selected.registration.provider_id,
                    "provider_attempt": provider_attempt,
                    "retry_attempt": 1,
                    "selection_key": selected.decision.selection_key,
                },
            )
            await self._emit_selected(
                provider_event,
                context,
                selected,
                provider_attempt=provider_attempt,
                retry_attempt=1,
                phase="fallback",
            )

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
        attempt_completed: AttemptCompletedCallback | None,
        initial_result: ResultEnvelope | None,
        provider_event: ProviderEventCallback | None,
    ) -> ResultEnvelope:
        result = initial_result or ResultEnvelope.cancelled()
        next_retry_attempt = retry_start_attempt
        if initial_result is not None:
            if not self._should_retry(
                selected.registration,
                initial_result,
                retry_start_attempt,
                policy,
                idempotency_key=idempotency_key,
                deadline_at=deadline_at,
            ):
                return initial_result
            backoff_seconds = self._backoff_seconds(policy, retry_start_attempt)
            if not self._deadline_allows(deadline_at, backoff_seconds):
                return initial_result
            next_retry_attempt += 1
            await self._emit_retrying(
                provider_event,
                selected,
                provider_attempt,
                retry_start_attempt,
                next_retry_attempt,
            )
            if backoff_seconds:
                await asyncio.sleep(backoff_seconds)

        for retry_attempt in range(next_retry_attempt, policy.max_attempts + 1):
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

            completed_attempt = self._completed_attempt(attempt, result)
            await self._emit_result_events(
                provider_event,
                selected,
                completed_attempt,
                result,
            )
            if attempt_completed is not None:
                await attempt_completed(completed_attempt, result)

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
            await self._emit_retrying(
                provider_event,
                selected,
                provider_attempt,
                retry_attempt,
                retry_attempt + 1,
            )
            if backoff_seconds:
                await asyncio.sleep(backoff_seconds)
        return result

    async def _emit_candidates(
        self,
        callback: ProviderEventCallback | None,
        context: SelectionContext,
        candidates: Sequence[ProviderRegistration],
        decision: SelectionDecision,
        *,
        phase: str,
    ) -> None:
        await self._notify(
            callback,
            "provider.candidates",
            {
                "capability_id": context.capability_id,
                "phase": phase,
                "candidate_provider_ids": [item.provider_id for item in candidates],
                "eligible_provider_ids": list(decision.eligible_candidates),
                "rejected_candidates": [
                    {
                        "provider_id": item.provider_id,
                        "reason_code": item.reason_code,
                    }
                    for item in decision.rejected_candidates
                ],
                "selector": decision.selector,
                "selection_key": decision.selection_key,
            },
        )

    async def _emit_selected(
        self,
        callback: ProviderEventCallback | None,
        context: SelectionContext,
        selected: SelectedProvider,
        *,
        provider_attempt: int,
        retry_attempt: int,
        phase: str,
    ) -> None:
        await self._notify(
            callback,
            "provider.selected",
            {
                "capability_id": context.capability_id,
                "provider_id": selected.registration.provider_id,
                "provider_attempt": provider_attempt,
                "retry_attempt": retry_attempt,
                "phase": phase,
                "selector": selected.decision.selector,
                "selection_reason": selected.decision.reason_code,
                "selection_key": selected.decision.selection_key,
                "equivalence_group": selected.registration.descriptor.equivalence_group,
            },
        )

    async def _emit_retrying(
        self,
        callback: ProviderEventCallback | None,
        selected: SelectedProvider,
        provider_attempt: int,
        retry_attempt: int,
        next_retry_attempt: int,
    ) -> None:
        await self._notify(
            callback,
            "provider.retrying",
            {
                "capability_id": selected.registration.capability.id,
                "provider_id": selected.registration.provider_id,
                "provider_attempt": provider_attempt,
                "retry_attempt": retry_attempt,
                "next_retry_attempt": next_retry_attempt,
                "selection_key": selected.decision.selection_key,
            },
        )

    async def _emit_result_events(
        self,
        callback: ProviderEventCallback | None,
        selected: SelectedProvider,
        attempt: ProviderAttempt,
        result: ResultEnvelope,
    ) -> None:
        if result.status is ResultStatus.FAILED:
            await self._notify(
                callback,
                "provider.failed",
                {
                    "capability_id": selected.registration.capability.id,
                    "provider_id": selected.registration.provider_id,
                    "provider_attempt": attempt.provider_attempt,
                    "retry_attempt": attempt.retry_attempt,
                    "selection_key": attempt.selection_key,
                    "error_code": result.error.code if result.error is not None else None,
                    "retryable": result.error.retryable if result.error is not None else False,
                    "fallbackable": (
                        result.error.fallbackable if result.error is not None else False
                    ),
                },
            )

    @staticmethod
    async def _notify(
        callback: ProviderEventCallback | None,
        name: str,
        attributes: dict[str, JsonValue],
    ) -> None:
        if callback is None:
            return
        try:
            await callback(name, attributes)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Observability 是 best-effort，不能改变 Provider 执行结果。
            return

    def _completed_attempt(
        self,
        attempt: ProviderAttempt,
        result: ResultEnvelope,
    ) -> ProviderAttempt:
        succeeded = result.status in {ResultStatus.SUCCESS, ResultStatus.PARTIAL}
        failure_code = None
        if not succeeded:
            failure_code = (
                result.error.code
                if result.error is not None
                else f"HARNESS.RESULT.{result.status.value.upper()}"
            )
        return attempt.model_copy(
            update={
                "completed_at": self._now(),
                "status": (
                    ProviderAttemptStatus.SUCCEEDED if succeeded else ProviderAttemptStatus.FAILED
                ),
                "failure_code": failure_code,
            }
        )

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
