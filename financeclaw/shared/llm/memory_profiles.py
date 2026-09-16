"""Freeze memory model profiles identically at API admission and worker startup."""

import json
from hashlib import sha256

from financeclaw.kernel.models import ModelProfile, ModelProfileCatalog


def memory_model_profiles(settings) -> tuple[ModelProfile, ModelProfile]:
    """Resolve registered capacity explicitly, never infer a window from a model name."""
    configuration = settings.model_configuration
    catalog = ModelProfileCatalog(configuration.profiles())
    profiles = []
    for purpose, output_cap in (("extraction", 2000), ("consolidation", 4000)):
        selected = catalog.resolve(configuration.task_ref(f"memory_{purpose}"))
        provider_identity = sha256(
            json.dumps(
                {
                    "base_url": configuration.providers[selected.connection_id].base_url,
                    "offline": settings.offline_model,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()[:16]
        # 派生任务受已有来源许可上限约束；选用默认大模型也不能扩大输出授权。
        profiles.append(
            ModelProfile.model_validate(
                {
                    **selected.model_dump(),
                    "profile_id": f"memory-{purpose}-{selected.profile_id}-{provider_identity}",
                    "max_tokens": min(selected.max_tokens, output_cap),
                    # 结构化记忆的有限输出额度用于 JSON，不能被 Qwen 思考耗尽。
                    # 仅派生后台档案，保留聊天模型的原有配置。
                    "enable_thinking": (
                        False if selected.model.startswith("openai:qwen") else None
                    ),
                    "fallback_profiles": (),
                    "supports_tool_calling": False,
                    "allowed_data_classes": selected.allowed_data_classes
                    & settings.memory_model_allowed_data_classes,
                    "allowed_regions": selected.allowed_regions
                    & settings.memory_model_allowed_regions,
                }
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
