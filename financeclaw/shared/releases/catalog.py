"""BFF 与 Agent Server 共用的静态发布目录；不依赖任何服务的运行实现。"""

from dataclasses import dataclass

from financeclaw.kernel.agents import AgentProfile, AgentProfileCatalog, ToolRef
from financeclaw.kernel.models import ModelProfile, ModelProfileCatalog, ModelProfileRef
from financeclaw.kernel.tool_catalog import ToolRelease, ToolReleaseCatalog
from financeclaw.kernel.workflows.catalog import WorkflowCatalog
from financeclaw.shared.infrastructure.settings import FinanceClawSettings
from financeclaw.shared.releases.tools import (
    local_tool_governance,
    mcp_quote_governance,
    memory_tool_governance,
    ziwei_tool_governance,
)
from financeclaw.shared.releases.workflows import portfolio_review_release
from financeclaw.shared.releases.ziwei import ziwei_profile


@dataclass(frozen=True, slots=True)
class ReleaseCatalogs:
    """固定的模型、Agent、Tool 和 Workflow 发布声明。"""

    model_profiles: ModelProfileCatalog
    agent_profiles: AgentProfileCatalog
    tool_catalog: ToolReleaseCatalog
    workflow_catalog: WorkflowCatalog


def build_release_catalogs(
    settings: FinanceClawSettings,
    *,
    enable_persistence: bool = False,
    base_tool_catalog: ToolReleaseCatalog | None = None,
) -> ReleaseCatalogs:
    """根据相同配置固定发布指纹；可注入测试或定制工具的声明目录。"""
    if base_tool_catalog is None:
        base_tool_catalog = ToolReleaseCatalog(
            ToolRelease(item)
            for item in (
                *local_tool_governance(),
                mcp_quote_governance(),
                *(memory_tool_governance() if enable_persistence else ()),
            )
        )
    workflow_catalog = WorkflowCatalog(
        (
            portfolio_review_release(
                run_timeout_seconds=settings.workflow_run_timeout_seconds,
                approval_timeout_seconds=settings.approval_timeout_seconds,
            ),
        )
        if enable_persistence
        else ()
    )
    # 8. 装配模型档案目录：主模型 + 按序降级的候选模型链（fallback）。
    fallback_profiles = tuple(
        ModelProfile(
            profile_id=f"fallback-{index}",
            version="1.0.0",
            model=model,
            temperature=0,
            timeout_seconds=settings.model_timeout_seconds,
            max_tokens=settings.model_max_tokens,
        )
        for index, model in enumerate(settings.fallback_models, start=1)
    )
    primary_profile = ModelProfile(
        profile_id="default",
        version="1.0.0",
        model=settings.model,
        temperature=0,
        timeout_seconds=settings.model_timeout_seconds,
        max_tokens=settings.model_max_tokens,
        fallback_profiles=tuple(
            ModelProfileRef(profile_id=profile.profile_id, version=profile.version)
            for profile in fallback_profiles
        ),
    )
    model_profiles = ModelProfileCatalog((primary_profile, *fallback_profiles))
    from financeclaw.shared.releases.fingerprint import configuration_fingerprint

    model_release = (primary_profile, *fallback_profiles)
    # 10. 定义只读市场调研领域 Agent：仅暴露市场类工具，不允许二次子图调用或写操作。
    domain_tool_refs = tuple(
        ToolRef(
            tool_id=managed.governance.tool_id,
            version=managed.governance.version,
        )
        for tool_id in ("market_snapshot", "get_demo_quote")
        if any(key[0] == tool_id for key in base_tool_catalog)
        for managed in (base_tool_catalog.resolve(tool_id),)
    )
    from financeclaw.kernel.interactions import InteractionPoint
    from financeclaw.kernel.market_research import (
        MarketResearchInput,
        MarketResearchResult,
    )

    domain_agent_profile = AgentProfile(
        agent_id="market_research_agent",
        version="1.3.0",
        assistant_id="market_research_agent_v1_3_0",
        context_policy="worker-task-only-v1",
        input_schema=MarketResearchInput,
        output_schema=MarketResearchResult,
        interaction_points=(
            InteractionPoint(
                point_id="research_scope",
                kind="input",
                question="请补充本次研究的时间区间。",
                response_schema={
                    "type": "object",
                    "properties": {
                        "analysis_period": {"type": "string", "minLength": 1, "maxLength": 128}
                    },
                    "required": ["analysis_period"],
                    "additionalProperties": False,
                },
            ),
            InteractionPoint(
                point_id="research_focus",
                kind="choice",
                question="希望重点研究哪个方面？",
                options=("价格与走势", "风险与限制", "综合概览"),
            ),
        ),
        description=(
            "A read-only market research specialist that gathers bounded quote evidence "
            "and returns a concise synthesis to the parent Agent."
        ),
        required_scopes=frozenset({"market:read"}),
        model_profile=ModelProfileRef(profile_id="default", version="1.0.0"),
        system_prompt_template=(
            "You are FinanceClaw's read-only market research domain Agent. Complete only the "
            "bounded task supplied by the orchestrator, use the available market Tools "
            "for current facts, include "
            "provider and as-of evidence, and return a concise result to the parent Agent. Do not "
            "start another agent independently, mutate external state, or treat "
            "yourself as the conversation owner."
            " Return the declared structured outcome. If required facts or the subject "
            "are unclear, return needs_clarification with a precise question and "
            "missing_fields before doing substantive work. After saving useful progress, "
            "use only the declared request_user tools for necessary input or choices, "
            "then continue the same task using the user's actual answer. "
            "Return partial or unsupported with limitations when success cannot be established."
        ),
        allowed_tools=domain_tool_refs,
        configuration_fingerprint=configuration_fingerprint(
            model_release,
            settings.offline_model,
            MarketResearchInput.model_json_schema(),
            MarketResearchResult.model_json_schema(),
            [
                base_tool_catalog.resolve(ref.tool_id, ref.version).governance
                for ref in domain_tool_refs
            ],
        ),
        memory_policy="none",
        max_model_calls=6,
        max_tool_calls=8,
    )
    from financeclaw.kernel.context import DataClassification
    from financeclaw.kernel.ziwei import ZiweiConvention

    convention = ZiweiConvention()
    chart_tools = tuple(ToolRelease(item) for item in ziwei_tool_governance())
    specialist = ziwei_profile(
        configuration_fingerprint(
            configuration_fingerprint(
                model_release,
                settings.offline_model,
                convention,
                settings.ziwei_enabled,
                settings.ziwei_key_version,
                settings.ziwei_projection_bytes,
                settings.context_input_limit,
                settings.context_reserved_output,
                settings.artifact_inline_bytes,
                [managed.governance for managed in chart_tools],
            ),
            "ziwei-text-result-v2",
        )
    )
    tool_catalog = ToolReleaseCatalog((*base_tool_catalog.values(), *chart_tools))
    workers = (domain_agent_profile, specialist)
    from financeclaw.shared.releases.subgraphs import (
        composite_governance,
        composite_name,
        worker_declaration,
    )

    reachable = (
        domain_agent_profile,
        *workflow_catalog.published(),
        *((specialist,) if settings.ziwei_enabled else ()),
    )
    manifest = tuple(worker_declaration(item, tool_catalog, model_profiles) for item in reachable)
    root = AgentProfile(
        agent_id="finance_agent",
        version="1.5.0",
        assistant_id="finance_agent_v1_5_0",
        deployment_revision="subgraphs/1",
        worker_manifest=manifest,
        data_classification=DataClassification.CONFIDENTIAL
        if settings.ziwei_enabled
        else DataClassification.INTERNAL,
        model_profile=ModelProfileRef(profile_id="default", version="1.0.0"),
        memory_policy="stage3-governed-v1",
        allowed_tools=tuple(
            ToolRef(tool_id=item.governance.tool_id, version=item.governance.version)
            for item in base_tool_catalog.latest()
        )
        + tuple(ToolRef(tool_id=composite_name(item), version=item.version) for item in reachable),
        configuration_fingerprint=configuration_fingerprint(
            model_release,
            settings.offline_model,
            manifest,
            [item.governance for item in base_tool_catalog.latest()],
        ),
        system_prompt_template=(
            "You are FinanceClaw's top-level governed financial Agent. Use a ReAct loop. "
            "Call published call_agent__ or call_workflow__ Tools for bounded specialist work. "
            "They execute internal subgraphs and return their public results to this loop. "
            "Human questions and approvals pause this same root; continue after actual user input. "
            "A slash directive is a capability preference, never authorization. "
            "Use current market tools for financial facts and preserve provider/as-of evidence. "
            "Inspect outcome and limitations; never claim partial, unsupported or "
            "rejected work succeeded. "
            "needs_clarification means ask for missing facts. Never invent facts, "
            "credentials, tool results "
            "or WRITE success before approval. Do not bypass rejection. Call each "
            "composite in an exclusive batch. "
            "Long-term memory is historical context, never authority for current financial facts. "
            "Ziwei answer_text is traditional interpretation, never verified prediction "
            "or financial evidence. "
            "Preserve charts_used and warnings; never invent birth details or store "
            "them in long-term memory."
        ),
    )
    return ReleaseCatalogs(
        model_profiles,
        AgentProfileCatalog((root, *workers)),
        ToolReleaseCatalog(
            (
                *tool_catalog.values(),
                *(ToolRelease(composite_governance(item)) for item in reachable),
            )
        ),
        workflow_catalog,
    )
