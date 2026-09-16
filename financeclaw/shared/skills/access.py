"""技能派生来源并集及读取授权；不依赖图运行时或进程全局用户状态。"""

from financeclaw.kernel.skills import SkillAccessRef, SkillError
from financeclaw.shared.skills.packages import canonical

ACCESS_KEY = "financeclaw_skill_access"
RESOURCE_KEY = "financeclaw_skill_resources"


def merge_access(*groups):
    """去重并严格验证来源，超过容量时失败而不删除权限约束。"""
    groups_by_source = {}
    for group in groups:
        for raw in group or ():
            item = SkillAccessRef.model_validate(raw).model_dump(mode="json")
            identity = canonical({k: v for k, v in item.items() if k not in {"start", "end"}})
            groups_by_source.setdefault(identity, []).append(item)
    items = []
    for identity in sorted(groups_by_source):
        fragments = groups_by_source[identity]
        if fragments[0]["resource_path"] is None:
            items.append(fragments[0])
            continue
        merged = []
        for fragment in sorted(fragments, key=lambda v: (v["start"], v["end"])):
            if merged and fragment["start"] <= merged[-1]["end"]:
                merged[-1]["end"] = max(merged[-1]["end"], fragment["end"])
            else:
                merged.append(fragment)
        items.extend(merged)
    if len(items) > 64:
        raise SkillError("SKILL_CONTEXT_BUDGET_EXCEEDED")
    return items


def require_access(refs, authorizer=None):
    """缺少可信图授权适配器的通用文件入口拒绝读取技能派生正文。"""
    values = merge_access(refs)
    if values:
        if authorizer is None:
            raise SkillError()
        authorizer(values)
    return values
