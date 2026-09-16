"""技能发布、按当前身份计算的可见性和固定依赖校验。"""

from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field

from financeclaw.kernel.agents import ToolRef
from financeclaw.kernel.skills import SkillError, SkillRef
from financeclaw.shared.turns.types import digest


class SkillRelease(BaseModel):
    """平台发布策略与包身份；包作者只能进一步限制隐式选择。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    ref: SkillRef
    display_name: str = Field(default="", max_length=64)
    enabled: bool = True
    allow_implicit_invocation: bool = True
    required_tools: tuple[ToolRef, ...] = ()
    required_scopes: tuple[str, ...] = ()
    tenant_allowlist: tuple[str, ...] | None = None
    policy_version: str = "skills/1"

    @property
    def policy_hash(self):
        """所有平台策略参与来源约束指纹。"""
        return digest(self.model_dump(mode="json"))


class SkillCatalog:
    """不可变包快照与发布声明，用户可见性从不全局缓存。"""

    def __init__(self, entries=()):
        """入口为 release/package 配对，重复或包身份不符立即阻止启动。"""
        values = {}
        for release, package in entries:
            key = (release.ref.skill_id, release.ref.version, release.ref.package_hash)
            if key in values or package.package_hash != release.ref.package_hash:
                raise ValueError("duplicate or mismatched skill release")
            if package.name != release.ref.skill_id:
                raise ValueError("skill ID must match package name")
            values[key] = (release, package)
        self.entries = MappingProxyType(values)

    def resolve(self, ref):
        """只解析完整固定身份，不从名称猜测版本。"""
        ref = SkillRef.model_validate(ref)
        try:
            return self.entries[(ref.skill_id, ref.version, ref.package_hash)]
        except KeyError as exc:
            raise SkillError("SKILL_RELEASE_MISMATCH") from exc

    def validate_profile(self, profile):
        """所有包和业务工具依赖必须属于同一固定发布。"""
        names = set()
        tools = {(r.tool_id, r.version) for r in profile.allowed_tools}
        for ref in profile.allowed_skills:
            release, package = self.resolve(ref)
            if package.name in names:
                raise ValueError("ambiguous skill name")
            names.add(package.name)
            if any((r.tool_id, r.version) not in tools for r in release.required_tools):
                raise ValueError("skill tool dependency is not bound to the profile")

    def authorize(self, profile, context, skill_id, *, explicit=False, invoke=False):
        """目录、激活和恢复共用当前身份判定，隐式标志不能由模型授予。"""
        ref = next((r for r in profile.allowed_skills if r.skill_id == skill_id), None)
        if ref is None:
            raise SkillError()
        release, package = self.resolve(ref)
        if (
            not release.enabled
            or (
                release.tenant_allowlist is not None
                and context.tenant_id not in release.tenant_allowlist
            )
            or ("*" not in context.scopes and not set(release.required_scopes) <= context.scopes)
        ):
            raise SkillError()
        if invoke and not explicit and not (release.allow_implicit_invocation and package.implicit):
            raise SkillError("SKILL_EXPLICIT_REQUIRED")
        return release, package

    def fingerprint(self, profile):
        """含包 manifest、平台规则及预算的固定发布内容。"""
        return digest(
            [
                profile.skill_budget.model_dump(),
                [
                    [r.model_dump(mode="json"), [f.model_dump() for f in p.resources]]
                    for r, p in (self.resolve(ref) for ref in profile.allowed_skills)
                ],
            ]
        )
