"""工具进度的公开展示契约，不包含调用参数、工具结果或异常正文。"""

from typing import Literal

from pydantic import Field

from financeclaw.kernel.responses import ContractModel

TOOL_PROGRESS_LIMIT = 24


class ToolProgress(ContractModel):
    """按原生节点命名空间和调用 ID 关联一次工具调用，供根图和子图共用。

    call_id 是内部身份的摘要；agent 和 tool 取自装配时固定的目录。
    status 只表示工具调用状态，不能作为整个任务完成或业务交付的凭据。
    """

    type: Literal["tool.progress"] = "tool.progress"
    call_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    agent: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.:-]+$")
    tool: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.:-]+$")
    status: Literal["started", "completed", "failed", "interrupted", "cancelled"]
