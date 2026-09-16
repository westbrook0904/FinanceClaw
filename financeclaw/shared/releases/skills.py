"""内置技能及工具发布声明；API 与 Worker 从同一清单校验包。"""

import json
from pathlib import Path

from financeclaw.kernel.agents import ToolRef
from financeclaw.kernel.skills import SkillRef
from financeclaw.kernel.tools import (
    ApprovalMode,
    Egress,
    Idempotency,
    RetryProfile,
    RiskLevel,
    Sensitivity,
    SideEffect,
    ToolGovernance,
)
from financeclaw.shared.skills.catalog import SkillCatalog, SkillRelease
from financeclaw.shared.skills.packages import load_package

SKILL_TOOLS = frozenset({"load_skill", "read_skill_resource"})


def builtin_skill_release(ref: SkillRef) -> SkillRelease:
    """逐项声明已审阅包的依赖；新增目录不能自动获得发布或业务权限。"""
    match ref.skill_id:
        case "market-brief":
            return SkillRelease(
                ref=ref,
                display_name="行情简报",
                required_tools=(ToolRef(tool_id="market_snapshot", version="1.0.0"),),
                required_scopes=("market:read",),
            )
        case "cocktail-from-what-i-have":
            return SkillRelease(ref=ref, display_name="现有材料调酒")
        case _:
            raise ValueError("skill has no reviewed platform release policy")


def builtin_skills() -> SkillCatalog:
    """启动时核验固定 manifest，内容变更必须同步发布新版本及 hash。"""
    root = Path(__file__).parents[1] / "skills/builtin"
    index = json.loads((root / "index.json").read_text())
    entries = []
    for item in index:
        ref = SkillRef.model_validate(item)
        release = builtin_skill_release(ref)
        package = load_package(root / ref.skill_id, ref.package_hash)
        entries.append((release, package))
    return SkillCatalog(entries)


def skill_tool_governance():
    """加载必须独占；资源读取保持普通只读治理，不允许瞬态自动重试。"""
    return tuple(
        ToolGovernance(
            tool_id=name,
            version="1.0.0",
            side_effect=SideEffect.READ,
            idempotency=Idempotency.IDEMPOTENT,
            risk_level=RiskLevel.LOW,
            approval=ApprovalMode.NONE,
            egress=Egress.NONE,
            sensitivity=Sensitivity.INTERNAL,
            retry_profile=RetryProfile.NONE,
            exclusive_batch=name == "load_skill",
        )
        for name in sorted(SKILL_TOOLS)
    )
