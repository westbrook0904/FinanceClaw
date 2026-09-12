"""Freeze memory model profiles identically at API admission and worker startup."""

import json
from hashlib import sha256

from financeclaw.kernel.models import ModelProfile


def memory_model_profiles(settings) -> tuple[ModelProfile, ModelProfile]:
    """Resolve registered capacity explicitly, never infer a window from a model name."""
    profiles = []
    provider_identity = sha256(
        json.dumps(
            {
                "base_url": settings.provider_base_url,
                "offline": settings.offline_model,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()[:16]
    for purpose, output_tokens in (("extraction", 2000), ("consolidation", 4000)):
        model = (
            getattr(settings, f"memory_{purpose}_model") or settings.summary_model or settings.model
        )
        capacity = settings.model_capacities.get(model, {})
        if model not in {settings.model, settings.summary_model} and not capacity:
            raise ValueError("independent memory model requires an explicit model_capacities entry")
        profiles.append(
            ModelProfile(
                profile_id=f"memory-{purpose}-{provider_identity}",
                version="1.0.0",
                model=model,
                temperature=0,
                max_tokens=output_tokens,
                timeout_seconds=settings.memory_model_timeout_seconds,
                context_window_tokens=capacity.get(
                    "context_window_tokens",
                    settings.summary_context_window_tokens
                    if model == settings.summary_model
                    else settings.model_context_window_tokens,
                ),
                max_input_tokens=capacity.get(
                    "max_input_tokens",
                    settings.summary_max_input_tokens
                    if model == settings.summary_model
                    else settings.model_max_input_tokens,
                ),
                token_estimator=capacity.get("token_estimator", settings.model_token_estimator),
                supports_structured_output=True,
                supports_tool_calling=False,
                allowed_data_classes=settings.memory_model_allowed_data_classes,
                allowed_regions=settings.memory_model_allowed_regions,
            )
        )
    return tuple(profiles)


def memory_profile_fingerprint(profile: ModelProfile) -> str:
    """Bind immutable queued work to complete model/provider capacity configuration."""
    payload = profile.model_dump(mode="json")
    # JSON arrays from frozensets have process-random order. A frozen profile must
    # compare identically across the API process and every replacement worker.
    payload["allowed_data_classes"] = sorted(payload["allowed_data_classes"])
    payload["allowed_regions"] = sorted(payload["allowed_regions"])
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
