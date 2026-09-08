"""认证适配器签发的有限任务授权依据，不携带原凭据。"""

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class AuthorizationEvidence(BaseModel):
    """可信入口固定来源、验证时间和期限；不能从请求正文或模型参数构建。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    source: str = Field(pattern=r"^(oidc|feishu|development)$")
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    issued_at: AwareDatetime
    expires_at: AwareDatetime
