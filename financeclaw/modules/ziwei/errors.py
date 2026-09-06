"""安全领域错误，不携带出生资料或第三方异常正文。"""


class ZiweiError(ValueError):
    """携带可返回用户的错误码、说明和可选澄清字段。"""

    def __init__(self, code: str, message: str, fields: tuple[str, ...] = ()) -> None:
        """错误本身不拼接原始输入，以免进入异常日志。"""
        super().__init__(message)
        self.code = code
        self.fields = fields
