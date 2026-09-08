"""已验证渠道入口提供的通知地址；不得从 Agent 输出或 HTTP 请求正文构造。"""

from typing import Annotated, Literal

from pydantic import Field

from financeclaw.kernel.responses import ContractModel

Identifier = Annotated[str, Field(min_length=1, max_length=128)]


class NotificationAddress(ContractModel):
    """原始入站消息固定的单聊身份；接收端还必须核对持久会话绑定。"""

    channel: Literal["feishu"] = "feishu"
    app_id: Identifier
    tenant_key: Identifier
    open_id: Identifier
    chat_id: Identifier
    message_id: Identifier
