"""只用于隔离验收的发布配置，允许验证子 Agent 的原生写审批。"""

from dataclasses import replace

from financeclaw.kernel.agents import AgentProfileCatalog, ToolRef
from financeclaw.shared.releases.fingerprint import configuration_fingerprint
from tests.support import build_components


def build_live_components(settings):
    """生产市场 Agent 仍只读；测试发布额外允许本地演示写工具并标记修订。"""
    components = build_components(settings, enable_persistence=True)
    domain = components.agent_profiles.resolve("market_research_agent")
    domain = domain.model_copy(
        update={
            "allowed_tools": (
                *domain.allowed_tools,
                ToolRef(tool_id="watchlist_add", version="1.0.0"),
            ),
            "deployment_revision": domain.deployment_revision + "/live-test",
        }
    )
    root = components.default_agent_profile.model_copy(
        update={
            "deployment_revision": components.default_agent_profile.deployment_revision
            + "/live-test",
            "configuration_fingerprint": configuration_fingerprint(
                components.default_agent_profile.model_dump(mode="json"),
                domain.model_dump(mode="json"),
            ),
        }
    )
    return replace(components, agent_profiles=AgentProfileCatalog((root, domain)))
