"""Gateway-owned model attempt 用量遥测聚合。"""

from __future__ import annotations

from harness_contracts import (
    ErrorCode,
    ModelAttemptAccounting,
    ModelGenerationAccounting,
    ModelProviderAttemptUsage,
    ModelProviderFeatures,
    ModelUsage,
    ProviderError,
)

from .contracts import GenerateResult, GenerateStatus


class ModelAccountingAccumulator:
    """在 ResultEnvelope 转换前收集成功和失败 Provider attempt。"""

    def __init__(self) -> None:
        self._attempts: list[ModelProviderAttemptUsage] = []

    @property
    def attempts(self) -> tuple[ModelProviderAttemptUsage, ...]:
        return tuple(self._attempts)

    def record_result(
        self,
        provider_id: str,
        result: GenerateResult,
        features: ModelProviderFeatures,
    ) -> ModelProviderAttemptUsage:
        if not isinstance(result, GenerateResult):
            raise TypeError("result must be GenerateResult")
        if not isinstance(features, ModelProviderFeatures):
            raise TypeError("features must be ModelProviderFeatures")
        if result.accounting is not None:
            raise ProviderError(
                "model provider must not construct aggregate accounting",
                code=ErrorCode.MODEL_ACCOUNTING_INCOMPLETE,
            )

        raw = result.attempt_accounting
        if raw is None:
            raw = self._synthesize_attempt(result, features)
        elif result.status is GenerateStatus.SUCCESS and raw.usage != result.usage:
            raise ProviderError(
                "model provider accounting does not match successful usage",
                code=ErrorCode.MODEL_ACCOUNTING_INCOMPLETE,
            )
        if raw.complete and raw.usage is None:
            raise ProviderError(
                "complete model accounting requires token usage",
                code=ErrorCode.MODEL_ACCOUNTING_INCOMPLETE,
            )
        attempt = ModelProviderAttemptUsage(
            provider_id=provider_id,
            ordinal=len(self._attempts) + 1,
            usage=raw.usage,
            complete=raw.complete,
        )
        self._attempts.append(attempt)
        return attempt

    def record_unavailable(self, provider_id: str) -> ModelProviderAttemptUsage:
        attempt = ModelProviderAttemptUsage(
            provider_id=provider_id,
            ordinal=len(self._attempts) + 1,
            complete=False,
        )
        self._attempts.append(attempt)
        return attempt

    def aggregate(self) -> ModelGenerationAccounting:
        input_tokens = sum(
            attempt.usage.input_tokens for attempt in self._attempts if attempt.usage is not None
        )
        output_tokens = sum(
            attempt.usage.output_tokens for attempt in self._attempts if attempt.usage is not None
        )
        return ModelGenerationAccounting(
            attempts=tuple(self._attempts),
            aggregate_usage=ModelUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
            complete=all(attempt.complete for attempt in self._attempts),
        )

    def attach(self, result: GenerateResult) -> GenerateResult:
        return result.model_copy(
            update={
                "attempt_accounting": None,
                "accounting": self.aggregate() if self._attempts else None,
            }
        )

    @staticmethod
    def _synthesize_attempt(
        result: GenerateResult,
        features: ModelProviderFeatures,
    ) -> ModelAttemptAccounting:
        usage = result.usage if result.status is GenerateStatus.SUCCESS else None
        complete = usage is not None and features.usage_tokens
        return ModelAttemptAccounting(
            usage=usage,
            complete=complete,
        )
