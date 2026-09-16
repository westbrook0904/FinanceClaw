"""固定技能发布、访问来源和容量契约；不包含执行或文件系统依赖。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SkillError(ValueError):
    """可跨 API、工具及渠道投影的有界技能业务错误。"""

    def __init__(self, code: str = "SKILL_UNAVAILABLE"):
        """只使用平台文案，异常不携带包正文、路径或租户诊断。"""
        self.code = code
        self.message = {
            "SKILL_DIRECTIVE_INVALID": "请使用 /skill 技能名称 任务正文。",
            "SKILL_UNAVAILABLE": "当前无法使用该技能或资料。",
            "SKILL_EXPLICIT_REQUIRED": "该技能需要用户通过 /skill 明确选择。",
            "SKILL_DEPENDENCY_UNAVAILABLE": "该技能所需的业务能力当前不可用。",
            "SKILL_ACTIVATION_LIMIT": "本次任务已达到技能数量上限。",
            "SKILL_CONTEXT_BUDGET_EXCEEDED": "当前任务内容无法容纳完整技能指导。",
            "SKILL_RESOURCE_INVALID": "技能资料路径或分页位置无效。",
            "SKILL_RELEASE_MISMATCH": "本次任务固定的技能版本不可用。",
        }[code]
        super().__init__(self.message)

    def payload(self) -> dict:
        """返回所有公开入口共用的稳定错误结构。"""
        return {"code": self.code, "message": self.message}


class SkillRef(BaseModel):
    """由平台发布绑定的完整包身份，运行期不解析 latest。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    skill_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    package_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class SkillResource(BaseModel):
    """包内规范相对路径及原始字节摘要，用于固定版本资源读取。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str = Field(min_length=1, max_length=256)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0, le=262144)
    media_type: str


class SkillBudget(BaseModel):
    """独立区域限额；最终仍服从模型完整输入的共同窗口。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    catalog_tokens: int = Field(default=1024, ge=1, le=4096)
    body_tokens: int = Field(default=2048, ge=1, le=8192)
    active_tokens: int = Field(default=4096, ge=1, le=16384)
    max_active: int = Field(default=2, ge=1, le=8)
    page_tokens: int = Field(default=1024, ge=128, le=4096)
    page_bytes: int = Field(default=8192, ge=512, le=16384)


class SkillAccessRef(BaseModel):
    """平台签发的派生内容来源；授权仍需按当前运行重新验证。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    ref: SkillRef
    policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_turn_id: str | None = None
    source_scope: str = Field(min_length=1, max_length=128)
    required_scopes: tuple[str, ...] = ()
    resource_path: str | None = None
    resource_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_resource_span(self):
        """资源来源必须包含完整 hash 和字符范围，主文依赖不携带半个资源引用。"""
        values = (self.resource_path, self.resource_hash, self.start, self.end)
        if any(v is not None for v in values) and (
            any(v is None for v in values) or self.end < self.start
        ):
            raise ValueError("skill resource provenance requires a complete ordered span")
        return self


class SkillPreparation(BaseModel):
    """显式准备的公开流事件，不包含正文、权限或工具参数。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["skill.preparation"] = "skill.preparation"
    event_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    skill: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    source: Literal["explicit"] = "explicit"
    status: Literal["preparing", "prepared", "failed"]
