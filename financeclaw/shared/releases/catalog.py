"""三个服务共用的静态发布目录；不依赖任何服务的运行实现。"""

from dataclasses import dataclass

from financeclaw.kernel.agents import AgentProfile, AgentProfileCatalog, ToolRef
from financeclaw.kernel.delegation.naming import delegation_tool_name
from financeclaw.kernel.models import ModelProfile, ModelProfileCatalog, ModelProfileRef
from financeclaw.kernel.tool_catalog import ToolRelease, ToolReleaseCatalog
from financeclaw.kernel.workflows.catalog import WorkflowCatalog
from financeclaw.kernel.workflows.models import WorkflowStatus
from financeclaw.shared.infrastructure.settings import FinanceClawSettings
from financeclaw.shared.releases.tools import (
    delegation_governance,
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


def agent_delegation_release(profile: AgentProfile) -> ToolRelease:
    """根据 Agent 发布声明生成父工具的治理声明。"""
    return ToolRelease(
        delegation_governance(
            delegation_tool_name("agent", profile.agent_id),
            profile.required_scopes,
            profile.version,
        )
    )


def workflow_delegation_release(definition) -> ToolRelease:
    """根据 Workflow 发布声明生成父工具的治理声明。"""
    return ToolRelease(
        delegation_governance(
            delegation_tool_name("workflow", definition.workflow_id),
            definition.required_scopes,
            definition.version,
        )
    )


def build_release_catalogs(
    settings: FinanceClawSettings,
    *,
    enable_persistence: bool = False,
    base_tool_catalog: ToolReleaseCatalog | None = None,
    include_subgraphs: bool = False,
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
    # 10. 定义只读市场调研领域 Agent：仅暴露市场类工具，不允许二次委派或写操作。
    domain_tool_refs = tuple(
        ToolRef(
            tool_id=managed.governance.tool_id,
            version=managed.governance.version,
        )
        for tool_id in ("market_snapshot", "get_demo_quote")
        if any(key[0] == tool_id for key in base_tool_catalog)
        for managed in (base_tool_catalog.resolve(tool_id),)
    )
    from financeclaw.kernel.delegation.market_research import (
        MarketResearchInput,
        MarketResearchResult,
    )
    from financeclaw.kernel.interactions import InteractionPoint

    domain_agent_profile = AgentProfile(
        agent_id="market_research_agent",
        version="1.2.0",
        assistant_id="market_research_agent_v1_2_0",
        context_policy="delegated-task-only-v1",
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
        delegatable=True,
        required_scopes=frozenset({"market:read"}),
        model_profile=ModelProfileRef(profile_id="default", version="1.0.0"),
        system_prompt_template=(
            "You are FinanceClaw's read-only market research domain Agent. Complete only the "
            "bounded delegated task, use the available market Tools for current facts, include "
            "provider and as-of evidence, and return a concise result to the parent Agent. Do not "
            "delegate again, mutate external state, or treat yourself as the conversation owner."
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
    # 为每个已发布且激活的 Workflow 与领域 Agent 生成委派工具，供顶层 Agent 调用。
    delegation_tools = (
        *(
            workflow_delegation_release(definition)
            for definition in workflow_catalog.published()
            if definition.status is WorkflowStatus.ACTIVE
        ),
        agent_delegation_release(domain_agent_profile),
    )
    # 把委派工具并入目录，使顶层 Agent 能以工具调用形式触发 Workflow/Agent 委派。
    tool_catalog = ToolReleaseCatalog((*base_tool_catalog.values(), *delegation_tools))
    # 11. 定义顶层 finance_agent 档案：ReAct 决策直接回答、Tool、Workflow 或委派。
    agent_profile = AgentProfile(
        agent_id="finance_agent",
        version="1.4.0",
        assistant_id="finance_agent_v1_4_0",
        configuration_fingerprint=configuration_fingerprint(
            model_release,
            settings.offline_model,
            domain_agent_profile.model_dump(mode="python"),
            [managed.governance for managed in tool_catalog.latest()],
        ),
        model_profile=ModelProfileRef(profile_id="default", version="1.0.0"),
        system_prompt_template=(
            "You are FinanceClaw's top-level governed financial Agent. Use a ReAct loop to decide "
            "whether to answer directly, call a Tool, invoke a published Workflow, or delegate a "
            "bounded task to a domain Agent. A user slash directive is an invocation preference, "
            "not identity, authorization, or permission to bypass policy. Elicit only missing "
            "required slots before invocation; when all slots are valid, use the named "
            "capability without silently substituting another. Use tools for current financial "
            "facts, preserve provider/as-of evidence, never invent tool results, never expose "
            "credentials, and never "
            "claim a WRITE occurred before approval and tool success. The root conversation always "
            "remains yours; domain Agents are delegated workers, not conversation targets. "
            "Long-term memory is user-approved historical context, never an authority for current "
            "prices, holdings, balances, financial statements, news, rates or product rules."
            " Inspect delegated outcome, not just transport status: needs_clarification means ask "
            "the user and end this turn; after their answer start a new bounded delegation. "
            "Never report failed, unsupported or partial child results as success. Preserve the "
            "subject, source, evidence and limitations; do not switch tools to bypass a rejection."
        ),
        allowed_tools=tuple(
            ToolRef(tool_id=managed.governance.tool_id, version=managed.governance.version)
            for managed in tool_catalog.latest()
        ),
        memory_policy="stage3-governed-v1",
    )
    # 统一使用根 1.4.0；候选开关仅控制紫微委派工具是否进入白名单。
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
    ziwei_delegate = agent_delegation_release(specialist)
    if settings.ziwei_enabled:
        ziwei_prompt = (
            " Handle bounded Ziwei (紫微斗数) traditional-culture requests by delegating "
            "to ziwei_doushu_agent. Never calculate star positions yourself. Collect birth "
            "calendar/date, local clock or shichen, time basis, place and sex_for_chart; "
            "do not invent missing fields. Set requested level and explicit target. "
            "Preserve chart evidence and warnings. Astrology is not financial evidence. "
            "Never store birth data or divination conclusions in long-term memory."
            " Ziwei answer_text is free-form traditional interpretation, "
            "not a verified prediction. charts_used records calculated sources, "
            "not proof of every claim."
        )
        agent_profile = agent_profile.model_copy(
            update={
                "version": "1.4.0",
                "assistant_id": "finance_agent_v1_4_0",
                "deployment_revision": "stage7-text/1",
                "data_classification": DataClassification.CONFIDENTIAL,
                "allowed_tools": (
                    *agent_profile.allowed_tools,
                    ToolRef(tool_id=ziwei_delegate.governance.tool_id, version=specialist.version),
                ),
                "configuration_fingerprint": configuration_fingerprint(
                    agent_profile.configuration_fingerprint,
                    specialist.model_dump(mode="json"),
                    ziwei_delegate.governance,
                    "ziwei-text-result-v2",
                ),
                "system_prompt_template": agent_profile.system_prompt_template + ziwei_prompt,
            }
        )
    agent_profiles = AgentProfileCatalog((agent_profile, domain_agent_profile, specialist))
    tool_catalog = ToolReleaseCatalog((*tool_catalog.values(), *chart_tools, ziwei_delegate))

    releases = ReleaseCatalogs(model_profiles, agent_profiles, tool_catalog, workflow_catalog)
    if include_subgraphs:
        from financeclaw.shared.releases.subgraphs import with_subgraph_releases

        return with_subgraph_releases(releases)
    return releases
